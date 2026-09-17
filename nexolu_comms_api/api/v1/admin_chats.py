"""La bandeja / live chat de Connect (la de ManyChat).

Criticidad operativa: el numero del negocio en Cloud API no tiene app
movil ni SIM - esta bandeja web es la UNICA forma humana de leer la
conversacion y responder. El historial lo alimentan el webhook (todo
entrante), el motor de flujos y este mismo panel al responder.

La ventana de 24h manda: `window_open` avisa al front si un texto libre
entregaria; fuera de ventana el envio igual se intenta y Meta decide
(la fila queda con status failed, visible en el hilo).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import resolve_by_app_id
from nexolu_comms_api.core.auth.dependencies import get_panel_scope
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity
from nexolu_comms_api.core.channels.registry import get_channel_registry
from nexolu_comms_api.core.chats import log_outbound_chat
from nexolu_comms_api.core.db.entities import ChatMessage, Contact
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.templates.service import TemplateRepository
from nexolu_comms_api.core.webhooks.app_events import post_app_event

router = APIRouter(prefix="/v1/admin/chats", tags=["admin-chats"])

WINDOW_HOURS = 24


class ConversationOut(BaseModel):
    contact_id: str
    app_id: str
    business_id: str
    phone: str
    name: str
    last_body: str
    last_direction: str
    last_at: datetime
    window_open: bool


class ChatMessageOut(BaseModel):
    id: str
    direction: str
    message_type: str
    body: str
    payload: dict[str, Any]
    status: str
    origin: str
    created_at: datetime


class ChatTemplateIn(BaseModel):
    """La plantilla aprobada con la que se REABRE una conversacion fria.
    `params` es un texto por cada {{1}}, {{2}}... del cuerpo."""

    name: str = Field(min_length=1, max_length=191)
    language: str = Field(default="es", min_length=2, max_length=16)
    params: list[str] = Field(default_factory=list, max_length=20)


class ChatSendIn(BaseModel):
    """O texto libre (dentro de la ventana de 24h) o una plantilla (la unica
    forma de entregar fuera de ella). Nunca las dos."""

    text: str | None = Field(default=None, min_length=1, max_length=4096)
    template: ChatTemplateIn | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ChatSendIn:
        if (self.text is None) == (self.template is None):
            raise ValueError("Manda 'text' o 'template', no ambos ni ninguno.")
        return self


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationOut]:
    # El ultimo mensaje de cada contacto, en una pasada: max(created_at)
    # por contacto y luego las filas de esos maximos.
    last = (
        select(
            ChatMessage.contact_id,
            func.max(ChatMessage.created_at).label("last_at"),
        )
        .group_by(ChatMessage.contact_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ChatMessage, Contact)
            .join(last, (ChatMessage.contact_id == last.c.contact_id) & (ChatMessage.created_at == last.c.last_at))
            .join(Contact, Contact.id == ChatMessage.contact_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(200)
        )
    ).all()

    threshold = datetime.utcnow() - timedelta(hours=WINDOW_HOURS)
    out: list[ConversationOut] = []
    seen: set[str] = set()
    for message, contact in rows:
        if not scope.allows(contact.app_id):
            continue
        if app_id and contact.app_id != app_id:
            continue
        if contact.id in seen:  # empate exacto de created_at
            continue
        seen.add(contact.id)
        out.append(
            ConversationOut(
                contact_id=contact.id,
                app_id=contact.app_id,
                business_id=contact.business_id,
                phone=contact.phone,
                name=contact.name,
                last_body=message.body[:120],
                last_direction=message.direction,
                last_at=message.created_at,
                window_open=bool(contact.last_inbound_at and contact.last_inbound_at > threshold),
            )
        )
    return out


async def _contact_in_scope(
    session: AsyncSession, contact_id: str, scope: PanelScope
) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None or not scope.allows(contact.app_id):
        # 404 tambien para lo ajeno: no filtrar existencia entre clientes.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversacion no encontrada.")
    return contact


@router.get("/{contact_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(
    contact_id: str,
    limit: int = 100,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> list[ChatMessageOut]:
    contact = await _contact_in_scope(session, contact_id, scope)
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.contact_id == contact.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(min(limit, 300))
        )
    ).scalars().all()
    return [
        ChatMessageOut(
            id=m.id,
            direction=m.direction,
            message_type=m.message_type,
            body=m.body,
            payload=m.payload,
            status=m.status,
            origin=m.origin,
            created_at=m.created_at,
        )
        for m in reversed(rows)
    ]


@router.post(
    "/{contact_id}/messages", response_model=ChatMessageOut, status_code=status.HTTP_201_CREATED
)
async def send_message(
    contact_id: str,
    payload: ChatSendIn,
    background_tasks: BackgroundTasks,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> ChatMessageOut:
    """Responde desde la bandeja, como si el negocio escribiera desde el
    celular que no tiene.

    Dos formas, segun la ventana de 24h: `text` (libre, solo entrega
    dentro de la ventana) o `template` (plantilla aprobada - lo UNICO que
    entrega fuera, y la razon por la que el panel puede rescatar una
    conversacion fria sin depender de un flujo).

    Al terminar avisa a la app duena (`human_reply`) para que SU bot se
    calle: el saliente del panel sale por Cloud API y la app no se
    enteraria de otra forma."""
    contact = await _contact_in_scope(session, contact_id, scope)

    app = await resolve_by_app_id(session, contact.app_id)
    if app is None or app.whatsapp is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"La app '{contact.app_id}' no tiene WhatsApp configurado.",
        )

    identity, _ = await resolve_whatsapp_identity(
        session, app, contact.business_id or contact.app_id
    )

    if payload.template is not None:
        await _assert_template_sendable(session, contact.app_id, identity, payload.template)
        message_out = OutboundMessage(
            to=contact.phone,
            # Reabrir la conversacion se cobra: es utility, no service.
            category="utility",
            template_name=payload.template.name,
            template_language=payload.template.language,
            template_components=[
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in payload.template.params],
                }
            ]
            if payload.template.params
            else [],
        )
    else:
        message_out = OutboundMessage(to=contact.phone, text=payload.text, category="service")

    sender = get_channel_registry().resolve("whatsapp")
    result = await sender.send(identity, message_out)

    message = log_outbound_chat(
        session, contact=contact, message=message_out, result=result, origin="panel"
    )
    if payload.template is not None:
        # El cuerpo real lo tiene Meta; en el hilo se guarda lo que el
        # operador vio al enviar (nombre + variables), que es lo que
        # permite entender la conversacion despues.
        message.body = _template_preview(payload.template)
    await session.commit()

    background_tasks.add_task(
        post_app_event,
        app.whatsapp,
        "human_reply",
        {
            "business_id": contact.business_id,
            "contact": {
                "name": contact.name,
                "phone": contact.phone,
                "tags": contact.tags,
                "fields": contact.fields,
            },
            "message": {
                "type": "template" if payload.template else "text",
                "text": message.body,
                "template": payload.template.name if payload.template else None,
            },
            "status": result.status,
        },
    )

    return ChatMessageOut(
        id=message.id,
        direction=message.direction,
        message_type=message.message_type,
        body=message.body,
        payload=message.payload,
        status=message.status,
        origin=message.origin,
        created_at=message.created_at,
    )


def _template_preview(template: ChatTemplateIn) -> str:
    if not template.params:
        return f"[plantilla {template.name}]"
    return f"[plantilla {template.name}] " + " · ".join(template.params)


async def _assert_template_sendable(
    session: AsyncSession, app_id: str, identity, template: ChatTemplateIn
) -> None:
    """Corta antes de llamar a Meta si el espejo sabe que la plantilla no
    esta aprobada: un rechazo seguro no vale gastar rate limit, y el
    operador merece el motivo en pantalla. Si el espejo no la conoce se
    envia igual (el espejo es opt-in, no un registro obligatorio)."""
    waba_id = identity.whatsapp.waba_id if identity.whatsapp else None
    row = await TemplateRepository(session).get_for_send(
        app_id, waba_id, template.name, template.language
    )
    if row is None or row.status == "APPROVED":
        return
    detail = f" ({row.reason})" if row.reason else ""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"La plantilla '{row.name}' ({row.language}) esta en estado {row.status}{detail} - "
            "Meta la rechazaria. Usa otra o corrigela en Plantillas."
        ),
    )
