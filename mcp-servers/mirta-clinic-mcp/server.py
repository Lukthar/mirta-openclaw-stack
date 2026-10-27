import os
import json
import secrets
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time
from typing import Optional, List, Dict, Any, Literal
from dotenv import load_dotenv

from mcp.server.fastmcp import FastMCP
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from cache_sync import ClinicCacheManager
from security_harness import (
    SecurityHarness,
    PatientBookingInput,
    format_canonical_argentina_phone,
    ARG_TZ
)

load_dotenv()

logger = logging.getLogger("mirta.mcp_server")
logging.basicConfig(level=logging.INFO)

mcp = FastMCP("mirta-clinic-mcp")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "./credentials/service_account.json")
TURNOS_SHEET_ID = os.getenv("TURNOS_SHEET_ID", "")
TURNOS_SHEET_NAME = os.getenv("TURNOS_SHEET_NAME", "Pacientes")

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")

cache_mgr = ClinicCacheManager(redis_url=REDIS_URL, credentials_path=GOOGLE_CREDS_PATH)
harness = SecurityHarness(redis_client=cache_mgr.redis)

DIAS_MAP = {
    0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves",
    4: "viernes", 5: "sabado", 6: "domingo"
}


def _get_google_services():
    cal_service = build("calendar", "v3", credentials=cache_mgr.creds)
    sheets_service = build("sheets", "v4", credentials=cache_mgr.creds)
    return cal_service, sheets_service


def _get_session_context(session_phone: str) -> Dict[str, str]:
    clean_phone = format_canonical_argentina_phone(session_phone)
    raw = cache_mgr.redis.get(f"active_turn_ctx:{clean_phone}")
    if raw:
        return json.loads(raw)
    return {"session_jid": f"{clean_phone}@s.whatsapp.net", "turn_id": "manual_invoke"}


