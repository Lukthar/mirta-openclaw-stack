import os
import json
import base64
import asyncio
import logging
import urllib.request
from datetime import datetime
from typing import Dict, Any, List
from dotenv import load_dotenv
from pydantic import BaseModel
from fastapi import FastAPI, Request, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from google import genai
from google.genai import types

from cache_sync import ClinicCacheManager
from security_harness import (
    sanitize_user_input,
    format_whatsapp_output,
    format_canonical_argentina_phone,
    ARG_TZ
)
from server import (
    mcp,
    _get_google_services,
    _read_all_turnos_rows,
    _send_evolution_whatsapp,
    TURNOS_SHEET_ID,
    TURNOS_SHEET_NAME
)
from cron_workers import start_scheduler, _primer_nombre

load_dotenv()

logger = logging.getLogger("mirta.gateway")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Mirta OpenClaw Gateway & Security Harness")

# Exponer el servidor MCP por red interna HTTP/SSE en /mcp/sse
app.mount("/mcp", mcp.sse_app())

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "./credentials/service_account.json")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENCLAW_API_URL = os.getenv("OPENCLAW_API_URL", "http://localhost:18789/v1/chat/completions")
OPENCLAW_API_KEY = os.getenv("OPENCLAW_API_KEY", "")
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "4.0"))

AUDIT_SHEET_ID = os.getenv("AUDIT_SHEET_ID", "")
AUDIT_SHEET_NAME = os.getenv("AUDIT_SHEET_NAME", "Sheet1")
ADMIN_WEBHOOK_SECRET = os.getenv("ADMIN_WEBHOOK_SECRET", "")
SURVEY_FORM_URL = os.getenv("SURVEY_FORM_URL", "")

cache_mgr = ClinicCacheManager(redis_url=REDIS_URL, credentials_path=GOOGLE_CREDS_PATH)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


class BotTogglePayload(BaseModel):
    estado_encendido: bool


@app.on_event("startup")
def on_startup():
    start_scheduler()


def _transcribe_whatsapp_audio(server_url: str, instance: str, apikey: str, msg_id: str) -> str:
    if not gemini_client or not server_url or not instance or not apikey:
        return ""
    url = f"{server_url.rstrip('/')}/chat/getBase64FromMediaMessage/{instance}"
    payload = json.dumps({"message": {"key": {"id": msg_id}}, "convertToMP4": False}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json", "apikey": apikey}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            media_data = json.loads(resp.read().decode("utf-8"))
        b64_str = media_data.get("base64", "")
        mime_type = media_data.get("mimetype", "audio/ogg")
        if not b64_str:
            return ""

        audio_bytes = base64.b64decode(b64_str)
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                "Transcribe exactamente este mensaje de voz de un paciente en español rioplatense. Devuelve solo la transcripción."
            ]
        )
        return response.text.strip() if response.text else ""
    except Exception as e:
        logger.error(f"Error transcribiendo audio {msg_id}: {e}")
        return ""


def _get_patient_active_appointments(clean_phone: str) -> List[Dict[str, str]]:
    try:
        _, sheets_service = _get_google_services()
        rows = _read_all_turnos_rows(sheets_service)
        mis_turnos = []
        for r in rows:
            row_digits = format_canonical_argentina_phone(r.get("Contacto", ""))
            if row_digits and len(clean_phone) >= 10 and row_digits[-10:] == clean_phone[-10:] and r.get("Accion") == "ACTIVO":
                mis_turnos.append({
                    "event_id": r["ID"],
                    "nombre": r["Nombre"],
                    "dni": r["DNI"],
                    "mail": r["Mail"],
                    "obra_social": r["Obra_Social"],
                    "profesional": r["Profesional"],
                    "tratamiento": r["Tratamiento"],
                    "fecha": r["Fecha"],
                    "hora": r["Hora"]
                })
        return mis_turnos
    except Exception as e:
        logger.error(f"Error buscando turnos del paciente {clean_phone}: {e}")
        return []


