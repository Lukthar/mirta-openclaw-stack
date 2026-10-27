import re
import json
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple
import redis
from pydantic import BaseModel, Field, field_validator

ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

INJECTION_PATTERNS = [
    r"(?i)\*\*\s*(secretaria|asistente|assistant|system|sistema)\s*:\s*\*\*",
    r"(?i)^\s*(secretaria|asistente|assistant|system|sistema)\s*:",
    r"</?\s*(Historial|Lista_Servicios|Lista_Profesionales|Lista_Turnos|Lista_ObrasSociales|Datos_Paciente|Contexto_Clinico|Contexto_Sistema|Turnos_Activos_Paciente|audio)\s*>",
    r"(?i)(ignora\s+todas\s+las\s+instrucciones|ignore\s+all\s+previous\s+instructions|system\s+prompt)"
]


def sanitize_user_input(raw_text: str, max_length: int = 1500) -> str:
    """
    Arnés 1 (Pre-LLM): Limpia inyecciones de etiquetas XML, falsificación de roles en el historial
    y caracteres de control provenientes de WhatsApp o de transcripciones de audio.
    """
    if not raw_text:
        return ""

    text = raw_text.strip()[:max_length]
    for pattern in INJECTION_PATTERNS:
        text = re.sub(pattern, "[FILTRADO]", text)

    text = text.replace("<", "‹").replace(">", "›")
    return text


def normalize_whatsapp_phone(raw_jid_or_phone: str) -> str:
    """
    Sanitiza un JID de Evolution API o número telefónico:
    1. Elimina '@s.whatsapp.net', '@c.us', '@lid' y sufijos de dispositivo (ej. ':14').
    2. Conserva únicamente los dígitos numéricos.
    """
    if not raw_jid_or_phone:
        return ""
    base_part = str(raw_jid_or_phone).strip().split("@")[0].split(":")[0]
    return re.sub(r"\D", "", base_part)


def format_canonical_argentina_phone(raw_jid_or_phone: str) -> str:
    """Devuelve siempre '549' + 10 dígitos para guardar limpio en la columna 'Contacto' de Sheets."""
    digits = normalize_whatsapp_phone(raw_jid_or_phone)
    if len(digits) >= 10:
        return f"549{digits[-10:]}"
    return digits


