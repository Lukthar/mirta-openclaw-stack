Mirta: Asistente Clínico Autónomo en OpenClaw con Arquitectura MCP y Arneses de SeguridadStack de producción diseñado para migrar y desacoplar un flujo conversacional y transaccional de atención médica y gestión de turnos desde n8n hacia una arquitectura de agentes en código con OpenClaw, servidores MCP (Model Context Protocol) y arneses de validación deterministas.1. Visión General de la ArquitecturaEl sistema reemplaza la ejecución de grafos monolíticos de n8n por una topología desacoplada de 4 capas:[ WhatsApp / Paciente ]
         │
         ▼
[ Evolution API (Webhook) ]
         │
         ▼
┌────────────────────────────────────────────────────────────────────────┐
│  CAPA 1: GATEWAY & PRE-PROCESAMIENTO DETERMINISTA (FastAPI)            │
│  ├── Debouncer en Redis (4s) para ráfagas de mensajes                  │
│  ├── Transcripción de notas de voz con Gemini 2.5 Flash                │
│  ├── Sanitizador Anti-Prompt Injection & Role Spoofing                 │
│  └── Interruptor General de Operación (/shut-down)                     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│  CAPA 2: RUNTIME OPENCLAW (Agente con Memoria Comprimida)              │
│  ├── SOUL.md: Voseo rioplatense, empatía y guardrails médicos          │
│  ├── Ventana deslizante (últimos 3 pares de mensajes en Redis)         │
│  └── Contexto Clínico seguro (Google Docs/Sheets cacheados en RAM)     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ stdio (Protocolo MCP)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│  CAPA 3: SERVIDOR MCP TRANSACCIONAL (`mirta-clinic-mcp`)               │
│  ├── `consultar_disponibilidad`: Rangos fijos y duración por catálogo  │
│  ├── `agendar_turno`: Candado slot_token + Creación atómica Calendar/DB │
│  ├── `reprogramar_turno`: Creación prioritaria + borrado con fallback  │
│  ├── `cancelar_turno`: Verificación estricta de propiedad por JID      │
│  └── `admin_cancelar_agenda_rango`: Human-in-the-Loop para secretaría  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│  CAPA 4: INFRAESTRUCTURA Y WORKERS PROGRAMADOS (APScheduler)           │
│  ├── Google Calendar API: Fuente de verdad de agenda y bloqueos        │
│  ├── Google Sheets API: Respaldo histórico unificado (12 columnas A:L) │
│  ├── Cron 3m: Sincronización bidireccional (altas manuales y bajas)    │
│  ├── Cron 10:00: Recordatorios a 24 horas vía WhatsApp                 │
│  └── Cron 15m: Seguimiento post-consulta sin colisión de tokens        │
└────────────────────────────────────────────────────────────────────────┘
2. Arneses de Seguridad y Defensa en ProfundidadPara prevenir alucinaciones del LLM, inyecciones de prompt y accesos no autorizados, el stack implementa 5 arneses en código duro:ArnésCapaMecanismo de ProtecciónSanitizador Anti-SpoofingGatewayNeutraliza patrones de inyección (**Secretaria:**, </Historial>, <Lista_Turnos>) y caracteres de control antes de tocar el LLM.Candado de Dos Turnos (slot_token)MCP ServerObliga a que el agente consulte disponibilidad antes de reservar. El token emitido solo puede consumirse en un mensaje posterior, impidiendo reservas unilaterales del LLM.Excepción auto_bookableMCP ServerSi el paciente propuso día y hora exactos y existe un único profesional disponible, el token se emite pre-autorizado para completarse en un único turno de ejecución.Verificación de Propiedad de TurnoMCP ServerCompara los últimos 10 dígitos normalizados del remitente de WhatsApp con la columna Contacto de la planilla. Impide que un usuario cancele o modifique turnos ajenos conociendo un DNI.Anti-Formula InjectionPydanticRemueve caracteres de ejecución de comandos (=, +, -, @) en campos de texto antes de impactar filas en Google Sheets.3. Estructura del RepositorioPlaintextmirta-openclaw-stack/
├── .gitignore                          # Exclusión estricta de .env, credentials/ y cachés
├── .env.example                        # Plantilla de variables de entorno (Zero-Secrets)
├── requirements.txt                    # Dependencias unificadas de producción y testing
├── Dockerfile                          # Contenedor Python 3.12-slim con zona horaria de Argentina
├── docker-compose.yml                  # Orquestación lista para EasyPanel con Redis 7
├── gateway.py                          # Webhook Evolution API, Gemini Audio, Debounce y Endpoints
├── mcp-servers/
│   └── mirta-clinic-mcp/
│       ├── __init__.py
│       ├── cache_sync.py               # Sincronización Google Docs/Sheets -> Redis (<1ms lectura)
│       ├── security_harness.py         # Lógica determinista de validación, JIDs y tokens
│       ├── server.py                   # Servidor FastMCP con herramientas transaccionales
│       └── cron_workers.py             # Scheduler de recordatorios, bajas, altas manuales y encuestas
├── openclaw-workspace/
│   ├── openclaw.json                   # Configuración del agente y conexión al MCP vía stdio
│   ├── SOUL.md                         # Tono rioplatense, voseo, restricciones médicas y estilo
│   └── skills/
│       └── atencion-y-turnos/
│           └── SKILL.md                # Lógica conversacional E1->E3 y triaje por síntoma
└── tests/
    └── test_security_harnesses.py      # Suite de pruebas unitarias con fakeredis (7 tests)
