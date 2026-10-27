import os
import re
import json
import secrets
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

from cache_sync import ClinicCacheManager
from security_harness import ARG_TZ
from server import (
    _get_google_services,
    _read_all_turnos_rows,
    _send_evolution_whatsapp,
    TURNOS_SHEET_ID,
    TURNOS_SHEET_NAME
)

load_dotenv()

logger = logging.getLogger("mirta.cron_workers")
logging.basicConfig(level=logging.INFO)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "./credentials/service_account.json")
PUBLIC_GATEWAY_URL = os.getenv("PUBLIC_GATEWAY_URL", "http://localhost:8000")

cache_mgr = ClinicCacheManager(redis_url=REDIS_URL, credentials_path=GOOGLE_CREDS_PATH)

MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}
DIAS_SEMANA_ES = {
    0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
    4: "viernes", 5: "sábado", 6: "domingo"
}


def _primer_nombre(nombre_completo: str) -> str:
    if not nombre_completo:
        return "Hola"
    return nombre_completo.strip().split()[0]


def cron_sync_manual_calendar_changes():
    """
    Detecta acciones manuales de la secretaria en Google Calendar:
    - Ignora eventos con flag 'Ocupado' (vacaciones/días libres).
    - Sincroniza y embellece turnos nuevos creados en mostrador y envía confirmación.
    - Detecta turnos eliminados en Calendar, marca 'CANCELO' en Sheets y avisa al paciente.
    """
    internal = cache_mgr.get_internal_data()
    cfg = internal["config"]
    odontologos = internal["catalog"]["odontologos"]
    direccion_clinica = (
        cfg.get("clinica", {}).get("ubicacion", {}).get("direccion")
        or os.getenv("DEFAULT_CLINIC_ADDRESS", "")
    )

    cal_service, sheets_service = _get_google_services()
    rows = _read_all_turnos_rows(sheets_service)
    rows_by_event_id = {r["ID"]: r for r in rows if r.get("ID")}

    now = datetime.now(ARG_TZ)
    updated_min_iso = (now - timedelta(minutes=6)).isoformat()

    for prof in odontologos:
        cal_id = prof.get("id_calendar")
        nombre_prof = prof.get("profesional")
        if not cal_id:
            continue

        try:
            events_res = cal_service.events().list(
                calendarId=cal_id,
                updatedMin=updated_min_iso,
                showDeleted=True,
                singleEvents=True
            ).execute()
        except Exception as e:
            logger.error(f"Error consultando cambios en calendario de {nombre_prof}: {e}")
            continue

        for ev in events_res.get("items", []):
            ev_id = ev.get("id", "")
            status = ev.get("status", "")
            summary = ev.get("summary", "").strip()
            description = ev.get("description", "").strip()

            # 1. Ignorar vacaciones o días bloqueados con flag 'Ocupado'
            if re.search(r"(?i)\bocupado\b", description) or re.search(r"(?i)\bocupado\b", summary):
                logger.info(f"Evento {ev_id} detectado como bloqueo 'Ocupado'. Se omite sincronización.")
                continue

            # 2. Turno eliminado manualmente por la secretaria en Calendar
            if status == "cancelled":
                if ev_id in rows_by_event_id:
                    row_data = rows_by_event_id[ev_id]
                    if row_data.get("Accion") == "ACTIVO":
                        r_num = row_data["_row_number"]
                        sheets_service.spreadsheets().values().update(
                            spreadsheetId=TURNOS_SHEET_ID,
                            range=f"{TURNOS_SHEET_NAME}!J{r_num}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [["CANCELO"]]}
                        ).execute()

                        contacto = row_data.get("Contacto", "")
                        if contacto:
                            p_nombre = _primer_nombre(row_data.get("Nombre", ""))
                            msg_cancelado = (
                                f"Hola {p_nombre} cómo andás?\n"
                                f"Quería avisarte que {row_data.get('Profesional', nombre_prof)} tuvo que cancelar el turno de "
                                f"{row_data.get('Tratamiento', 'consulta')} que tenías el {row_data.get('Fecha')}, a las {row_data.get('Hora')}.\n\n"
                                f"Disculpá las molestias. 💛\n"
                                f"Si te parece, puedo ayudarte a reprogramarlo para otra fecha que te quede cómoda. "
                                f"Te mando un abrazo y espero verte pronto. 😊"
                            )
                            _send_evolution_whatsapp(contacto, msg_cancelado, delay_ms=2200)
                continue

            # 3. Turno creado manualmente por la secretaria en Calendar
            if status == "confirmed":
                if "[Sincronizado]" in description:
                    continue

                solo_numeros = re.sub(r"\D", "", description)
                if len(solo_numeros) < 10:
                    continue
                clean_phone = f"549{solo_numeros[-10:]}"

                if "-" in summary:
                    partes = summary.split("-")
                    paciente_nombre = partes[0].strip()
                    tratamiento_nombre = "-".join(partes[1:]).strip()
                else:
                    paciente_nombre = summary
                    tratamiento_nombre = "Consulta general"

                if not paciente_nombre:
                    continue

                correo = ""
                for att in ev.get("attendees", []):
                    if not att.get("organizer") and not att.get("self") and att.get("email"):
                        correo = att["email"].strip()
                        break
                if not correo:
                    match_mail = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", description)
                    if match_mail:
                        correo = match_mail.group(0)

                start_raw = ev.get("start", {}).get("dateTime")
                if not start_raw:
                    continue
                start_dt = datetime.fromisoformat(start_raw).astimezone(ARG_TZ)
                fecha_str = start_dt.strftime("%d/%m/%Y")
                hora_str = start_dt.strftime("%H:%M")
                html_link = ev.get("htmlLink", "")

                p_nombre = _primer_nombre(paciente_nombre)
                nueva_descripcion = (
                    f"Hola {p_nombre} Recorda llegar 10 minutos antes porfavor🙏 \n"
                    f"Cualquier consulta o reclamo, con gusto que podes consultar por chat! \n"
                    f"Nos vemos pronto!☺️\n\n"
                    f"📆 [Sincronizado]"
                )
                patch_body = {
                    "summary": f"{paciente_nombre} - {tratamiento_nombre}",
                    "location": direccion_clinica,
                    "description": nueva_descripcion
                }
                if correo:
                    patch_body["attendees"] = [{"email": correo}]

                try:
                    cal_service.events().patch(calendarId=cal_id, eventId=ev_id, body=patch_body).execute()
                except Exception as e:
                    logger.error(f"Error embelleciendo evento {ev_id} en Calendar: {e}")

                if ev_id in rows_by_event_id:
                    r_num = rows_by_event_id[ev_id]["_row_number"]
                    upd_row = [
                        paciente_nombre, rows_by_event_id[ev_id].get("DNI", ""), correo, clean_phone,
                        rows_by_event_id[ev_id].get("Obra_Social", "Particular"), nombre_prof,
                        tratamiento_nombre, fecha_str, hora_str, "ACTIVO", html_link, ev_id
                    ]
                    sheets_service.spreadsheets().values().update(
                        spreadsheetId=TURNOS_SHEET_ID,
                        range=f"{TURNOS_SHEET_NAME}!A{r_num}:L{r_num}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [upd_row]}
                    ).execute()
                else:
                    new_row = [
                        paciente_nombre, "", correo, clean_phone, "Particular",
                        nombre_prof, tratamiento_nombre, fecha_str, hora_str, "ACTIVO", html_link, ev_id
                    ]
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=TURNOS_SHEET_ID,
                        range=f"{TURNOS_SHEET_NAME}!A:L",
                        valueInputOption="USER_ENTERED",
                        insertDataOption="INSERT_ROWS",
                        body={"values": [new_row]}
                    ).execute()

                msg_creado = (
                    f"Hola {p_nombre} cómo andás?\n"
                    f"Te mando el mensajito así ya te queda guardado por acá el turno para {tratamiento_nombre}.\n\n"
                    f"📅 Recordá que es el {fecha_str} a las {hora_str}, con {nombre_prof}.\n\n"
                    f"Espero que tengas un hermoso día💛"
                )
                _send_evolution_whatsapp(clean_phone, msg_creado, delay_ms=2200)


