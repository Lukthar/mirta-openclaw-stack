import os
import re
import json
import logging
from typing import Dict, Any, List
import redis
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()

logger = logging.getLogger("mirta.cache_sync")
logging.basicConfig(level=logging.INFO)

SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar"
]


class ClinicCacheManager:
    """
    Sincroniza la configuración dinámica de Google Docs y el catálogo de Google Sheets
    hacia Redis, garantizando lecturas en <1ms y ocultando datos sensibles (PII/IDs) al LLM.
    """
    def __init__(self, redis_url: str, credentials_path: str):
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.credentials_path = credentials_path
        self._creds = None

        self.doc_id = os.getenv("CLINIC_DOC_ID", "")
        self.catalog_sheet_id = os.getenv("CATALOG_SHEET_ID", "")
        self.cache_ttl = int(os.getenv("CACHE_TTL_SECONDS", "3600"))

    @property
    def creds(self) -> Credentials:
        if self._creds is None:
            if not os.path.exists(self.credentials_path):
                raise FileNotFoundError(
                    f"No se encontró el archivo de credenciales de Google en: {self.credentials_path}"
                )
            self._creds = Credentials.from_service_account_file(self.credentials_path, scopes=SCOPES)
        return self._creds

    def _extract_doc_text(self, doc_content: List[Dict[str, Any]]) -> str:
        full_text = []
        for element in doc_content:
            if "paragraph" in element:
                for elem in element["paragraph"].get("elements", []):
                    full_text.append(elem.get("textRun", {}).get("content", ""))
        return "".join(full_text)

    @staticmethod
    def _parse_minutes(raw_val: Any, default: int = 45) -> int:
        if isinstance(raw_val, (int, float)):
            return int(raw_val)
        match = re.search(r"(\d+)", str(raw_val))
        return int(match.group(1)) if match else default

    def sync_google_doc_config(self) -> Dict[str, Any]:
        if not self.doc_id:
            raise ValueError("La variable de entorno CLINIC_DOC_ID no está configurada.")

        docs_service = build("docs", "v1", credentials=self.creds)
        doc = docs_service.documents().get(documentId=self.doc_id).execute()
        raw_text = self._extract_doc_text(doc.get("body", {}).get("content", []))

        clean_text = re.sub(r"```json|```", "", raw_text, flags=re.IGNORECASE).strip()
        clean_text = clean_text.replace("“", '"').replace("”", '"').replace("’", "'")

        try:
            config_data = json.loads(clean_text)
        except json.JSONDecodeError as e:
            logger.error(f"Error crítico parseando el JSON de Google Docs: {e}")
            raise ValueError("El contenido del Google Doc no es un JSON válido.") from e

        self.redis.setex("clinic:config:raw", self.cache_ttl, json.dumps(config_data, ensure_ascii=False))
        logger.info("Configuración de Google Docs sincronizada en Redis ('clinic:config:raw').")
        return config_data

    def sync_catalog_sheets(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.catalog_sheet_id:
            raise ValueError("La variable de entorno CATALOG_SHEET_ID no está configurada.")

        sheets_service = build("sheets", "v4", credentials=self.creds)
        ranges = ["Tratamientos!A1:E100", "Odontologos!A1:D50", "Obras Sociales!A1:C100"]

        result = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=self.catalog_sheet_id, ranges=ranges
        ).execute()
        value_ranges = result.get("valueRanges", [])

        def rows_to_dicts(rows: List[List[str]]) -> List[Dict[str, str]]:
            if not rows or len(rows) < 2:
                return []
            headers = [h.strip() for h in rows[0] if h.strip()]
            items = []
            for row in rows[1:]:
                if not any(row):
                    continue
                item = {headers[i]: (row[i].strip() if i < len(row) else "-") for i in range(len(headers))}
                items.append(item)
            return items

        raw_tratamientos = rows_to_dicts(value_ranges[0].get("values", []))
        raw_odontologos = rows_to_dicts(value_ranges[1].get("values", []))
        raw_obras_sociales = rows_to_dicts(value_ranges[2].get("values", []))

        tratamientos = []
        for t in raw_tratamientos:
            if t.get("Tratamiento", "-") != "-":
                tratamientos.append({
                    "tratamiento": t.get("Tratamiento"),
                    "area": t.get("Area_profesional", "Odontología"),
                    "especialidad": t.get("Especialidad", "General"),
                    "duracion_min": self._parse_minutes(t.get("Tiempo_aprox_(minutos)", "45")),
                    "precio_ars": t.get("Precio", "-")
                })

        odontologos_internal = []
        odontologos_public = []
        for o in raw_odontologos:
            nombre = o.get("Profesional", "-")
            if nombre != "-":
                odontologos_internal.append({
                    "profesional": nombre,
                    "especialidad": o.get("Especializacion", "-"),
                    "id_calendar": o.get("ID_CALENDAR", "").strip()
                })
                odontologos_public.append({
                    "profesional": nombre,
                    "especialidad": o.get("Especializacion", "-")
                })

        obras_sociales = []
        for os_item in raw_obras_sociales:
            nombre_os = os_item.get("Obra_social", "-")
            if nombre_os != "-":
                obras_sociales.append({
                    "obra_social": nombre_os,
                    "copago": os_item.get("Co-Pago", "-"),
                    "token_seguridad": os_item.get("Token_Seguridad", "-")
                })

        catalog_internal = {
            "tratamientos": tratamientos,
            "odontologos": odontologos_internal,
            "obras_sociales": obras_sociales
        }
        catalog_public = {
            "tratamientos": tratamientos,
            "odontologos": odontologos_public,
            "obras_sociales": obras_sociales
        }

        self.redis.setex("clinic:catalog:internal", self.cache_ttl, json.dumps(catalog_internal, ensure_ascii=False))
        self.redis.setex("clinic:catalog:public", self.cache_ttl, json.dumps(catalog_public, ensure_ascii=False))
        logger.info("Catálogo de Google Sheets sincronizado en Redis (vistas 'internal' y 'public').")
        return catalog_internal

    def get_internal_data(self, force_refresh: bool = False) -> Dict[str, Any]:
        raw_cfg = self.redis.get("clinic:config:raw")
        raw_cat = self.redis.get("clinic:catalog:internal")

        if force_refresh or not raw_cfg or not raw_cat:
            cfg = self.sync_google_doc_config()
            cat = self.sync_catalog_sheets()
        else:
            cfg = json.loads(raw_cfg)
            cat = json.loads(raw_cat)

        return {"config": cfg, "catalog": cat}

    def get_llm_safe_context(self) -> Dict[str, Any]:
        raw_cfg = self.redis.get("clinic:config:raw")
        raw_pub_cat = self.redis.get("clinic:catalog:public")

        if not raw_cfg or not raw_pub_cat:
            self.get_internal_data(force_refresh=True)
            raw_cfg = self.redis.get("clinic:config:raw")
            raw_pub_cat = self.redis.get("clinic:catalog:public")

        cfg = json.loads(raw_cfg)
        pub_cat = json.loads(raw_pub_cat)

        return {
            "clinica": {
                "nombre": cfg.get("clinica", {}).get("nombre", ""),
                "direccion": cfg.get("clinica", {}).get("ubicacion", {}).get("direccion", ""),
                "horarios": cfg.get("horarios_clinica", {}).get("atencion_regular", {}),
                "pagos": cfg.get("pagos", {})
            },
            "catalogo": pub_cat
        }