def _send_evolution_whatsapp(target_phone_or_jid: str, text: str, delay_ms: int = 1800) -> bool:
    if not EVOLUTION_API_KEY or not EVOLUTION_API_URL or not EVOLUTION_INSTANCE:
        logger.warning("Credenciales de Evolution API incompletas en .env. Omitiendo envío de WhatsApp.")
        return False

    clean_digits = format_canonical_argentina_phone(target_phone_or_jid)
    if not clean_digits:
        return False

    jid = f"{clean_digits}@s.whatsapp.net"
    url = f"{EVOLUTION_API_URL.rstrip('/')}/message/sendText/{EVOLUTION_INSTANCE}"

    payload = json.dumps({
        "number": jid,
        "text": text,
        "presence": "composing",
        "delay": delay_ms
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "apikey": EVOLUTION_API_KEY
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        logger.error(f"Error enviando WhatsApp vía Evolution API a {jid}: {e}")
        return False


def _read_all_turnos_rows(sheets_service) -> List[Dict[str, Any]]:
    if not TURNOS_SHEET_ID:
        raise ValueError("La variable de entorno TURNOS_SHEET_ID no está configurada.")

    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=TURNOS_SHEET_ID,
        range=f"{TURNOS_SHEET_NAME}!A1:L2000"
    ).execute()
    values = res.get("values", [])
    if len(values) < 2:
        return []

    headers = [
        "Nombre", "DNI", "Mail", "Contacto", "Obra_Social", "Profesional",
        "Tratamiento", "Fecha", "Hora", "Accion", "Link Calendar", "ID"
    ]
    rows = []
    for idx, r in enumerate(values[1:], start=2):
        row_dict = {"_row_number": idx}
        for col_i, h in enumerate(headers):
            row_dict[h] = r[col_i].strip() if col_i < len(r) else ""
        rows.append(row_dict)
    return rows


def _is_slot_free_in_calendar(
    cal_service,
    id_calendar: str,
    start_dt: datetime,
    busy_end_dt: datetime,
    exclude_event_id: Optional[str] = None
) -> bool:
    events_result = cal_service.events().list(
        calendarId=id_calendar,
        timeMin=start_dt.isoformat(),
        timeMax=busy_end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    for ev in events_result.get("items", []):
        if ev.get("status") == "cancelled":
            continue
        if exclude_event_id and ev.get("id") == exclude_event_id:
            continue
        return False
    return True


@mcp.tool()
def consultar_disponibilidad(
    session_phone: str,
    tratamiento: str,
    preferencia_rango: Literal[
        "HOY_MANANA",
        "ESTA_SEMANA",
        "PROXIMA_SEMANA",
        "FECHA_PUNTUAL",
        "FECHA_HORA_EXACTA_PACIENTE"
    ],
    profesional_preferido: Optional[str] = None,
    fecha_hora_puntual_iso: Optional[str] = None
) -> str:
    """
    Busca horarios disponibles en Google Calendar cruzando la especialidad del tratamiento
    y la duración exacta (Tiempo_aprox + 15 min de ventana).
    """
    internal = cache_mgr.get_internal_data()
    cfg = internal["config"]
    cat = internal["catalog"]
    turn_ctx = _get_session_context(session_phone)

    trat_obj = next(
        (t for t in cat["tratamientos"] if t["tratamiento"].lower() == tratamiento.strip().lower()),
        None
    )
    if not trat_obj:
        trat_obj = next(
            (t for t in cat["tratamientos"] if tratamiento.strip().lower() in t["tratamiento"].lower()),
            None
        )
    if not trat_obj:
        nombres_validos = [t["tratamiento"] for t in cat["tratamientos"]]
        return json.dumps({
            "status": "error",
            "mensaje": f"El tratamiento '{tratamiento}' no figura en el catálogo. Usa uno exacto de: {nombres_validos}"
        }, ensure_ascii=False)

    especialidad_req = trat_obj["especialidad"].lower()
    duracion_min = int(trat_obj["duracion_min"])
    bloque_total_min = duracion_min + 15

    candidatos = [
        o for o in cat["odontologos"]
        if o["especialidad"].lower() == especialidad_req and o.get("id_calendar")
    ]

    if profesional_preferido:
        prof_exacto = next(
            (o for o in cat["odontologos"] if profesional_preferido.strip().lower() in o["profesional"].lower()),
            None
        )
        if prof_exacto and prof_exacto["especialidad"].lower() != especialidad_req:
            nombres_correctos = [c["profesional"] for c in candidatos]
            return json.dumps({
                "status": "incompatibilidad_profesional",
                "mensaje": (
                    f"{prof_exacto['profesional']} se dedica a {prof_exacto['especialidad']}, "
                    f"no realiza '{trat_obj['tratamiento']}'. "
                    f"Los especialistas que lo realizan son: {', '.join(nombres_correctos)}."
                )
            }, ensure_ascii=False)
        elif prof_exacto:
            candidatos = [prof_exacto]

    if not candidatos:
        return json.dumps({
            "status": "error",
            "mensaje": f"No se encontraron profesionales con calendario configurado para {trat_obj['especialidad']}."
        }, ensure_ascii=False)

    cal_service, _ = _get_google_services()
    now = datetime.now(ARG_TZ)

    if preferencia_rango == "FECHA_HORA_EXACTA_PACIENTE" and fecha_hora_puntual_iso:
        try:
            start_dt, event_end_dt, busy_end_dt, _ = harness.calculate_appointment_window(
                start_iso=fecha_hora_puntual_iso,
                tratamiento_nombre=trat_obj["tratamiento"],
                catalog_tratamientos=cat["tratamientos"]
            )
        except ValueError as e:
            return json.dumps({"status": "horario_invalido", "mensaje": str(e)}, ensure_ascii=False)

        profesionales_libres = []
        for prof in candidatos:
            if _is_slot_free_in_calendar(cal_service, prof["id_calendar"], start_dt, busy_end_dt):
                profesionales_libres.append(prof)

        if len(profesionales_libres) == 1:
            prof = profesionales_libres[0]
            token = harness.issue_slot_token(
                patient_jid=session_phone,
                turn_id=turn_ctx["turn_id"],
                profesional=prof["profesional"],
                id_calendar=prof["id_calendar"],
                tratamiento=trat_obj["tratamiento"],
                start_iso=start_dt.isoformat(),
                event_end_iso=event_end_dt.isoformat(),
                busy_end_iso=busy_end_dt.isoformat(),
                auto_bookable=True
            )
            return json.dumps({
                "status": "exact_match_single_professional",
                "auto_bookable_en_este_turno": True,
                "instruccion": (
                    "El horario exacto solicitado está libre con UN único especialista. "
                    "Si ya tenés todos los datos del paciente (Nombre, DNI, Mail, Obra Social), "
                    "podés ejecutar 'agendar_turno' ahora mismo usando este slot_token."
                ),
                "opciones": [{
                    "slot_token": token,
                    "profesional": prof["profesional"],
                    "especialidad": prof["especialidad"],
                    "tratamiento": trat_obj["tratamiento"],
                    "fecha": start_dt.strftime("%d/%m/%Y"),
                    "hora": start_dt.strftime("%H:%M"),
                    "inicio_iso": start_dt.isoformat()
                }]
            }, ensure_ascii=False)

        elif len(profesionales_libres) > 1:
            opciones = []
            for prof in profesionales_libres:
                token = harness.issue_slot_token(
                    patient_jid=session_phone,
                    turn_id=turn_ctx["turn_id"],
                    profesional=prof["profesional"],
                    id_calendar=prof["id_calendar"],
                    tratamiento=trat_obj["tratamiento"],
                    start_iso=start_dt.isoformat(),
                    event_end_iso=event_end_dt.isoformat(),
                    busy_end_iso=busy_end_dt.isoformat(),
                    auto_bookable=False
                )
                opciones.append({
                    "slot_token": token,
                    "profesional": prof["profesional"],
                    "especialidad": prof["especialidad"],
                    "fecha": start_dt.strftime("%d/%m/%Y"),
                    "hora": start_dt.strftime("%H:%M")
                })
            return json.dumps({
                "status": "multiple_professionals_available",
                "auto_bookable_en_este_turno": False,
                "instruccion": "Hay más de un especialista libre en ese horario. Ofrecele ambos al paciente y esperá a que elija.",
                "opciones": opciones
            }, ensure_ascii=False)

    if preferencia_rango == "HOY_MANANA":
        search_start = now + timedelta(minutes=30)
        search_end = now + timedelta(days=2)
    elif preferencia_rango == "PROXIMA_SEMANA":
        search_start = now + timedelta(days=7)
        search_end = now + timedelta(days=15)
    elif preferencia_rango in ("FECHA_PUNTUAL", "FECHA_HORA_EXACTA_PACIENTE") and fecha_hora_puntual_iso:
        target_dt = datetime.fromisoformat(fecha_hora_puntual_iso).replace(tzinfo=ARG_TZ)
        search_start = max(now + timedelta(minutes=30), target_dt.replace(hour=0, minute=0))
        search_end = target_dt.replace(hour=23, minute=59) + timedelta(days=2)
    else:
        search_start = now + timedelta(minutes=30)
        search_end = now + timedelta(days=7)

    atencion = cfg.get("horarios_clinica", {}).get("atencion_regular", {})
    dias_habiles = [
        d.lower().replace("é", "e").replace("á", "a")
        for d in atencion.get("dias", ["Martes", "Miércoles", "Jueves", "Viernes", "Sábado"])
    ]
    franjas_raw = atencion.get("turnos", {"mañana": ["09:00", "13:00"], "tarde": ["18:00", "21:00"]})

    opciones_encontradas = []

    for prof in candidatos:
        events_res = cal_service.events().list(
            calendarId=prof["id_calendar"],
            timeMin=search_start.isoformat(),
            timeMax=search_end.isoformat(),
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        busy_intervals = []
        for ev in events_res.get("items", []):
            if ev.get("status") == "cancelled":
                continue
            s_str = ev["start"].get("dateTime") or f"{ev['start'].get('date')}T00:00:00-03:00"
            e_str = ev["end"].get("dateTime") or f"{ev['end'].get('date')}T23:59:59-03:00"
            busy_intervals.append((datetime.fromisoformat(s_str), datetime.fromisoformat(e_str)))

        prof_slots = 0
        curr_date = search_start.date()
        while curr_date <= search_end.date() and prof_slots < 3:
            nombre_dia = DIAS_MAP[curr_date.weekday()]
            if nombre_dia in dias_habiles:
                for _, rango_horas in franjas_raw.items():
                    if len(rango_horas) < 2 or prof_slots >= 3:
                        continue
                    h_ini, m_ini = map(int, rango_horas[0].split(":"))
                    h_fin, m_fin = map(int, rango_horas[1].split(":"))

                    slot_dt = datetime.combine(curr_date, time(h_ini, m_ini), tzinfo=ARG_TZ)
                    franja_end_dt = datetime.combine(curr_date, time(h_fin, m_fin), tzinfo=ARG_TZ)

                    while slot_dt + timedelta(minutes=duracion_min) <= franja_end_dt:
                        if slot_dt > now + timedelta(minutes=15):
                            event_end_dt = slot_dt + timedelta(minutes=duracion_min)
                            busy_end_dt = slot_dt + timedelta(minutes=bloque_total_min)

                            overlap = any(
                                (slot_dt < b_end and busy_end_dt > b_start)
                                for b_start, b_end in busy_intervals
                            )
                            if not overlap:
                                token = harness.issue_slot_token(
                                    patient_jid=session_phone,
                                    turn_id=turn_ctx["turn_id"],
                                    profesional=prof["profesional"],
                                    id_calendar=prof["id_calendar"],
                                    tratamiento=trat_obj["tratamiento"],
                                    start_iso=slot_dt.isoformat(),
                                    event_end_iso=event_end_dt.isoformat(),
                                    busy_end_iso=busy_end_dt.isoformat(),
                                    auto_bookable=False
                                )
                                opciones_encontradas.append({
                                    "slot_token": token,
                                    "profesional": prof["profesional"],
                                    "especialidad": prof["especialidad"],
                                    "tratamiento": trat_obj["tratamiento"],
                                    "dia_semana": nombre_dia.capitalize(),
                                    "fecha": slot_dt.strftime("%d/%m/%Y"),
                                    "hora": slot_dt.strftime("%H:%M")
                                })
                                prof_slots += 1
                                if prof_slots >= 3:
                                    break
                                slot_dt += timedelta(minutes=90)
                                continue
                        slot_dt += timedelta(minutes=30)
            curr_date += timedelta(days=1)

    return json.dumps({
        "status": "ok" if opciones_encontradas else "sin_disponibilidad",
        "auto_bookable_en_este_turno": False,
        "instruccion": (
            "Presentale hasta 3 de estas opciones al paciente indicando el profesional y qué hace en palabras simples. "
            "PROHIBIDO llamar a 'agendar_turno' en este mismo turno. Esperá su respuesta."
        ),
        "opciones": opciones_encontradas[:5]
    }, ensure_ascii=False)


@mcp.tool()
def agendar_turno(
    session_phone: str,
    slot_token: str,
    dni: str,
    nombre: str,
    mail: str,
    obra_social: str
) -> str:
    """
    Ejecuta de forma ATÓMICA la validación del slot_token, el re-chequeo en Google Calendar,
    la creación del evento y el guardado en Google Sheets con estado 'ACTIVO'.
    """
    validated = PatientBookingInput(
        slot_token=slot_token,
        dni=dni,
        nombre=nombre,
        mail=mail,
        obra_social=obra_social
    )
    turn_ctx = _get_session_context(session_phone)
    clean_phone = format_canonical_argentina_phone(session_phone)

    slot_data = harness.consume_slot_token(
        patient_jid=clean_phone,
        token=validated.slot_token,
        current_turn_id=turn_ctx["turn_id"]
    )

    id_calendar = slot_data["id_calendar"]
    start_dt = datetime.fromisoformat(slot_data["start_iso"])
    event_end_dt = datetime.fromisoformat(slot_data["event_end_iso"])
    busy_end_dt = datetime.fromisoformat(slot_data["busy_end_iso"])
    profesional = slot_data["profesional"]
    tratamiento = slot_data["tratamiento"]

    if not harness.acquire_booking_lock(id_calendar, slot_data["start_iso"]):
        return json.dumps({
            "status": "ocupado_concurrente",
            "mensaje": "Justo otro paciente está reservando ese mismo horario. Por favor ofrecé otra alternativa."
        }, ensure_ascii=False)

    try:
        cal_service, sheets_service = _get_google_services()
        internal = cache_mgr.get_internal_data()
        direccion_clinica = (
            internal["config"].get("clinica", {}).get("ubicacion", {}).get("direccion")
            or os.getenv("DEFAULT_CLINIC_ADDRESS", "")
        )

        existing_rows = _read_all_turnos_rows(sheets_service)
        fecha_str = start_dt.strftime("%d/%m/%Y")
        hora_str = start_dt.strftime("%H:%M")

        for r in existing_rows:
            if (
                r.get("Accion") == "ACTIVO"
                and harness.verify_appointment_ownership(r, clean_phone, validated.dni)
                and r.get("Fecha") == fecha_str
                and r.get("Hora") == hora_str
            ):
                return json.dumps({
                    "status": "ya_agendado",
                    "mensaje": "El paciente ya tiene este turno registrado como ACTIVO.",
                    "link_calendar": r.get("Link Calendar", "")
                }, ensure_ascii=False)

        if not _is_slot_free_in_calendar(cal_service, id_calendar, start_dt, busy_end_dt):
            return json.dumps({
                "status": "horario_ocupado",
                "mensaje": "Al realizar el re-chequeo final en el calendario, el horario ya fue ocupado. Ofrecele otra opción."
            }, ensure_ascii=False)

        primer_nombre = validated.nombre.split()[0]
        event_body = {
            "summary": f"{validated.nombre} - {tratamiento}",
            "location": direccion_clinica,
            "description": (
                f"Hola {primer_nombre}! Recordá llegar 10 minutos antes por favor 🙏\n"
                f"Paciente: {validated.nombre} | DNI: {validated.dni} | OS: {validated.obra_social}\n"
                f"Tel: {clean_phone} | Mail: {validated.mail}\n\n"
                f"📆 [Sincronizado]"
            ),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Argentina/Buenos_Aires"},
            "end": {"dateTime": event_end_dt.isoformat(), "timeZone": "America/Argentina/Buenos_Aires"},
            "attendees": [{"email": validated.mail}],
            "transparency": "opaque",
            "visibility": "public"
        }

        created_event = cal_service.events().insert(calendarId=id_calendar, body=event_body).execute()
        event_id = created_event.get("id", "")
        html_link = created_event.get("htmlLink", "")

        new_row = [
            validated.nombre,
            validated.dni,
            validated.mail,
            clean_phone,
            validated.obra_social,
            profesional,
            tratamiento,
            fecha_str,
            hora_str,
            "ACTIVO",
            html_link,
            event_id
        ]

        sheets_service.spreadsheets().values().append(
            spreadsheetId=TURNOS_SHEET_ID,
            range=f"{TURNOS_SHEET_NAME}!A:L",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [new_row]}
        ).execute()

        return json.dumps({
            "status": "agendado_exitoso",
            "resumen": {
                "paciente": validated.nombre,
                "profesional": profesional,
                "tratamiento": tratamiento,
                "fecha": fecha_str,
                "hora": hora_str,
                "obra_social": validated.obra_social,
                "link_calendar": html_link,
                "event_id": event_id
            }
        }, ensure_ascii=False)

    finally:
        harness.release_booking_lock(id_calendar, slot_data["start_iso"])


@mcp.tool()
def reprogramar_turno(
    session_phone: str,
    slot_token_nuevo: str,
    event_id_original: Optional[str] = None,
    dni: Optional[str] = None
) -> str:
    """
    Reprograma un turno ACTIVO del paciente cumpliendo los pasos de seguridad:
    crea primero el nuevo evento, borra el original, y si el borrado falla marca REVISAR_DUPLICADO.
    """
    turn_ctx = _get_session_context(session_phone)
    clean_phone = format_canonical_argentina_phone(session_phone)

    cal_service, sheets_service = _get_google_services()
    rows = _read_all_turnos_rows(sheets_service)

    mis_turnos = [
        r for r in rows
        if r.get("Accion") == "ACTIVO" and harness.verify_appointment_ownership(r, clean_phone, dni)
    ]

    if not mis_turnos:
        return json.dumps({
            "status": "no_encontrado",
            "mensaje": "No se encontró ningún turno ACTIVO asociado a tu número de WhatsApp."
        }, ensure_ascii=False)

    if len(mis_turnos) > 1 and not event_id_original:
        opciones_actuales = [
            {"event_id": t["ID"], "profesional": t["Profesional"], "tratamiento": t["Tratamiento"], "fecha": t["Fecha"], "hora": t["Hora"]}
            for t in mis_turnos
        ]
        return json.dumps({
            "status": "multiples_turnos_activos",
            "mensaje": "El paciente tiene más de un turno activo. Preguntale cuál de estos quiere reprogramar:",
            "turnos_activos": opciones_actuales
        }, ensure_ascii=False)

    turno_orig = (
        next((t for t in mis_turnos if t["ID"] == event_id_original), None)
        if event_id_original else mis_turnos[0]
    )
    if not turno_orig:
        return json.dumps({"status": "error", "mensaje": "El ID del turno original no coincide con tus turnos activos."}, ensure_ascii=False)

    slot_data = harness.consume_slot_token(
        patient_jid=clean_phone,
        token=slot_token_nuevo,
        current_turn_id=turn_ctx["turn_id"]
    )

    new_id_calendar = slot_data["id_calendar"]
    start_dt = datetime.fromisoformat(slot_data["start_iso"])
    event_end_dt = datetime.fromisoformat(slot_data["event_end_iso"])
    busy_end_dt = datetime.fromisoformat(slot_data["busy_end_iso"])

    internal = cache_mgr.get_internal_data()
    orig_prof_obj = next(
        (o for o in internal["catalog"]["odontologos"] if o["profesional"].lower() == turno_orig["Profesional"].lower()),
        None
    )
    orig_id_calendar = orig_prof_obj["id_calendar"] if orig_prof_obj else new_id_calendar
    orig_event_id = turno_orig["ID"]

    if not _is_slot_free_in_calendar(cal_service, new_id_calendar, start_dt, busy_end_dt, exclude_event_id=orig_event_id):
        return json.dumps({
            "status": "horario_ocupado",
            "mensaje": "El nuevo horario elegido acaba de ocuparse. El turno original sigue vigente. Ofrecele otra opción."
        }, ensure_ascii=False)

    direccion_clinica = (
        internal["config"].get("clinica", {}).get("ubicacion", {}).get("direccion")
        or os.getenv("DEFAULT_CLINIC_ADDRESS", "")
    )
    primer_nombre = turno_orig["Nombre"].split()[0] if turno_orig["Nombre"] else "Paciente"
    event_body = {
        "summary": f"{turno_orig['Nombre']} - {slot_data['tratamiento']}",
        "location": direccion_clinica,
        "description": (
            f"Hola {primer_nombre}! Recordá llegar 10 minutos antes por favor 🙏\n"
            f"Turno reprogramado | Tel: {clean_phone}\n\n📆 [Sincronizado]"
        ),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Argentina/Buenos_Aires"},
        "end": {"dateTime": event_end_dt.isoformat(), "timeZone": "America/Argentina/Buenos_Aires"},
        "attendees": [{"email": turno_orig["Mail"]}] if turno_orig.get("Mail") else [],
        "transparency": "opaque",
        "visibility": "public"
    }

    try:
        new_ev = cal_service.events().insert(calendarId=new_id_calendar, body=event_body).execute()
        new_event_id = new_ev.get("id", "")
        new_html_link = new_ev.get("htmlLink", "")
    except Exception as e:
        logger.error(f"Fallo al crear nuevo turno en reprogramación: {e}")
        return json.dumps({
            "status": "error_creacion",
            "mensaje": "No se pudo crear el nuevo turno por un error temporal. Tu turno original sigue vigente sin cambios."
        }, ensure_ascii=False)

    delete_failed = False
    if orig_event_id:
        try:
            cal_service.events().delete(calendarId=orig_id_calendar, eventId=orig_event_id).execute()
        except HttpError as e:
            if e.resp.status != 404:
                delete_failed = True
                logger.error(f"Error borrando evento original {orig_event_id}: {e}")
        except Exception as e:
            delete_failed = True
            logger.error(f"Error borrando evento original {orig_event_id}: {e}")

    nueva_fecha_str = start_dt.strftime("%d/%m/%Y")
    nueva_hora_str = start_dt.strftime("%H:%M")
    row_num = turno_orig["_row_number"]

    if delete_failed:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=TURNOS_SHEET_ID,
            range=f"{TURNOS_SHEET_NAME}!J{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [["REVISAR_DUPLICADO"]]}
        ).execute()

        new_row = [
            turno_orig["Nombre"], turno_orig["DNI"], turno_orig["Mail"], clean_phone,
            turno_orig["Obra_Social"], slot_data["profesional"], slot_data["tratamiento"],
            nueva_fecha_str, nueva_hora_str, "ACTIVO", new_html_link, new_event_id
        ]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=TURNOS_SHEET_ID,
            range=f"{TURNOS_SHEET_NAME}!A:L",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [new_row]}
        ).execute()
    else:
        updated_segment = [
            slot_data["profesional"],
            slot_data["tratamiento"],
            nueva_fecha_str,
            nueva_hora_str,
            "ACTIVO",
            new_html_link,
            new_event_id
        ]
        sheets_service.spreadsheets().values().update(
            spreadsheetId=TURNOS_SHEET_ID,
            range=f"{TURNOS_SHEET_NAME}!F{row_num}:L{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [updated_segment]}
        ).execute()

    return json.dumps({
        "status": "reprogramado_exitoso",
        "resumen": {
            "profesional": slot_data["profesional"],
            "tratamiento": slot_data["tratamiento"],
            "nueva_fecha": nueva_fecha_str,
            "nueva_hora": nueva_hora_str,
            "link_calendar": new_html_link
        }
    }, ensure_ascii=False)


