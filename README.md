# Nexolu Communications

Servicio centralizado de envío de WhatsApp y correo para todo el ecosistema
Nexolu (POS, Spa, EasyTickets, CRM y futuras aplicaciones). Mismo patrón que
[Nexolu IA Core](https://github.com/mattchaparro/nexolu-ia-core) y Nexolu
Payments Core: un solo servicio, cada app cliente autenticada con su propia
API key, sin ningún concepto de negocio propio (producto-agnóstico).

## La idea en una llamada

Una app que quiere avisar algo por varios canales a la vez solo indica
cuáles quiere usar en `channels`:

```bash
curl -X POST http://localhost:8010/v1/notifications/send \
  -H "Authorization: Bearer dev-pos-key" \
  -H "Content-Type: application/json" \
  -d '{
    "business_id": "42",
    "reference": "low_stock_alert:456",
    "channels": ["whatsapp", "email"],
    "to": {"whatsapp": "+573001234567", "email": "dueno@negocio.com"},
    "subject": "Alerta de inventario bajo",
    "text": "3 productos están por debajo del umbral."
  }'
```

Cada canal se procesa de forma **independiente**: si WhatsApp no está
configurado para esa app, o el correo falla, el otro canal igual se
intenta y la respuesta trae un resultado por canal:

```json
{
  "reference": "low_stock_alert:456",
  "business_id": "42",
  "results": [
    {"channel": "whatsapp", "status": "sent", "provider_message_id": "wamid.xxx", "cost_micros": null, "error": null},
    {"channel": "email", "status": "sent", "provider_message_id": "<msg@brevo>", "cost_micros": null, "error": null}
  ]
}
```

## Stack

- **Python 3.11 + FastAPI** — Swagger autogenerado en `/docs`.
- **SQLAlchemy 2.0 async + Alembic** — SQLite en desarrollo/tests (cero
  infraestructura), **MySQL** en producción (`mysql+aiomysql://...`).
- **httpx** — llamadas directas a WhatsApp Cloud API (Meta) y a la API
  transaccional de Brevo, sin SDKs de por medio.
- **pytest + pytest-httpx + ruff**.

## Arquitectura

- **Auth**: cada app cliente (POS, Spa, ...) tiene una API key. Sin sesión
  de usuario final: el header `Authorization: Bearer <api_key>` autentica la
  llamada completa. Un segundo nivel, separado, es `NEXOLU_PLATFORM_API_KEY`
  - acceso de Nexolú al gasto agregado de TODAS las apps, a los logs de
  envíos de todas las apps (`GET /v1/platform/notifications`) y a la gestión
  de apps/credenciales de proveedor (`/v1/admin/apps/*`, ver más abajo).
- **Apps y credenciales de proveedor, persistidas en BD**: `comms_apps` +
  `provider_credentials` (cifrado Fernet, `COMMS_MASTER_KEY` - ver
  `core/security/crypto.py`), gestionables vía `/v1/admin/apps/*`
  (protegido por `NEXOLU_PLATFORM_API_KEY`). `NEXOLU_APPS_JSON` sigue
  existiendo como fallback de transición mientras
  `scripts/migrate_apps_json_to_db.py` no haya corrido en un ambiente
  (ver `core/auth/apps.py`) - una vez migrados todos los ambientes, ese
  fallback y la variable se eliminan.
- **`business_id` es una clave de partición opaca, no un dato propio de
  POS**: este servicio nunca la valida contra nada suyo, solo la usa para
  agrupar reportes de uso por app (`GET /v1/usage/*`). Una app con su propio
  concepto de tenant (negocio, sede, cliente...) manda ese identificador
  ahí; una app de un solo tenant puede omitirla por completo - cae al
  `app_id` de quien llama, así toda su actividad queda bajo una sola
  partición en vez de fallar por falta de un dato que no le aplica.
- **Canales** (`core/channels/`): `ChannelSender` es el contrato común
  (`whatsapp.py`, `email.py`). Agregar un canal nuevo (SMS, push...) es
  escribir una clase que lo implemente y una línea en `registry.py` - nada
  más del servicio necesita cambiar.
- **Credenciales por app, no compartidas**: cada app tiene su propio número
  de WhatsApp Business (así el negocio le habla a sus clientes desde el
  número que ya conocen) y su propia identidad de remitente de correo. Brevo
  sí puede compartirse (una sola cuenta, remitentes distintos) - cada app
  puede traer su propia API key de Brevo si necesita una cuenta separada.
- **Persistencia** (`core/db/`): una sola tabla `notifications` - cada
  intento de envío por canal es una fila (auditoría + fuente de los
  reportes de uso/costo, agregados en el momento vía SQL, sin tabla de
  rollup aparte). Deliberadamente sin tablas de negocio: eso vive en la
  base de datos de cada app.
- **Costo**: WhatsApp se estima por categoría de plantilla (Meta cobra
  distinto por `marketing`/`utility`/`authentication`/`service`, tarifas
  configurables). Email queda con costo desconocido (`cost_micros: null`,
  no `0`): Brevo no lo informa por envío y la mayoría de planes son por
  volumen, no por mensaje.
- **Webhook entrante, uno por app (no por negocio dentro de una app)**:
  Meta registra el webhook a nivel de App/WABA, no por número de teléfono -
  así que el patrón correcto no es "un webhook por negocio", es "un webhook
  por app integradora" (`GET/POST /webhooks/whatsapp/{app_id}`). Este
  servicio **nunca interpreta** el evento entrante (texto, respuesta de un
  Flow, etc.) - eso sigue siendo lógica de cada app. Lo que hace es: (1)
  verificar que el evento vino de verdad de Meta (`X-Hub-Signature-256`,
  con el App Secret de esa app), (2) responderle 200 a Meta de inmediato, y
  (3) reenviar el payload crudo, firmado con HMAC propio
  (`X-Nexolu-Timestamp`/`X-Nexolu-Signature`, mismo esquema que ya usa
  Nexolu Payments Core), al `callback_url` que esa app registró. Cada
  evento se **persiste crudo en `webhook_events` antes del 200**: si el
  reenvío falla, un worker del propio proceso lo reintenta con backoff
  (60s → 5m → 25m → 2h → 6h) hasta entregarlo o declararlo `dead` -
  consultable y re-lanzable a mano vía `/v1/admin/webhook-events`. Con
  `enforce_meta_signature` activo en la credencial de la app, un evento
  sin firma verificable se rechaza con 401 (fallar cerrado).

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Liveness check. |
| `GET` | `/v1/channels` | Lista los canales disponibles. |
| `POST` | `/v1/notifications/send` | Envía por uno o varios canales en una sola llamada. Acepta header `Idempotency-Key`: repetir la llamada con la misma clave devuelve la respuesta original sin reenviar. |
| `POST` | `/v1/whatsapp/read-receipt` | Marca un mensaje entrante como leído + activa "escribiendo...". |
| `POST` | `/panel/auth/login` | Sesión del panel Connect: contraseña de un `panel_user` o el break-glass de env (`PANEL_EMAIL`/`PANEL_PASSWORD_HASH`). |
| `POST` | `/panel/auth/sso/exchange` | Canjea una aserción de nexolu-auth (RS256, audiencia `nexolu-connect`, verificación 100% local) por el JWT del panel. |
| `GET` | `/panel/me` | Usuario de la sesión (rol + apps). |
| `GET/POST/PATCH` | `/v1/admin/users` | (plataforma) Usuarios del panel: rol `platform` (admin Nexolú) o `client` (negocio externo atado a sus apps por membresías). |
| `GET` | `/v1/onboarding/whatsapp/config` | (app) Datos para abrir el popup de Embedded Signup (meta_app_id, login_config_id). |
| `GET` | `/v1/onboarding/whatsapp/channels/{business_id}` | (app) Estado del numero propio de un negocio (`not_connected`/`pending`/`active`/`disconnected`). |
| `POST` | `/v1/onboarding/whatsapp/complete` | (app) Lado servidor del Embedded Signup: intercambia el code, suscribe la WABA, registra el numero y guarda el `BusinessChannel`. |
| `GET`/`POST` | `/webhooks/whatsapp/platform` | Webhook de la App Meta de plataforma (numeros propios): firma obligatoria, enruta por `phone_number_id` y reenvia con `X-Nexolu-Business-Id`. |
| `GET` | `/v1/admin/business-channels` | (scope) Canales por negocio; `/{id}` detalle, `/{id}/disconnect` desconexion manual. |
| `POST` | `/v1/flows/trigger` | (app) Dispara un flujo de conversación para un teléfono con variables — el caso "agendaste una cita". |
| `GET/POST/PATCH/DELETE` | `/v1/admin/flows` | (scope) Flujos de automatización (modelo ManyChat): disparador keyword/API, nodos message/buttons/cta_url, tags y variables; la definición se valida al guardar. Ver core/flows/engine.py. |
| `GET/PATCH` | `/v1/admin/contacts` | (scope) Contactos con tags y campos (el subscriber de ManyChat), creados solos por mensajes entrantes o flujos. |
| `GET/POST` | `/v1/admin/templates` | (scope) Espejo de plantillas de Meta: listar y crear (`POST /{waba_id}/message_templates`); `/sync` reconcilia contra Meta; `DELETE /{id}` borra en Meta (¡todos los idiomas del nombre!) y en el espejo. El estado se mantiene al dia solo con el webhook `message_template_status_update`, y `/v1/notifications/send` corta ANTES de llamar a Meta si la plantilla pedida esta en el espejo y no esta APPROVED. |
| `GET` | `/v1/admin/webhook-events` | (plataforma) Lista eventos de webhook con filtros (`app_id`, `forward_status`, `event_type`). |
| `GET` | `/v1/admin/webhook-events/{id}` | (plataforma) Detalle de un evento, con su payload crudo. |
| `POST` | `/v1/admin/webhook-events/{id}/retry` | (plataforma) Re-lanza un evento `failed`/`dead`/`skipped`; `delivered` y `rejected` se rechazan (409). |
| `GET` | `/v1/usage/summary` | Gasto propio de la app (opcional: por negocio/canal). |
| `GET` | `/v1/usage/daily` | Serie diaria del gasto propio. |
| `GET` | `/v1/platform/usage` | Gasto de TODAS las apps (requiere `NEXOLU_PLATFORM_API_KEY`). |
| `GET` | `/v1/platform/notifications` | Log de envíos de TODAS las apps, filtrable y paginado (requiere `NEXOLU_PLATFORM_API_KEY`). |
| `GET` | `/webhooks/whatsapp/{app_id}` | Handshake de verificación de Meta. |
| `POST` | `/webhooks/whatsapp/{app_id}` | Recibe un evento de Meta y lo reenvía firmado al `callback_url` de esa app. |
| `GET`/`POST` | `/v1/admin/apps` | Lista/crea apps (requiere `NEXOLU_PLATFORM_API_KEY`). |
| `PATCH` | `/v1/admin/apps/{app_id}` | Edita nombre/estado de una app. |
| `POST` | `/v1/admin/apps/{app_id}/regenerate-key` | Regenera la api_key de una app (overwrite inmediato). |
| `GET`/`POST` | `/v1/admin/apps/{app_id}/providers/meta-whatsapp` | Consulta/configura credenciales de WhatsApp Cloud API. |
| `GET` | `/v1/admin/apps/{app_id}/providers/meta-whatsapp/secrets` | Revela las credenciales de WhatsApp en claro. |
| `GET`/`POST` | `/v1/admin/apps/{app_id}/providers/brevo` | Consulta/configura credenciales de Brevo. |
| `GET` | `/v1/admin/apps/{app_id}/providers/brevo/secrets` | Revela la API key de Brevo en claro. |

Contrato completo, con ejemplos: `GET /docs` (Swagger) una vez el servicio
esté corriendo.

## Desarrollo local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env  # completar NEXOLU_APPS_JSON con al menos una app de prueba y generar COMMS_MASTER_KEY

uvicorn nexolu_comms_api.main:app --reload --port 8010
```

Las tablas se crean solas al arrancar cuando `DATABASE_URL` es SQLite (ver
`main.py`). En producción con MySQL el esquema se maneja con Alembic:

```bash
alembic upgrade head
```

Para migrar las apps/credenciales que hoy viven en `NEXOLU_APPS_JSON` hacia
`comms_apps`/`provider_credentials` (idempotente, no genera api_keys
nuevas):

```bash
python scripts/migrate_apps_json_to_db.py
```

### Tests

```bash
pytest
ruff check .
```

## Autorización del panel Connect (connect.nexolu.co)

Dos sujetos con distinción dura: el **admin de Nexolú** (`role=platform`,
o la platform key server-side) tiene acceso total; un **cliente externo**
(`role=client`) es un negocio que usa Connect como producto — es una
`CommsApp` propia, y su usuario queda atado a ella por `panel_memberships`.
Todo endpoint que admite clientes se autoriza por **scope**
(`get_panel_scope`): el recorte se aplica en el servidor y lo ajeno
responde 404, como si no existiera. Crear apps, rotar api_keys y gestionar
usuarios sigue siendo solo-plataforma (`require_platform_access`, que ya
NO acepta JWTs de clientes). La identidad se resuelve contra la BD en cada
request: desactivar un usuario mata su sesión de inmediato.

## Qué falta / deliberadamente fuera de alcance en esta primera versión

- **Costo de email desconocido**: no hay tarifa por mensaje configurada
  para Brevo (ver arriba). Si en el futuro se necesita, es un campo más en
  `EmailAppConfig`/`Settings`, mismo patrón que WhatsApp.
- **Solo dos canales** (WhatsApp, email): SMS/push quedan para cuando haga
  falta - la arquitectura ya está pensada para agregarlos sin tocar el
  endpoint de envío ni las apps que ya integran.