context_header = (
        f"<Contexto_Sistema>\n"
        f"- Fecha_Hora_Actual: {now_str} (America/Argentina/Buenos_Aires)\n"
        f"- Session_Phone (usar en tools MCP): {clean_phone}\n"
        f"- Nombre_WhatsApp: {push_name}\n"
        f"- Rol_Usuario: {'SECRETARIA_ADMIN' if is_secretary else 'PACIENTE'}\n"
        f"</Contexto_Sistema>\n\n"
        f"<Contexto_Clinico>\n{json.dumps(safe_ctx, ensure_ascii=False)}\n</Contexto_Clinico>\n\n"
        f"<Turnos_Activos_Paciente>\n{json.dumps(mis_turnos, ensure_ascii=False)}\n</Turnos_Activos_Paciente>"
    )

    messages = [{"role": "system", "content": context_header}]
    messages.extend(sliding_window[-6:])
    messages.append({"role": "user", "content": user_text})

    payload = json.dumps({
        "model": "openclaw:main",
        "messages": messages
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "x-openclaw-session-key": f"whatsapp:{clean_phone}"
    }
    if OPENCLAW_API_KEY:
        headers["Authorization"] = f"Bearer {OPENCLAW_API_KEY}"

    req = urllib.request.Request(OPENCLAW_API_URL, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            reply_text = data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        logger.error(f"Detalle error OpenClaw ({e.code}): {err_body}")
        raise

    sliding_window.append({"role": "user", "content": user_text})
    sliding_window.append({"role": "assistant", "content": reply_text.replace("[ENVIAR_MAPA]", "").strip()})
    cache_mgr.redis.setex(history_key, 7200, json.dumps(sliding_window[-8:], ensure_ascii=False))

    return reply_text


def _set_evolution_read_messages(read_messages: bool) -> bool:
    evo_url = os.getenv("EVOLUTION_API_URL", "")
    evo_instance = os.getenv("EVOLUTION_INSTANCE", "")
    evo_key = os.getenv("EVOLUTION_API_KEY", "")

    if not evo_key or not evo_url or not evo_instance:
        logger.warning("Variables de Evolution API incompletas: no se pudo cambiar readMessages.")
        return False

    url = f"{evo_url.rstrip('/')}/settings/set/{evo_instance}"
    payload = json.dumps({
        "rejectCall": False,
        "groupsIgnore": False,
        "alwaysOnline": False,
        "readMessages": bool(read_messages),
        "readStatus": False,
        "syncFullHistory": False
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "apikey": evo_key},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        logger.error(f"Error configurando readMessages={read_messages} en Evolution API: {e}")
        return False


def _log_toggle_to_audit_sheet(accion: str) -> None:
    if not AUDIT_SHEET_ID:
        return
    try:
        _, sheets_service = _get_google_services()
        now = datetime.now(ARG_TZ)
        fila = [
            accion,
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M"),
            "Mirta OpenClaw"
        ]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=AUDIT_SHEET_ID,
            range=f"{AUDIT_SHEET_NAME}!A:D",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [fila]}
        ).execute()
    except Exception as e:
        logger.error(f"Error guardando auditoría de {accion} en Google Sheets: {e}")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    body = await request.json()
    data = body.get("data", {})
    key_info = data.get("key", {})

    remote_jid = key_info.get("remoteJid", "")
    remote_jid_alt = key_info.get("remoteJidAlt") or remote_jid
    from_me = bool(key_info.get("fromMe", False))
    msg_id = key_info.get("id", "")
    push_name = data.get("pushName", "Paciente")
    msg_type = data.get("messageType", "")

    server_url = body.get("server_url") or os.getenv("EVOLUTION_API_URL", "")
    instance = body.get("instance") or os.getenv("EVOLUTION_INSTANCE", "")
    apikey = body.get("apikey") or os.getenv("EVOLUTION_API_KEY", "")

    internal_cfg = cache_mgr.get_internal_data()["config"]
    sec_jid = (
        internal_cfg.get("clinica", {}).get("telefono_secretaria")
        or os.getenv("FALLBACK_SECRETARY_PHONE", "")
    )
    is_secretary = from_me and bool(
        sec_jid and format_canonical_argentina_phone(remote_jid_alt) == format_canonical_argentina_phone(sec_jid)
    )

    if from_me and not is_secretary:
        return JSONResponse({"status": "ignored_from_me"})

    bot_off = cache_mgr.redis.get("clinic:bot_disabled")
    if bot_off == "1" and not is_secretary:
        return JSONResponse({"status": "bot_disabled"})

    raw_content = ""
    if msg_type == "conversation":
        raw_content = data.get("message", {}).get("conversation", "")
    elif msg_type == "extendedTextMessage":
        raw_content = data.get("message", {}).get("extendedTextMessage", {}).get("text", "")
    elif msg_type == "audioMessage":
        transcribed = await asyncio.to_thread(_transcribe_whatsapp_audio, server_url, instance, apikey, msg_id)
        if transcribed:
            raw_content = f"[Nota de voz transcrita]: {transcribed}"

    clean_text = sanitize_user_input(raw_content)
    if not clean_text or not msg_id:
        return JSONResponse({"status": "empty_or_invalid"})

    clean_phone = format_canonical_argentina_phone(remote_jid_alt)
    buffer_key = f"buffer:{clean_phone}"
    last_id_key = f"buffer_last_id:{clean_phone}"

    cache_mgr.redis.rpush(buffer_key, clean_text)
    cache_mgr.redis.setex(last_id_key, 60, msg_id)
    cache_mgr.redis.expire(buffer_key, 60)

    await asyncio.sleep(DEBOUNCE_SECONDS)

    if cache_mgr.redis.get(last_id_key) != msg_id:
        return JSONResponse({"status": "debounced_merged"})

    all_fragments = cache_mgr.redis.lrange(buffer_key, 0, -1)
    cache_mgr.redis.delete(buffer_key)
    joined_message = "\n".join(all_fragments)

    cache_mgr.redis.setex(
        f"active_turn_ctx:{clean_phone}",
        300,
        json.dumps({"session_jid": remote_jid, "turn_id": msg_id})
    )

    raw_reply = await asyncio.to_thread(
        _invoke_openclaw_agent,
        clean_phone,
        push_name,
        joined_message,
        is_secretary
    )

    wants_map = "[ENVIAR_MAPA]" in raw_reply
    clean_reply = raw_reply.replace("[ENVIAR_MAPA]", "").strip()

    bubbles = format_whatsapp_output(clean_reply, max_bubbles=3)
    for b in bubbles:
        await asyncio.to_thread(_send_evolution_whatsapp, remote_jid, b["text"], b["delay"])
        await asyncio.sleep(1.0)

    if wants_map:
        await asyncio.sleep(3.5)
        maps_link = (
            internal_cfg.get("clinica", {}).get("ubicacion", {}).get("maps_link")
            or os.getenv("DEFAULT_MAPS_LINK", "")
        )
        if maps_link:
            await asyncio.to_thread(_send_evolution_whatsapp, remote_jid, maps_link, len(maps_link) * 26)

    return JSONResponse({"status": "processed", "bubbles_sent": len(bubbles)})


@app.post("/shut-down")
@app.post("/Shut_Down")
async def toggle_chatbot_status(
    payload: BotTogglePayload,
    x_admin_token: str = Header(default=None)
):
    if ADMIN_WEBHOOK_SECRET and x_admin_token != ADMIN_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Token administrativo inválido.")

    encender = payload.estado_encendido
    internal_cfg = cache_mgr.get_internal_data()["config"]
    admin_phone = (
        internal_cfg.get("clinica", {}).get("telefono_admin")
        or os.getenv("FALLBACK_ADMIN_PHONE", "")
    )

    if encender:
        cache_mgr.redis.set("clinic:bot_disabled", "0")
        await asyncio.to_thread(_set_evolution_read_messages, True)
        msg_on = (
            "Hola jefe, volví de las vacaciones y estoy listo para seguir respondiendo 🏖\n\n"
            "✅ *Chatbot activado con éxito* ✅"
        )
        if admin_phone:
            await asyncio.to_thread(_send_evolution_whatsapp, admin_phone, msg_on, 1200)
        await asyncio.to_thread(_log_toggle_to_audit_sheet, "ENCENDIO")
        return JSONResponse({"status": "enabled", "estado_encendido": True})
    else:
        cache_mgr.redis.set("clinic:bot_disabled", "1")
        await asyncio.to_thread(_set_evolution_read_messages, False)
        msg_off = (
            "Hola jefe, me acabás de desactivar. Tené en cuenta que ahora deberás responder los mensajes a mano 📝\n\n"
            "❌ *El chatbot se ha desactivado* ❌"
        )
        if admin_phone:
            await asyncio.to_thread(_send_evolution_whatsapp, admin_phone, msg_off, 1200)
        await asyncio.to_thread(_log_toggle_to_audit_sheet, "APAGO")
        return JSONResponse({"status": "disabled", "estado_encendido": False})


@app.get("/asistencia/{token}", response_class=HTMLResponse)
async def mostrar_formulario_asistencia(token: str):
    raw = cache_mgr.redis.get(f"post_visit_token:{token}")
    if not raw:
        return HTMLResponse("<h3>Este link ya fue respondido o expiró. ¡Gracias!</h3>", status_code=404)
    turno = json.loads(raw)
    return f"""
    <html>
      <head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
      <body style="font-family: sans-serif; max-width: 420px; margin: 40px auto; padding: 20px; border: 1px solid #ddd; border-radius: 12px;">
        <h3>¿El paciente asistió al turno?</h3>
        <p><b>Paciente:</b> {turno.get('Nombre')}<br><b>Tratamiento:</b> {turno.get('Tratamiento')}<br><b>Hora:</b> {turno.get('Hora')}</p>
        <form method="post" action="/asistencia/{token}">
          <button type="submit" name="estado" value="ASISTIO" style="width:100%; padding:14px; margin-bottom:12px; font-size:16px; background:#e6f4ea; border:1px solid #34a853; border-radius:8px; cursor:pointer;">
            ✅ Sí, vino al turno 💪😎
          </button>
          <button type="submit" name="estado" value="AUSENTO" style="width:100%; padding:14px; font-size:16px; background:#fce8e6; border:1px solid #ea4335; border-radius:8px; cursor:pointer;">
            ❌ No, faltó al turno 👊😤
          </button>
        </form>
      </body>
    </html>
    """


@app.post("/asistencia/{token}", response_class=HTMLResponse)
async def procesar_formulario_asistencia(token: str, estado: str = Form(...)):
    raw = cache_mgr.redis.get(f"post_visit_token:{token}")
    if not raw:
        return HTMLResponse("<h3>Este registro ya fue procesado.</h3>")

    cache_mgr.redis.delete(f"post_visit_token:{token}")
    turno = json.loads(raw)
    _, sheets_service = _get_google_services()

    r_num = turno["_row_number"]
    nuevo_estado = "ASISTIO" if estado == "ASISTIO" else "AUSENTO"
    sheets_service.spreadsheets().values().update(
        spreadsheetId=TURNOS_SHEET_ID,
        range=f"{TURNOS_SHEET_NAME}!J{r_num}",
        valueInputOption="USER_ENTERED",
        body={"values": [[nuevo_estado]]}
    ).execute()

    contacto = turno.get("Contacto", "")
    p_nombre = _primer_nombre(turno.get("Nombre", ""))

    if contacto:
        if nuevo_estado == "ASISTIO":
            msg = (
                f"Hola {p_nombre} tenés un minutito para contarnos qué te pareció la atención?\n"
                f"Nos ayudaría mucho que puedas completar la encuesta, porque nos sirve para saber cómo fue tu experiencia y seguir mejorando 💛\n\n"
                f"👇 Cuando tengas un ratito, podés responderla acá:\n{SURVEY_FORM_URL}\n"
                f"Es cortita, no te lleva más de 2 minutos..."
            )
        else:
            msg = (
                f"Hola {p_nombre}, todo bien? 😊\n"
                f"Vi que hoy no pudiste venir. No hay problema 💛 Si querés, podemos reagendar tu turno para el día y horario que te quede más cómodo.\n\n"
                f"Cuando quieras, lo coordinamos 🙌"
            )
        await asyncio.to_thread(_send_evolution_whatsapp, contacto, msg, 2000)

    return HTMLResponse("<h3>¡Gracias! Se actualizó la planilla y se envió el seguimiento al paciente.</h3>")