@mcp.tool()
def cancelar_turno(
    session_phone: str,
    event_id: Optional[str] = None,
    dni: Optional[str] = None
) -> str:
    """
    Cancela un turno ACTIVO del paciente verificando propiedad por teléfono (Contacto),
    elimina el evento en Google Calendar y actualiza el estado en Google Sheets a 'CANCELO'.
    """
    clean_phone = format_canonical_argentina_phone(session_phone)
    cal_service, sheets_service = _get_google_services()
    rows = _read_all_turnos_rows(sheets_service)

    mis_turnos = [
        r for r in rows
        if r.get("Accion") == "ACTIVO" and harness.verify_appointment_ownership(r, clean_phone, dni)
    ]

    if not mis_turnos:
        return json.dumps({
            "status": "no_encontrado",
            "mensaje": "No encontramos ningún turno activo asociado a tu número de teléfono."
        }, ensure_ascii=False)

    if len(mis_turnos) > 1 and not event_id:
        return json.dumps({
            "status": "multiples_turnos_activos",
            "mensaje": "Tenés más de un turno activo. ¿Cuál de estos querés cancelar?",
            "turnos_activos": [
                {"event_id": t["ID"], "profesional": t["Profesional"], "fecha": t["Fecha"], "hora": t["Hora"]}
                for t in mis_turnos
            ]
        }, ensure_ascii=False)

    turno = next((t for t in mis_turnos if t["ID"] == event_id), None) if event_id else mis_turnos[0]
    if not turno:
        return json.dumps({"status": "error", "mensaje": "No se encontró el turno indicado."}, ensure_ascii=False)

    internal = cache_mgr.get_internal_data()
    prof_obj = next(
        (o for o in internal["catalog"]["odontologos"] if o["profesional"].lower() == turno["Profesional"].lower()),
        None
    )

    if prof_obj and prof_obj.get("id_calendar") and turno.get("ID"):
        try:
            cal_service.events().delete(calendarId=prof_obj["id_calendar"], eventId=turno["ID"]).execute()
        except HttpError as e:
            if e.resp.status != 404:
                logger.warning(f"No se pudo borrar de Calendar el evento {turno['ID']}: {e}")

    row_num = turno["_row_number"]
    sheets_service.spreadsheets().values().update(
        spreadsheetId=TURNOS_SHEET_ID,
        range=f"{TURNOS_SHEET_NAME}!J{row_num}",
        valueInputOption="USER_ENTERED",
        body={"values": [["CANCELO"]]}
    ).execute()

    return json.dumps({
        "status": "cancelado_exitoso",
        "turno_cancelado": {
            "profesional": turno["Profesional"],
            "tratamiento": turno["Tratamiento"],
            "fecha": turno["Fecha"],
            "hora": turno["Hora"]
        }
    }, ensure_ascii=False)


