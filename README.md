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

- **Auth**: cada app cliente (POS, Spa, ...) tiene una API key, registrada
  en `NEXOLU_APPS_JSON` (ver `.env.example`). Sin sesión de usuario final:
  el header `Authorization: Bearer <api_key>` autentica la llamada
  completa. Un segundo nivel, separado, es `NEXOLU_PLATFORM_API_KEY` -
  acceso de Nexolú al gasto agregado de TODAS las apps.
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
  Nexolu Payments Core), al `callback_url` que esa app registró. Sin cola
  ni reintento todavía - ver limitaciones abajo.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Liveness check. |
| `GET` | `/v1/channels` | Lista los canales disponibles. |
| `POST` | `/v1/notifications/send` | Envía por uno o varios canales en una sola llamada. |
| `POST` | `/v1/whatsapp/read-receipt` | Marca un mensaje entrante como leído + activa "escribiendo...". |
| `GET` | `/v1/usage/summary` | Gasto propio de la app (opcional: por negocio/canal). |
| `GET` | `/v1/usage/daily` | Serie diaria del gasto propio. |
| `GET` | `/v1/platform/usage` | Gasto de TODAS las apps (requiere `NEXOLU_PLATFORM_API_KEY`). |
| `GET` | `/webhooks/whatsapp/{app_id}` | Handshake de verificación de Meta. |
| `POST` | `/webhooks/whatsapp/{app_id}` | Recibe un evento de Meta y lo reenvía firmado al `callback_url` de esa app. |

Contrato completo, con ejemplos: `GET /docs` (Swagger) una vez el servicio
esté corriendo.

## Desarrollo local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env  # completar NEXOLU_APPS_JSON con al menos una app de prueba

uvicorn nexolu_comms_api.main:app --reload --port 8010
```

Las tablas se crean solas al arrancar cuando `DATABASE_URL` es SQLite (ver
`main.py`). En producción con MySQL el esquema se maneja con Alembic:

```bash
alembic upgrade head
```

### Tests

```bash
pytest
ruff check .
```

## Qué falta / deliberadamente fuera de alcance en esta primera versión

- **Sin idempotencia**: llamar `POST /v1/notifications/send` dos veces con
  la misma `reference` envía dos veces. La app llamante es responsable de
  no duplicar la llamada.
- **Sin gestión de plantillas de WhatsApp**: este servicio *envía*
  plantillas ya aprobadas en Meta, no las crea ni las administra - eso
  sigue siendo un paso manual en el dashboard de Meta por app.
- **Reenvío de webhooks sin cola ni reintento**: si el `callback_url` de
  una app no responde, el evento se pierde (queda logueado, no
  persistido). Para v1 es aceptable - agregar reintento con backoff es un
  cambio localizado en `api/webhooks.py::_forward()` el día que haga falta.
- **Costo de email desconocido**: no hay tarifa por mensaje configurada
  para Brevo (ver arriba). Si en el futuro se necesita, es un campo más en
  `EmailAppConfig`/`Settings`, mismo patrón que WhatsApp.
- **Solo dos canales** (WhatsApp, email): SMS/push quedan para cuando haga
  falta - la arquitectura ya está pensada para agregarlos sin tocar el
  endpoint de envío ni las apps que ya integran.