def cron_recordatorios_24h():
    _, sheets_service = _get_google_services()
    rows = _read_all_turnos_rows(sheets_service)

    manana_dt = datetime.now(ARG_TZ) + timedelta(days=1)
    manana_str = manana_dt.strftime("%d/%m/%Y")
    fecha_legible = f"{DIAS_SEMANA_ES[manana_dt.weekday()]} {manana_dt.day:02d} de {MESES_ES[manana_dt.month]}"

    for r in rows:
        if r.get("Accion") == "ACTIVO" and r.get("Fecha") == manana_str and r.get("Contacto"):
            p_nombre = _primer_nombre(r.get("Nombre", ""))
            msg = (
                f"Hola {p_nombre} pasaba a recordarte que *el {fecha_legible} a las {r.get('Hora')}* "
                f"tenés {r.get('Tratamiento')} pendiente con {r.get('Profesional')} 😷\n\n"
                f"Avisame si tenés algún inconveniente. Te espero! 😊"
            )
            _send_evolution_whatsapp(r["Contacto"], msg, delay_ms=2000)


def cron_seguimiento_post_consulta():
    internal = cache_mgr.get_internal_data()
    odontologos = internal["catalog"]["odontologos"]
    contactos_medicos = {
        p["nombre"].lower(): p.get("whatsapp")
        for p in internal["config"].get("profesionales_contacto", [])
        if p.get("nombre")
    }
    tel_admin = (
        internal["config"].get("clinica", {}).get("telefono_admin")
        or os.getenv("FALLBACK_ADMIN_PHONE", "")
    )

    cal_service, sheets_service = _get_google_services()
    rows = _read_all_turnos_rows(sheets_service)
    rows_by_event_id = {r["ID"]: r for r in rows if r.get("ID")}

    now = datetime.now(ARG_TZ)
    window_start = now - timedelta(minutes=15)

    for prof in odontologos:
        cal_id = prof.get("id_calendar")
        if not cal_id:
            continue

        events = cal_service.events().list(
            calendarId=cal_id,
            timeMin=window_start.isoformat(),
            timeMax=now.isoformat(),
            singleEvents=True
        ).execute().get("items", [])

        for ev in events:
            ev_id = ev.get("id", "")
            end_raw = ev.get("end", {}).get("dateTime")
            if not end_raw or ev_id not in rows_by_event_id:
                continue

            end_dt = datetime.fromisoformat(end_raw).astimezone(ARG_TZ)
            row_data = rows_by_event_id[ev_id]

            if window_start <= end_dt <= now and row_data.get("Accion") == "ACTIVO":
                r_num = row_data["_row_number"]
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=TURNOS_SHEET_ID,
                    range=f"{TURNOS_SHEET_NAME}!J{r_num}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [["INACTIVO"]]}
                ).execute()

                form_token = secrets.token_urlsafe(12)
                cache_mgr.redis.setex(f"post_visit_token:{form_token}", 86400, json.dumps(row_data, ensure_ascii=False))

                prof_nombre = row_data.get("Profesional", prof["profesional"])
                p_nombre_prof = _primer_nombre(prof_nombre)
                target_phone = contactos_medicos.get(prof_nombre.lower()) or tel_admin

                link_asistencia = f"{PUBLIC_GATEWAY_URL.rstrip('/')}/asistencia/{form_token}"
                msg_prof = (
                    f"Hola {p_nombre_prof}😊\n\n"
                    f"Podés entrar al link y marcar si pudiste atender a *{row_data.get('Nombre')}*? 🙌\n"
                    f"👉 {link_asistencia}\n\n"
                    f"Es rapidito, no te lleva más de 5 segundos. Me ayuda para poder hacerle seguimiento después 🙏"
                )
                _send_evolution_whatsapp(target_phone, msg_prof, delay_ms=1500)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=ARG_TZ)
    scheduler.add_job(cron_sync_manual_calendar_changes, "interval", minutes=3, id="sync_calendar")
    scheduler.add_job(cron_recordatorios_24h, "cron", hour=10, minute=0, id="recordatorios_24h")
    scheduler.add_job(cron_seguimiento_post_consulta, "interval", minutes=15, id="post_consulta")
    scheduler.add_job(lambda: cache_mgr.get_internal_data(force_refresh=True), "interval", minutes=60, id="sync_cache")
    scheduler.start()
    logger.info("APScheduler iniciado con los 4 Cron Jobs activos.")
    return scheduler