@mcp.tool()
def notificar_profesional(
    session_phone: str,
    nombre_profesional: str,
    nombre_paciente: str,
    motivo_mensaje: str
) -> str:
    """
    Envía un aviso interno por WhatsApp al odontólogo cuando el paciente solicita su número personal
    o pide hablar con él, SIN exponer jamás el teléfono privado del profesional al LLM ni al paciente.
    """
    internal = cache_mgr.get_internal_data()
    contactos = internal["config"].get("profesionales_contacto", [])
    clean_patient_phone = format_canonical_argentina_phone(session_phone)

    prof_contact = next(
        (p for p in contactos if nombre_profesional.strip().lower() in p.get("nombre", "").lower()),
        None
    )

    target_whatsapp = (
        prof_contact.get("whatsapp")
        if prof_contact and prof_contact.get("whatsapp")
        else internal["config"].get("clinica", {}).get("telefono_secretaria", os.getenv("FALLBACK_SECRETARY_PHONE", ""))
    )

    texto_prearmado = urllib.parse.quote(f"Hola {nombre_paciente}! Me avisaron de la clínica que querías comunicarte conmigo.")
    wa_link = f"https://wa.me/{clean_patient_phone}?text={texto_prearmado}"

    mensaje_interno = (
        f"Hola! El paciente *{nombre_paciente}* pidió comunicarse con {nombre_profesional} 💬\n"
        f"--------------------------\n"
        f"Motivo: _\"{motivo_mensaje}\"_\n\n"
        f"Podés escribirle directo tocando este link: 👇\n{wa_link}"
    )

    enviado = _send_evolution_whatsapp(target_whatsapp, mensaje_interno)
    return json.dumps({
        "status": "notificado" if enviado else "registrado_sin_whatsapp",
        "mensaje": "Se envió el aviso interno al profesional para que se ponga en contacto con el paciente."
    }, ensure_ascii=False)


