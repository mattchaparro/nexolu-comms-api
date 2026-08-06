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

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Liveness check. |
| `GET` | `/v1/channels` | Lista los canales disponibles. |
| `POST` | `/v1/notifications/send` | Envía por uno o varios canales en una sola llamada. |
| `GET` | `/v1/usage/summary` | Gasto propio de la app (opcional: por negocio/canal). |
| `GET` | `/v1/usage/daily` | Serie diaria del gasto propio. |
| `GET` | `/v1/platform/usage` | Gasto de TODAS las apps (requiere `NEXOLU_PLATFORM_API_KEY`). |

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
- **Costo de email desconocido**: no hay tarifa por mensaje configurada
  para Brevo (ver arriba). Si en el futuro se necesita, es un campo más en
  `EmailAppConfig`/`Settings`, mismo patrón que WhatsApp.
- **Solo dos canales** (WhatsApp, email): SMS/push quedan para cuando haga
  falta - la arquitectura ya está pensada para agregarlos sin tocar el
  endpoint de envío ni las apps que ya integran.
