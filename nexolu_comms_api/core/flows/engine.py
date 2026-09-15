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
web/app duena, no aca). Todo nodo puede ademas `add_tags`/`remove_tags`/
`set_fields` sobre el contacto. `{{...}}` interpola contra el contexto de
la sesion (variables del trigger + `contact.*`).

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

VALID_NODE_TYPES = ("message", "buttons", "cta_url")


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
        if not node.get("text"):
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


_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def interpolate(text: str, context: dict[str, Any]) -> str:
    """`{{fecha}}`, `{{contact.name}}`... contra el contexto. Lo que no
    exista se reemplaza por cadena vacia - un mensaje con un hueco es mejor
    que un `{{fecha}}` literal delante del cliente."""

    def resolve(match: re.Match[str]) -> str:
        value: Any = context
        for part in match.group(1).split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return ""
        return "" if value is None else str(value)

    return _PLACEHOLDER.sub(resolve, text)


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
            await self._send_node(node, context)

            if node["type"] == "buttons":
                # Parada: la sesion queda esperando la respuesta aqui.
                flow_session.current_node = node_id
                flow_session.status = "active"
                flow_session.updated_at = datetime.utcnow()
                await self._session.commit()
                return

            node_id = node.get("next")

        if steps >= MAX_NODES_PER_RUN:
            logger.warning("flows.max_nodes_reached", extra={"flow_id": self._flow.id})

        flow_session.current_node = None
        flow_session.status = "completed"
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

    # El flujo mas reciente gana: una sola sesion activa por contacto.
    stale = (
        await session.execute(
            select(FlowSession).where(
                FlowSession.contact_id == contact.id, FlowSession.status == "active"
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