@mcp.tool()
def admin_cancelar_agenda_rango(
    sender_jid: str,
    profesional: str,
    fecha_inicio_iso: str,
    fecha_fin_iso: str,
    confirmacion_token: Optional[str] = None
) -> str:
    """
    [SOLO SECRETARÍA] Cancela todos los turnos de un profesional en un rango de fechas
    y envía el aviso de disculpas por WhatsApp a cada paciente afectado.
    Requiere confirmación en 2 pasos (Human-in-the-Loop).
    """
    internal = cache_mgr.get_internal_data()
    if not harness.is_authorized_secretary(sender_jid, internal["config"]):
        raise PermissionError("ACCESO DENEGADO: Este comando es exclusivo para el número de secretaría.")

    prof_obj = next(
        (o for o in internal["catalog"]["odontologos"] if o["profesional"].lower() == profesional.strip().lower()),
        None
    )
    if not prof_obj:
        return json.dumps({"status": "error", "mensaje": f"No se encontró al profesional '{profesional}'."}, ensure_ascii=False)

    cal_service, sheets_service = _get_google_services()
    start_dt = datetime.fromisoformat(fecha_inicio_iso).replace(tzinfo=ARG_TZ)
    end_dt = datetime.fromisoformat(fecha_fin_iso).replace(tzinfo=ARG_TZ)

    events = cal_service.events().list(
        calendarId=prof_obj["id_calendar"],
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True
    ).execute().get("items", [])

    active_events = [e for e in events if e.get("status") != "cancelled"]

    if not confirmacion_token:
        hitl_token = f"CONFIRM-{secrets.token_hex(3).upper()}"
        cache_mgr.redis.setex(
            f"admin_confirm:{hitl_token}",
            600,
            json.dumps({
                "profesional": prof_obj["profesional"],
                "start_iso": start_dt.isoformat(),
                "end_iso": end_dt.isoformat()
            })
        )
        return json.dumps({
            "status": "requiere_confirmacion",
            "turnos_afectados": len(active_events),
            "confirmacion_token": hitl_token,
            "instruccion": (
                f"Informale a la secretaria que se encontraron {len(active_events)} turnos de {prof_obj['profesional']} "
                f"entre el {start_dt.strftime('%d/%m/%Y %H:%M')} y el {end_dt.strftime('%d/%m/%Y %H:%M')}. "
                f"Preguntale si confirma la eliminación (esta acción no se puede deshacer). "
                f"Si dice que sí, volvé a llamar a esta herramienta pasando confirmacion_token='{hitl_token}'."
            )
        }, ensure_ascii=False)

    saved_confirm = cache_mgr.redis.get(f"admin_confirm:{confirmacion_token.strip().upper()}")
    if not saved_confirm:
        return json.dumps({"status": "error", "mensaje": "El código de confirmación expiró o es inválido."}, ensure_ascii=False)
    cache_mgr.redis.delete(f"admin_confirm:{confirmacion_token.strip().upper()}")

    rows = _read_all_turnos_rows(sheets_service)
    rows_by_event_id = {r["ID"]: r for r in rows if r.get("ID")}
    cancelados_count = 0

    for ev in active_events:
        ev_id = ev["id"]
        try:
            cal_service.events().delete(calendarId=prof_obj["id_calendar"], eventId=ev_id).execute()
            cancelados_count += 1
        except Exception as e:
            logger.error(f"Error borrando evento {ev_id}: {e}")
            continue

        if ev_id in rows_by_event_id:
            row_info = rows_by_event_id[ev_id]
            r_num = row_info["_row_number"]
            sheets_service.spreadsheets().values().update(
                spreadsheetId=TURNOS_SHEET_ID,
                range=f"{TURNOS_SHEET_NAME}!J{r_num}",
                valueInputOption="USER_ENTERED",
                body={"values": [["CANCELO"]]}
            ).execute()

            if row_info.get("Contacto"):
                primer_nombre = row_info["Nombre"].split()[0] if row_info.get("Nombre") else "Hola"
                aviso_paciente = (
                    f"Hola {primer_nombre} cómo andás?\n"
                    f"Quería avisarte que {prof_obj['profesional']} tuvo que cancelar el turno de "
                    f"{row_info.get('Tratamiento', 'consulta')} que tenías el {row_info['Fecha']}, a las {row_info['Hora']}.\n\n"
                    f"Disculpá las molestias. 💛\n"
                    f"Si te parece, puedo ayudarte a reprogramarlo para otra fecha que te quede cómoda. "
                    f"Te mando un abrazo y espero verte pronto. 😊"
                )
                _send_evolution_whatsapp(row_info["Contacto"], aviso_paciente, delay_ms=2400)

    return json.dumps({
        "status": "agenda_cancelada_exitosamente",
        "turnos_eliminados_y_notificados": cancelados_count
    }, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()