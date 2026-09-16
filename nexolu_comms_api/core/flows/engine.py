"""El motor de flujos de Connect: la pieza de ManyChat que orquesta la
conversacion.

## El esquema de un flujo (`Flow.definition`)

```json
{
  "start": "menu",
  "nodes": {
    "menu": {
      "type": "buttons",
      "text": "Hola {{contact.name}}, tu cita quedo para {{fecha}}.",
      "buttons": [
        {"id": "cancelacion", "title": "Cancelaciones", "next": "cancelacion"},
        {"id": "gestionar", "title": "Gestionar cita", "next": "gestionar"}
      ]
    },
    "cancelacion": {"type": "message", "text": "Puedes cancelar hasta...", "add_tags": ["pregunto_cancelacion"]},
    "gestionar": {"type": "cta_url", "text": "Gestiona tu cita:", "url": "https://agenda.nexolu.co/{{slug}}", "button": "Abrir agenda"}
  }
}
```

Tipos de nodo: `message` (texto, sigue solo por `next`), `buttons` (hasta 3
- regla de Meta - y ESPERA la respuesta: la sesion queda parada ahi),
`cta_url` (boton que abre un link - la accion de negocio real vive en la
web/app duena, no aca), `condition` (no envia nada: evalua `when` sobre el
contacto/contexto y sigue por `then` o `else`) y `delay` (no envia nada:
la sesion queda `waiting` con `resume_at` y el worker de reanudacion sigue
por `next` cuando vence). Todo nodo puede ademas `add_tags`/`remove_tags`/
`set_fields` sobre el contacto. `{{...}}` interpola contra el contexto de
la sesion (variables del trigger + `contact.*`).

`condition.when` acepta exactamente UNA de estas formas:
  {"tag": "vip"} / {"not_tag": "vip"}          - tiene / no tiene el tag
  {"field": "x", "equals": "y"}                - tambien not_equals, contains
  {"field": "x", "exists": true}               - tiene valor no vacio
`field` es una ruta del contexto (`fecha`, `contact.name`...); un nombre
simple que no exista ahi se busca en `contact.fields` (los custom fields).

`delay` lleva `minutes` (1 a 20160 = 14 dias). ManyChat-semantica: un
mensaje entrante durante el delay NO lo interrumpe (la conversacion es de
la app), pero un flujo nuevo del mismo contacto si lo reemplaza.

La clave raiz `ui` (posiciones del builder visual del panel) se guarda con
la definicion y el motor la ignora por completo.

## Las dos entradas

- `start_flow()`: lo dispara la app por API (POST /v1/flows/trigger) o un
  keyword entrante. Corre nodos encadenados hasta parar en un `buttons`
  (sesion `active`) o agotarse (sesion `completed`).
- `handle_inbound()`: cada `message` entrante del webhook pasa por aca
  DESPUES de persistirse y sin afectar el reenvio a la app duena (el motor
  es una capa adicional, no un secuestro del canal). Si el contacto tiene
  una sesion esperando en botones, la respuesta avanza el flujo; si no,
  se evaluan los keywords de los flujos activos. Un texto que no matchea
  nada no genera respuesta: esa conversacion es de la app, no del motor.

Guardas: tope de nodos por corrida (anti-loop), sesiones con mas de 24h
quietas expiran, y un flujo nuevo reemplaza (`superseded`) la sesion
anterior del contacto - el flujo mas reciente gana, como en ManyChat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity
from nexolu_comms_api.core.channels.registry import get_channel_registry
from nexolu_comms_api.core.db.entities import Contact, Flow, FlowSession, WebhookEvent
from nexolu_comms_api.core.db.repository import NotificationRepository
from nexolu_comms_api.core.db.session import get_sessionmaker

logger = logging.getLogger(__name__)

# Tope de nodos ejecutados en UNA corrida: un flujo legitimo manda 2-4
# mensajes seguidos; 20 solo se alcanza con un ciclo mal armado.
MAX_NODES_PER_RUN = 20

SESSION_TTL_HOURS = 24

VALID_NODE_TYPES = ("message", "buttons", "cta_url", "condition", "delay")

# Nodos que envian un mensaje (y por eso exigen 'text').
SENDING_NODE_TYPES = ("message", "buttons", "cta_url")

MAX_DELAY_MINUTES = 20160  # 14 dias: mas alla, es una campana, no un flujo.

_CONDITION_OPS = ("equals", "not_equals", "contains", "exists")


class FlowDefinitionError(ValueError):
    """La definicion no es ejecutable; el mensaje dice exactamente por que."""


def validate_definition(definition: dict[str, Any]) -> None:
    """Valida al guardar, no al ejecutar: un flujo roto debe rebotar en el
    panel con un mensaje claro, nunca descubrirse con un cliente en vivo."""
    nodes = definition.get("nodes")
    start = definition.get("start")
    if not isinstance(nodes, dict) or not nodes:
        raise FlowDefinitionError("La definicion necesita 'nodes' con al menos un nodo.")
    if not start or start not in nodes:
        raise FlowDefinitionError(f"'start' debe apuntar a un nodo existente (vino: {start!r}).")

    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            raise FlowDefinitionError(f"El nodo '{node_id}' no es un objeto.")
        node_type = node.get("type")
        if node_type not in VALID_NODE_TYPES:
            raise FlowDefinitionError(
                f"El nodo '{node_id}' tiene type {node_type!r}; validos: {', '.join(VALID_NODE_TYPES)}."
            )
        if node_type in SENDING_NODE_TYPES and not node.get("text"):
            raise FlowDefinitionError(f"El nodo '{node_id}' necesita 'text'.")
        if node.get("next") is not None and node["next"] not in nodes:
            raise FlowDefinitionError(f"El nodo '{node_id}' apunta a 'next' inexistente: {node['next']!r}.")

        if node_type == "buttons":
            buttons = node.get("buttons")
            if not isinstance(buttons, list) or not (1 <= len(buttons) <= 3):
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' necesita entre 1 y 3 'buttons' (regla de Meta)."
                )
            for button in buttons:
                if not (isinstance(button, dict) and button.get("id") and button.get("title")):
                    raise FlowDefinitionError(f"Un boton de '{node_id}' necesita 'id' y 'title'.")
                if button.get("next") is not None and button["next"] not in nodes:
                    raise FlowDefinitionError(
                        f"El boton '{button.get('id')}' de '{node_id}' apunta a nodo inexistente."
                    )
        if node_type == "cta_url" and not node.get("url"):
            raise FlowDefinitionError(f"El nodo '{node_id}' (cta_url) necesita 'url'.")

        if node_type == "condition":
            _validate_condition(node_id, node, nodes)

        if node_type == "delay":
            minutes = node.get("minutes")
            if not isinstance(minutes, int) or not (1 <= minutes <= MAX_DELAY_MINUTES):
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' (delay) necesita 'minutes' entero entre 1 y {MAX_DELAY_MINUTES}."
                )


def _validate_condition(node_id: str, node: dict[str, Any], nodes: dict[str, Any]) -> None:
    when = node.get("when")
    if not isinstance(when, dict) or not when:
        raise FlowDefinitionError(f"El nodo '{node_id}' (condition) necesita 'when'.")

    has_tag = "tag" in when or "not_tag" in when
    ops = [op for op in _CONDITION_OPS if op in when]
    if has_tag:
        if len(when) != 1:
            raise FlowDefinitionError(
                f"El 'when' de '{node_id}' con tag/not_tag no admite otras claves."
            )
    else:
        if not when.get("field") or len(ops) != 1:
            raise FlowDefinitionError(
                f"El 'when' de '{node_id}' necesita 'tag'/'not_tag', o 'field' con exactamente "
                f"una de: {', '.join(_CONDITION_OPS)}."
            )

    if not node.get("then") and not node.get("else"):
        raise FlowDefinitionError(
            f"El nodo '{node_id}' (condition) necesita al menos una rama 'then' o 'else'."
        )
    for branch in ("then", "else"):
        if node.get(branch) is not None and node[branch] not in nodes:
            raise FlowDefinitionError(
                f"La rama '{branch}' de '{node_id}' apunta a nodo inexistente: {node[branch]!r}."
            )


_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def resolve_context_value(path: str, context: dict[str, Any]) -> str:
    """Ruta con puntos contra el contexto (`fecha`, `contact.name`...).
    Lo inexistente resuelve a cadena vacia."""
    value: Any = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return ""
    return "" if value is None else str(value)


def interpolate(text: str, context: dict[str, Any]) -> str:
    """`{{fecha}}`, `{{contact.name}}`... contra el contexto. Lo que no
    exista se reemplaza por cadena vacia - un mensaje con un hueco es mejor
    que un `{{fecha}}` literal delante del cliente."""
    return _PLACEHOLDER.sub(lambda m: resolve_context_value(m.group(1), context), text)


def _evaluate_condition(node: dict[str, Any], context: dict[str, Any]) -> bool:
    """El `when` de un nodo condition, contra el mismo contexto que la
    interpolacion. Comparaciones de texto: sin mayusculas ni espacios en
    los bordes - 'Cali ' y 'cali' son la misma respuesta de un humano."""
    when = node.get("when") or {}
    tags = [str(t) for t in (context.get("contact") or {}).get("tags") or []]

    if "tag" in when:
        return str(when["tag"]) in tags
    if "not_tag" in when:
        return str(when["not_tag"]) not in tags

    path = str(when.get("field", ""))
    value = resolve_context_value(path, context)
    if value == "" and "." not in path:
        # Nombre simple que no esta en el contexto: es un custom field.
        value = resolve_context_value(f"contact.fields.{path}", context)

    def norm(raw: Any) -> str:
        return str(raw).strip().lower()

    if "equals" in when:
        return norm(value) == norm(when["equals"])
    if "not_equals" in when:
        return norm(value) != norm(when["not_equals"])
    if "contains" in when:
        return norm(when["contains"]) in norm(value)
    if "exists" in when:
        return (value != "") is bool(when["exists"])
    return False


class ContactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self, app_id: str, business_id: str, phone: str, name: str = ""
    ) -> Contact:
        contact = (
            await self._session.execute(
                select(Contact).where(
                    Contact.app_id == app_id,
                    Contact.business_id == business_id,
                    Contact.phone == phone,
                )
            )
        ).scalar_one_or_none()
        if contact is None:
            contact = Contact(app_id=app_id, business_id=business_id, phone=phone, name=name)
            self._session.add(contact)
        elif name and not contact.name:
            contact.name = name
        return contact

    async def list_contacts(self, app_id: str | None = None) -> list[Contact]:
        query = select(Contact).order_by(Contact.updated_at.desc())
        if app_id:
            query = query.where(Contact.app_id == app_id)
        return list((await self._session.execute(query)).scalars())


def _apply_node_effects(contact: Contact, node: dict[str, Any], context: dict[str, Any]) -> None:
    """Tags y fields: el intercambio de datos entre flujos y apps. Se
    reasignan listas/dicts completos (no mutacion in-place) para que
    SQLAlchemy detecte el cambio en columnas JSON."""
    add = [str(t) for t in node.get("add_tags", [])]
    remove = {str(t) for t in node.get("remove_tags", [])}
    if add or remove:
        contact.tags = [t for t in dict.fromkeys([*contact.tags, *add]) if t not in remove]
    sets = node.get("set_fields")
    if isinstance(sets, dict) and sets:
        contact.fields = {
            **contact.fields,
            **{str(k): interpolate(str(v), context) for k, v in sets.items()},
        }


class FlowRunner:
    """Ejecuta nodos de UN flujo para UN contacto, enviando por la misma
    identidad de WhatsApp que usaria un envio normal de ese negocio."""

    def __init__(self, session: AsyncSession, app: AppIdentity, flow: Flow, contact: Contact) -> None:
        self._session = session
        self._app = app
        self._flow = flow
        self._contact = contact

    async def run_from(self, flow_session: FlowSession, node_id: str | None) -> None:
        nodes = self._flow.definition.get("nodes", {})
        steps = 0

        while node_id is not None and steps < MAX_NODES_PER_RUN:
            node = nodes.get(node_id)
            if node is None:
                logger.warning(
                    "flows.node_missing", extra={"flow_id": self._flow.id, "node_id": node_id}
                )
                break
            steps += 1

            context = {**flow_session.context, "contact": _contact_context(self._contact)}
            _apply_node_effects(self._contact, node, context)

            if node["type"] == "condition":
                # No envia nada: solo decide la rama. El contexto se rearma
                # en la proxima vuelta, ya con los tags/fields del nodo.
                context = {**flow_session.context, "contact": _contact_context(self._contact)}
                node_id = node.get("then") if _evaluate_condition(node, context) else node.get("else")
                continue

            if node["type"] == "delay":
                next_id = node.get("next")
                if next_id is None:
                    # Un delay sin continuacion no espera nada.
                    break
                flow_session.current_node = next_id
                flow_session.status = "waiting"
                flow_session.resume_at = datetime.utcnow() + timedelta(minutes=int(node["minutes"]))
                flow_session.updated_at = datetime.utcnow()
                await self._session.commit()
                return

            await self._send_node(node, context)

            if node["type"] == "buttons":
                # Parada: la sesion queda esperando la respuesta aqui.
                flow_session.current_node = node_id
                flow_session.status = "active"
                flow_session.resume_at = None
                flow_session.updated_at = datetime.utcnow()
                await self._session.commit()
                return

            node_id = node.get("next")

        if steps >= MAX_NODES_PER_RUN:
            logger.warning("flows.max_nodes_reached", extra={"flow_id": self._flow.id})

        flow_session.current_node = None
        flow_session.status = "completed"
        flow_session.resume_at = None
        flow_session.updated_at = datetime.utcnow()
        await self._session.commit()

    async def _send_node(self, node: dict[str, Any], context: dict[str, Any]) -> None:
        text = interpolate(str(node.get("text", "")), context)
        message = OutboundMessage(
            to=self._contact.phone,
            text=text,
            # Respuestas dentro de la conversacion: categoria service (Meta
            # no la cobra dentro de la ventana de 24h).
            category="service",
            buttons=[
                {"id": str(b["id"]), "title": interpolate(str(b["title"]), context)}
                for b in node.get("buttons", [])
            ]
            if node["type"] == "buttons"
            else [],
            cta_url=interpolate(str(node["url"]), context) if node["type"] == "cta_url" else None,
            cta_title=str(node.get("button", "Abrir")) if node["type"] == "cta_url" else None,
        )

        identity, _ = await resolve_whatsapp_identity(
            self._session, self._app, self._contact.business_id or self._app.app_id
        )
        sender = get_channel_registry().resolve("whatsapp")
        result = await sender.send(identity, message)

        # Auditoria/costos: cada mensaje del motor es una Notification mas,
        # rastreable por reference=flow:<id>.
        NotificationRepository(self._session).log(
            app_id=self._app.app_id,
            business_id=self._contact.business_id or self._app.app_id,
            channel="whatsapp",
            recipient=self._contact.phone,
            status=result.status,
            reference=f"flow:{self._flow.name}",
            provider_message_id=result.provider_message_id,
            error=result.error,
            cost_micros=result.cost_micros,
        )
        if result.status != "sent":
            logger.warning(
                "flows.send_failed",
                extra={"flow_id": self._flow.id, "contact_id": self._contact.id, "error": result.error},
            )


def _contact_context(contact: Contact) -> dict[str, Any]:
    return {"name": contact.name, "phone": contact.phone, "tags": contact.tags, "fields": contact.fields}


async def start_flow(
    session: AsyncSession,
    app: AppIdentity,
    flow: Flow,
    *,
    phone: str,
    business_id: str | None = None,
    variables: dict[str, Any] | None = None,
    contact_name: str = "",
) -> FlowSession:
    contact = await ContactRepository(session).get_or_create(
        app.app_id, business_id or flow.business_id or "", phone, name=contact_name
    )
    await session.flush()

    # El flujo mas reciente gana: una sola sesion viva por contacto
    # (activa esperando botones o waiting en un delay).
    stale = (
        await session.execute(
            select(FlowSession).where(
                FlowSession.contact_id == contact.id,
                FlowSession.status.in_(["active", "waiting"]),
            )
        )
    ).scalars()
    for old in stale:
        old.status = "superseded"

    flow_session = FlowSession(
        flow_id=flow.id,
        contact_id=contact.id,
        app_id=app.app_id,
        business_id=contact.business_id,
        context=dict(variables or {}),
    )
    session.add(flow_session)
    await session.flush()

    await FlowRunner(session, app, flow, contact).run_from(
        flow_session, flow.definition.get("start")
    )
    return flow_session


async def handle_inbound_event(event_id: str) -> None:
    """Punto de entrada desde el webhook (background task). Nunca lanza."""
    try:
        await _handle_inbound_event(event_id)
    except Exception:
        logger.exception("flows.inbound_error", extra={"event_id": event_id})


async def _handle_inbound_event(event_id: str) -> None:
    async with get_sessionmaker()() as session:
        event = await session.get(WebhookEvent, event_id)
        if event is None or event.event_type != "message":
            return

        inbound = _parse_inbound(event.payload)
        if inbound is None:
            return
        phone, text, button_id, profile_name = inbound

        app = await resolve_by_app_id(session, event.app_id)
        if app is None or app.whatsapp is None:
            return

        business_id = ""
        if event.business_channel_id:
            from nexolu_comms_api.core.db.entities import BusinessChannel

            channel = await session.get(BusinessChannel, event.business_channel_id)
            if channel is not None:
                business_id = channel.business_id

        contact = await ContactRepository(session).get_or_create(
            app.app_id, business_id, phone, name=profile_name
        )
        contact.last_inbound_at = datetime.utcnow()
        await session.flush()

        # 1) ¿Hay una sesion esperando en botones? La respuesta avanza el flujo.
        active = (
            await session.execute(
                select(FlowSession).where(
                    FlowSession.contact_id == contact.id, FlowSession.status == "active"
                )
            )
        ).scalars().first()

        if active is not None and active.updated_at < datetime.utcnow() - timedelta(
            hours=SESSION_TTL_HOURS
        ):
            active.status = "expired"
            await session.commit()
            active = None

        if active is not None and active.current_node:
            flow = await session.get(Flow, active.flow_id)
            if flow is None or not flow.is_active:
                active.status = "expired"
                await session.commit()
                return
            node = flow.definition.get("nodes", {}).get(active.current_node, {})
            chosen = _match_button(node, button_id, text)
            if chosen is None:
                # Respondio otra cosa: la conversacion es de la app duena,
                # el motor no insiste. La sesion sigue esperando.
                await session.commit()
                return
            await FlowRunner(session, app, flow, contact).run_from(active, chosen.get("next"))
            return

        # 2) Sin sesion: ¿algun keyword de un flujo activo matchea?
        if not text:
            await session.commit()
            return
        normalized = text.strip().lower()
        flows = (
            await session.execute(
                select(Flow).where(
                    Flow.app_id == app.app_id,
                    Flow.business_id.in_(["", business_id]),
                    Flow.is_active.is_(True),
                    Flow.trigger_type == "keyword",
                )
            )
        ).scalars()
        for flow in flows:
            if normalized in [str(k).strip().lower() for k in flow.trigger_keywords]:
                await start_flow(
                    session, app, flow, phone=phone, business_id=business_id, contact_name=profile_name
                )
                return
        await session.commit()


def _match_button(
    node: dict[str, Any], button_id: str | None, text: str | None
) -> dict[str, Any] | None:
    for button in node.get("buttons", []):
        if button_id and str(button.get("id")) == button_id:
            return button
        # Tolerancia: el usuario escribio el titulo en vez de tocar el boton.
        if text and str(button.get("title", "")).strip().lower() == text.strip().lower():
            return button
    return None


def _parse_inbound(payload: str) -> tuple[str, str | None, str | None, str] | None:
    """@return (phone, text, button_reply_id, profile_name) o None."""
    try:
        value = json.loads(payload)["entry"][0]["changes"][0]["value"]
        message = value["messages"][0]
        phone = str(message["from"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None

    profile_name = ""
    contacts = value.get("contacts")
    if isinstance(contacts, list) and contacts:
        profile_name = str((contacts[0].get("profile") or {}).get("name") or "")

    text: str | None = None
    button_id: str | None = None
    if message.get("type") == "text":
        text = str((message.get("text") or {}).get("body") or "")
    elif message.get("type") == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or {}
        button_id = str(reply.get("id")) if reply.get("id") else None
        text = str(reply.get("title")) if reply.get("title") else None
    elif message.get("type") == "button":
        # Boton de plantilla (quick reply de template).
        reply = message.get("button") or {}
        text = str(reply.get("text")) if reply.get("text") else None
        button_id = str(reply.get("payload")) if reply.get("payload") else None

    return phone, text, button_id, profile_name


async def resume_due_sessions() -> int:
    """Retoma las sesiones `waiting` con el delay vencido. Cada sesion se
    procesa aislada: una que falle no bloquea a las demas.
    @return cuantas se intentaron."""
    now = datetime.utcnow()
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(FlowSession.id)
            .where(FlowSession.status == "waiting", FlowSession.resume_at <= now)
            .order_by(FlowSession.resume_at)
            .limit(50)
        )
        due = [row[0] for row in result]

    for session_id in due:
        try:
            await _resume_session(session_id)
        except Exception:
            logger.exception("flows.resume_error", extra={"session_id": session_id})
            # Marcarla expirada evita reintentarla en caliente cada tick;
            # el detalle ya quedo en el log.
            async with get_sessionmaker()() as session:
                broken = await session.get(FlowSession, session_id)
                if broken is not None and broken.status == "waiting":
                    broken.status = "expired"
                    await session.commit()

    return len(due)


async def _resume_session(session_id: str) -> None:
    async with get_sessionmaker()() as session:
        flow_session = await session.get(FlowSession, session_id)
        if flow_session is None or flow_session.status != "waiting":
            return

        flow = await session.get(Flow, flow_session.flow_id)
        contact = await session.get(Contact, flow_session.contact_id)
        app = await resolve_by_app_id(session, flow_session.app_id)
        if flow is None or not flow.is_active or contact is None or app is None:
            flow_session.status = "expired"
            flow_session.resume_at = None
            await session.commit()
            return

        node_id = flow_session.current_node
        flow_session.resume_at = None
        await FlowRunner(session, app, flow, contact).run_from(flow_session, node_id)


async def flow_resume_worker_loop() -> None:
    """Task de proceso (arrancada en el lifespan), calcada del worker de
    reintento de webhooks: duerme, retoma lo vencido, y nunca muere en
    silencio."""
    from nexolu_comms_api.config import get_settings

    settings = get_settings()
    while True:
        await asyncio.sleep(settings.flow_resume_interval_seconds)
        try:
            await resume_due_sessions()
        except Exception:
            logger.exception("flows.resume_worker_error")
