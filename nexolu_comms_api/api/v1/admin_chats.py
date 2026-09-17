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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import resolve_by_app_id
from nexolu_comms_api.core.auth.dependencies import get_panel_scope
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity
from nexolu_comms_api.core.channels.registry import get_channel_registry
from nexolu_comms_api.core.db.entities import ChatMessage, Contact
from nexolu_comms_api.core.db.session import get_session

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


class ChatSendIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


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
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> ChatMessageOut:
    """Responde desde la bandeja, como si el negocio escribiera desde el
    celular que no tiene: texto libre por la misma identidad de WhatsApp
    del negocio. Fuera de la ventana de 24h Meta lo rechaza y la fila
    queda `failed` a la vista (la alternativa es una plantilla)."""
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
    sender = get_channel_registry().resolve("whatsapp")
    result = await sender.send(
        identity,
        OutboundMessage(to=contact.phone, text=payload.text, category="service"),
    )

    message = ChatMessage(
        app_id=contact.app_id,
        business_id=contact.business_id,
        contact_id=contact.id,
        direction="out",
        message_type="text",
        body=payload.text,
        payload={},
        wamid=result.provider_message_id,
        status=result.status,
        origin="panel",
    )
    session.add(message)
    await session.commit()

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