class PatientBookingInput(BaseModel):
    """Esquema estricto para validar datos del paciente antes de tocar Calendar o Sheets."""
    slot_token: str = Field(..., min_length=6, max_length=20)
    dni: str = Field(..., description="DNI numérico del paciente")
    nombre: str = Field(..., min_length=3, max_length=80)
    mail: str = Field(..., pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    obra_social: str = Field(..., min_length=2, max_length=60)

    @field_validator("dni")
    @classmethod
    def clean_and_validate_dni(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if not (7 <= len(digits) <= 9):
            raise ValueError("El DNI debe contener entre 7 y 9 dígitos numéricos.")
        return digits

    @field_validator("nombre", "obra_social")
    @classmethod
    def prevent_formula_injection(cls, v: str) -> str:
        return re.sub(r"^[=+\-@]+", "", v.strip())


class SecurityHarness:
    """
    Middleware determinista para hacer cumplir:
    - Autenticación de Secretaria vía Google Docs cacheado en Redis
    - Regla de los Dos Turnos mediante Slot Tokens con validación de turn_id y auto_bookable
    - Cálculo de ventana horaria (Tiempo_aprox + 15 min)
    - Propiedad del turno por JID sanitizado de WhatsApp
    """
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def is_authorized_secretary(self, sender_jid: str, clinic_config: Dict[str, Any]) -> bool:
        clinica = clinic_config.get("clinica", {})
        allowed_phones = {
            format_canonical_argentina_phone(clinica.get("telefono_secretaria", "")),
            format_canonical_argentina_phone(clinica.get("telefono_admin", "")),
            format_canonical_argentina_phone(clinica.get("telefono", ""))
        }
        allowed_phones.discard("")
        sender_clean = format_canonical_argentina_phone(sender_jid)
        return bool(sender_clean and sender_clean in allowed_phones)

    def calculate_appointment_window(
        self,
        start_iso: str,
        tratamiento_nombre: str,
        catalog_tratamientos: List[Dict[str, Any]],
        buffer_minutes: int = 15
    ) -> Tuple[datetime, datetime, datetime, int]:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=ARG_TZ)

        now = datetime.now(ARG_TZ)
        if start_dt <= now:
            raise ValueError("No se pueden gestionar turnos en fechas u horarios que ya pasaron.")

        if start_dt.minute not in (0, 30):
            raise ValueError("Los turnos solo pueden iniciar en el minuto :00 o :30.")

        duracion_min = 45
        for t in catalog_tratamientos:
            if t["tratamiento"].lower() == tratamiento_nombre.strip().lower():
                duracion_min = int(t["duracion_min"])
                break

        event_end_dt = start_dt + timedelta(minutes=duracion_min)
        busy_end_dt = event_end_dt + timedelta(minutes=buffer_minutes)
        return start_dt, event_end_dt, busy_end_dt, duracion_min

    def issue_slot_token(
        self,
        patient_jid: str,
        turn_id: str,
        profesional: str,
        id_calendar: str,
        tratamiento: str,
        start_iso: str,
        event_end_iso: str,
        busy_end_iso: str,
        auto_bookable: bool = False,
        ttl_seconds: int = 900
    ) -> str:
        clean_phone = format_canonical_argentina_phone(patient_jid)
        token = f"SLT-{secrets.token_hex(3).upper()}"
        key = f"slot_token:{clean_phone}:{token}"
        payload = {
            "patient_phone": clean_phone,
            "turn_id": turn_id,
            "auto_bookable": auto_bookable,
            "profesional": profesional,
            "id_calendar": id_calendar,
            "tratamiento": tratamiento,
            "start_iso": start_iso,
            "event_end_iso": event_end_iso,
            "busy_end_iso": busy_end_iso,
            "issued_at": datetime.now(ARG_TZ).isoformat()
        }
        self.redis.setex(key, ttl_seconds, json.dumps(payload, ensure_ascii=False))
        return token

    def consume_slot_token(self, patient_jid: str, token: str, current_turn_id: str) -> Dict[str, Any]:
        clean_phone = format_canonical_argentina_phone(patient_jid)
        key = f"slot_token:{clean_phone}:{token.strip().upper()}"
        raw = self.redis.get(key)
        if not raw:
            raise PermissionError(
                f"El token de horario '{token}' no existe o expiró. "
                "Debes ejecutar 'consultar_disponibilidad' primero."
            )

        data = json.loads(raw)

        if data.get("turn_id") == current_turn_id and not data.get("auto_bookable", False):
            raise PermissionError(
                "REGLA DE LOS DOS TURNOS VIOLADA: Acabas de consultar este horario en este mismo turno. "
                "No puedes agendarlo todavía. Detente, envíale las opciones de horario al paciente "
                "y espera su confirmación explícita en el próximo mensaje."
            )

        self.redis.delete(key)
        return data

    def acquire_booking_lock(self, id_calendar: str, start_iso: str, ttl_seconds: int = 30) -> bool:
        lock_key = f"lock:calendar:{id_calendar}:{start_iso}"
        return bool(self.redis.set(lock_key, "LOCKED", nx=True, ex=ttl_seconds))

    def release_booking_lock(self, id_calendar: str, start_iso: str) -> None:
        lock_key = f"lock:calendar:{id_calendar}:{start_iso}"
        self.redis.delete(lock_key)

    @staticmethod
    def verify_appointment_ownership(
        sheet_row: Dict[str, Any],
        session_jid: str,
        provided_dni: Optional[str] = None
    ) -> bool:
        row_phone = normalize_whatsapp_phone(str(sheet_row.get("Contacto", "")))
        session_phone = normalize_whatsapp_phone(session_jid)

        if len(row_phone) >= 10 and len(session_phone) >= 10:
            if row_phone[-10:] == session_phone[-10:]:
                return True

        if provided_dni and len(row_phone) < 10:
            row_dni = re.sub(r"\D", "", str(sheet_row.get("DNI", "")))
            clean_dni = re.sub(r"\D", "", str(provided_dni))
            return bool(row_dni and row_dni == clean_dni)

        return False


def format_whatsapp_output(raw_response: str, max_bubbles: int = 3) -> List[Dict[str, Any]]:
    """
    Arnés de Salida (Post-LLM):
    1. Elimina signos de apertura '¿' y '¡'.
    2. Divide el mensaje por '\n\n' en un máximo de 3 burbujas coherentes.
    3. Calcula el delay de 'composing' (20ms por carácter).
    """
    clean_text = raw_response.replace("¿", "").replace("¡", "").strip()
    if not clean_text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", clean_text) if p.strip()]

    if len(paragraphs) > max_bubbles:
        chunk_size = (len(paragraphs) + max_bubbles - 1) // max_bubbles
        grouped = [
            "\n\n".join(paragraphs[i:i + chunk_size])
            for i in range(0, len(paragraphs), chunk_size)
        ]
        paragraphs = grouped[:max_bubbles]

    bubbles = []
    for part in paragraphs:
        delay_ms = min(max(len(part) * 20, 800), 6000)
        bubbles.append({
            "text": part,
            "presence": "composing",
            "delay": delay_ms
        })

    return bubbles