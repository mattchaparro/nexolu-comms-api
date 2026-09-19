"""El registro de la bandeja (chat_messages), compartido entre quienes envian.

Tres origenes escriben salientes en el hilo: el motor de flujos (`flow`),
el panel (`panel`) y la API de las apps (`api` - p.ej. el bot del spa
respondiendo via /v1/notifications/send). Todos pasan por aca para que la
burbuja se pinte igual venga de donde venga.

La identidad del contacto es (app, negocio, telefono), pero un mismo
telefono puede aparecer con negocios distintos segun quien escribio
primero (el webhook del numero compartido no sabe el negocio; la app si).
`resolve_contact_for_send` tolera eso: antes de crear un contacto nuevo
busca el telefono en la app, para que el hilo no se parta en dos.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.channels.base import ChannelSendResult, OutboundMessage
from nexolu_comms_api.core.db.entities import ChatMessage, Contact


def outbound_chat_type(message: OutboundMessage) -> str:
    if message.media_kind:
        return message.media_kind
    if message.template_name:
        return "template"
    if message.list_rows:
        return "list"
    if message.buttons:
        return "buttons"
    if message.cta_url:
        return "cta_url"
    if message.product_retailer_id or message.product_sections or message.send_catalog:
        return "product"
    return "text"


def outbound_chat_body(message: OutboundMessage) -> str:
    """Lo que se lee en la burbuja del hilo.

    Una plantilla no lleva texto propio -- el cuerpo real lo tiene Meta --,
    asi que se guarda lo que el negocio ENVIO: nombre y variables. Sin esto
    las plantillas salen como burbujas vacias y el hilo se vuelve ilegible
    justo donde mas importa (el mensaje con el que se reabre una
    conversacion fria).
    """
    if message.text:
        return message.text
    if message.media_caption:
        return message.media_caption
    if message.template_name:
        params = [
            str(p.get("text", ""))
            for component in message.template_components
            if str(component.get("type", "")).lower() == "body"
            for p in component.get("parameters", [])
        ]
        etiqueta = f"[plantilla {message.template_name}]"
        return f"{etiqueta} " + " · ".join(params) if params else etiqueta
    return ""


def outbound_chat_payload(message: OutboundMessage) -> dict[str, Any]:
    """Lo minimo para pintar la burbuja rica en la bandeja."""
    payload: dict[str, Any] = {}
    if message.buttons:
        payload["buttons"] = message.buttons
    if message.list_rows:
        payload["rows"] = message.list_rows
        payload["list_button"] = message.list_button
    if message.cta_url:
        payload["cta_url"] = message.cta_url
        payload["cta_title"] = message.cta_title
    if message.media_kind:
        payload["media_kind"] = message.media_kind
        payload["media_url"] = message.media_url
    if message.template_name:
        payload["template"] = message.template_name
        payload["language"] = message.template_language
    return payload


def log_outbound_chat(
    session: AsyncSession,
    *,
    contact: Contact,
    message: OutboundMessage,
    result: ChannelSendResult,
    origin: str,
) -> ChatMessage:
    row = ChatMessage(
        app_id=contact.app_id,
        business_id=contact.business_id,
        contact_id=contact.id,
        direction="out",
        message_type=outbound_chat_type(message),
        body=outbound_chat_body(message),
        payload=outbound_chat_payload(message),
        wamid=result.provider_message_id,
        status=result.status,
        origin=origin,
    )
    session.add(row)
    return row


async def resolve_contact_for_send(
    session: AsyncSession, app_id: str, business_id: str, phone: str
) -> Contact:
    """El contacto al que se le esta escribiendo, sin partir el hilo.

    Meta no usa '+' y el motor tampoco; una app que mande '+57...' debe
    caer en el mismo contacto que el webhook creo como '57...'.
    """
    phone = phone.lstrip("+")
    exact = (
        await session.execute(
            select(Contact).where(
                Contact.app_id == app_id,
                Contact.business_id == business_id,
                Contact.phone == phone,
            )
        )
    ).scalar_one_or_none()
    if exact is not None:
        return exact

    # Mismo telefono con otro negocio (o sin negocio): es la misma persona
    # y el mismo hilo - el mas recientemente tocado gana.
    same_phone = (
        await session.execute(
            select(Contact)
            .where(Contact.app_id == app_id, Contact.phone == phone)
            .order_by(Contact.updated_at.desc())
        )
    ).scalars().first()
    if same_phone is not None:
        return same_phone

    contact = Contact(app_id=app_id, business_id=business_id, phone=phone)
    session.add(contact)
    await session.flush()
    return contact