4. Contrato de Herramientas MCP (mirta-clinic-mcp)Herramientas para Pacientesconsultar_disponibilidad:Argumentos: session_phone, tratamiento, preferencia_rango (HOY_MANANA, ESTA_SEMANA, PROXIMA_SEMANA, FECHA_PUNTUAL, FECHA_HORA_EXACTA_PACIENTE), profesional_preferido, fecha_hora_puntual_iso.Lógica: Cruza especialidad con el catálogo, extrae duración ($\text{Tiempo\_aprox} + 15\text{ min}$) y calcula huecos libres en Google Calendar. Emite un slot_token por cada opción.agendar_turno:Argumentos: session_phone, slot_token, dni, nombre, mail, obra_social.Lógica: Valida y consume el token, ejecuta candado atómico en Redis, re-chequea Google Calendar, crea el evento con invitados y persiste en la hoja Pacientes (12 columnas).reprogramar_turno:Argumentos: session_phone, slot_token_nuevo, event_id_original, dni.Lógica: Verifica propiedad, crea primero el evento nuevo en Calendar y luego elimina el original. Si el borrado falla por red, marca el original como REVISAR_DUPLICADO.cancelar_turno:Argumentos: session_phone, event_id, dni.Lógica: Verifica propiedad, elimina el evento de Calendar y actualiza el estado a CANCELO en Sheets.notificar_profesional:Argumentos: session_phone, nombre_profesional, nombre_paciente, motivo_mensaje.Lógica: Busca el WhatsApp privado del doctor en profesionales_contacto del Google Doc y le envía un aviso con link wa.me, sin exponer el número privado al LLM ni al paciente.Herramientas Administrativasadmin_cancelar_agenda_rango:Argumentos: sender_jid, profesional, fecha_inicio_iso, fecha_fin_iso, confirmacion_token.Lógica: Exige que el remitente sea la secretaria autorizada. Si no se provee token, devuelve la cantidad de turnos afectados en modo simulación (Dry-Run); al recibir el token de confirmación, elimina eventos en Calendar, actualiza Sheets a CANCELO y notifica individualmente a los pacientes por WhatsApp.5. Tareas en Segundo Plano y Cron Jobs (cron_workers.py)Sincronización Manual de Calendar (Cada 3 minutos):Días libres / Vacaciones: Si el evento contiene la palabra "Ocupado", el sistema lo ignora y no lo registra como paciente, pero Google Calendar bloquea esos horarios frente a nuevas reservas.Altas en Mostrador: Detecta eventos manuales (Nombre - Tratamiento con celular en descripción), formatea la descripción con recordatorio de 10 minutos y etiqueta 📆 [Sincronizado], guarda la fila en Sheets y envía el WhatsApp de confirmación.Bajas en Calendar: Si la secretaria borra un evento manual en Google Calendar, el worker actualiza Sheets a CANCELO y envía un mensaje ofreciendo reprogramar.Recordatorios a 24 Horas (Diario a las 10:00 hs):Filtra turnos ACTIVO del día siguiente y envía el recordatorio por WhatsApp con día y hora en lenguaje natural.Seguimiento Post-Consulta (Cada 15 minutos):Identifica turnos que terminaron en los últimos 15 minutos, los marca como INACTIVO y envía al profesional un link firmado con token único (/asistencia/{token}).Al recibir la respuesta del odontólogo:Asistió: Pasa a ASISTIO y envía al paciente la encuesta de satisfacción.Faltó: Pasa a AUSENTO y envía un mensaje ofreciendo reprogramar.Recarga de Caché (Cada 60 minutos):Sincroniza Redis con los cambios en Google Docs y Sheets sin reiniciar la aplicación.6. Esquema Unificado de Google Sheets (Pestaña Pacientes / Turnos)El sistema estandariza 12 columnas exactas (A hasta L):ColumnaCampoTipoDescripciónANombreStringNombre completo del pacienteBDNIStringDocumento numérico sanitizado (7-9 dígitos)CMailStringCorreo electrónico para la invitación de CalendarDContactoStringFormato canónico 549XXXXXXXXXX (sin @s.whatsapp.net)EObra_SocialStringObra social o "Particular"FProfesionalStringNombre exacto del odontólogoGTratamientoStringPráctica médica según catálogoHFechaStringFecha del turno (dd/MM/yyyy)IHoraStringHora del turno (HH:mm)JAccionStringACTIVO, CANCELO, ASISTIO, AUSENTO, INACTIVO, REVISAR_DUPLICADOKLink CalendarStringURL del evento (htmlLink)LIDStringIdentificador único del evento en Google Calendar7. Despliegue y Puesta en MarchaPrerrequisitosPython 3.12+ o Docker instalado.Instancia de Redis 7+.Cuenta de Servicio de Google Cloud con APIs activas: Google Calendar API, Google Sheets API, Google Docs API.Instancia operativa de Evolution API.Configuración de Credenciales de GoogleCrear una Service Account en Google Cloud Console y descargar el archivo de claves como service_account.json.Ubicar el archivo en la ruta del proyecto: credentials/service_account.json.Compartir con el correo de la cuenta de servicio (client_email):El archivo de Google Docs de configuración (CLINIC_DOC_ID) con rol Lector.El archivo de Google Sheets (TURNOS_SHEET_ID y CATALOG_SHEET_ID) con rol Editor.Cada uno de los calendarios de los odontólogos con permiso de Realizar cambios en eventos.Despliegue con Docker Compose (EasyPanel)Clonar el repositorio:Bashgit clone https://github.com/tu-usuario/mirta-openclaw-stack.git
cd mirta-openclaw-stack
Crear y completar el archivo de entorno:Bashcp .env.example .env
# Editar .env con tus claves y IDs reales
Levantar los servicios:Bashdocker compose up -d --build
Verificar los logs de inicialización:Bashdocker compose logs -f mirta-gateway
Ejecución de Pruebas AutomatizadasPara verificar el comportamiento de los arneses de seguridad y validación determinista sin tocar servicios de producción:Bashpytest tests/test_security_harnesses.py -v
8. Endpoints de la APIPOST /webhook/whatsapp: Endpoint principal configurado en Evolution API. Recibe texto y notas de voz.POST /shut-down (o POST /Shut_Down): Interruptor de activación/desactivación del bot con registro de auditoría en Sheets y ajuste de readMessages en Evolution API.Payload: {"estado_encendido": true | false}Header opcional: X-Admin-Token: <ADMIN_WEBHOOK_SECRET>GET /asistencia/{token}: Interfaz web mobile-first para que el odontólogo confirme asistencia post-consulta.POST /asistencia/{token}: Procesa la confirmación, actualiza la planilla y dispara las encuestas de satisfacción.