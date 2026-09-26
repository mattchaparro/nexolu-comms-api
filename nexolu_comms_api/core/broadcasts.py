"""Difusiones: una plantilla a un publico, ahora o a una hora.

El publico es un CRITERIO sobre los contactos de una app (y negocio), no
una lista: se evalua al enviar. Los datos con que se filtra son campos que
la app duena mantiene al dia en cada contacto (PUT /v1/contacts/bulk). Por
convencion, el spa manda:

    acepta_promociones  bool   si acepta mensajes de promociones
    ultima_visita       fecha  YYYY-MM-DD de la ultima visita cobrada
    visitas             int    visitas cobradas en total
    ultima_atencion_con str    quien la atendio la ultima vez

Reglas que no se negocian:
- Una plantilla de MARKETING solo le llega a quien acepta promociones
  (`acepta_promociones` true). No es opcional: es la Ley 1581 y es lo que
  mantiene la calidad del numero.
- La lista se congela al enviar (broadcast_recipients): el reporte dice a
  quien le salio, y un reintento no le escribe dos veces a nadie.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.db.entities import (
    Broadcast,
    BroadcastRecipient,
    ChatMessage,
    Contact,
    WhatsAppTemplate,
)
from nexolu_comms_api.core.db.session import get_sessionmaker

logger = logging.getLogger(__name__)

# Pausa entre envios: Meta acepta mucho mas, pero una difusion no tiene
# prisa y asi no compite con los mensajes de la conversacion en vivo.
SEND_PAUSE_SECONDS = 0.25
WORKER_INTERVAL_SECONDS = 30
REPLY_WINDOW_DAYS = 3

_SIN_NOMBRE = {"cliente", "clienta", "sin", "na", "nn", "hola", "no", "test", "prueba"}


def first_name(name: str) -> str:
    """El nombre de pila si sirve para saludar; "" si no ("?", ".", emojis)."""
    limpio = (name or "").strip()
    if not re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:[ '\-][A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)*", limpio):
        return ""
    primero = limpio.split(" ")[0]
    if len(primero) < 3 or primero.lower() in _SIN_NOMBRE:
        return ""
    return primero


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def matches(contact: Contact, audience: dict[str, Any], marketing: bool) -> bool:
    """Si este contacto entra en el publico."""
    fields = contact.fields or {}
    tags = set(contact.tags or [])

    if audience.get("contact_ids") and contact.id not in audience["contact_ids"]:
        return False

    if (marketing or audience.get("require_marketing_opt_in")) and fields.get("acepta_promociones") is not True:
        return False

    last = _as_date(fields.get("ultima_visita"))
    if audience.get("last_visit_from"):
        if last is None or last < _as_date(audience["last_visit_from"]):
            return False
    if audience.get("last_visit_to"):
        if last is None or last > _as_date(audience["last_visit_to"]):
            return False
    if audience.get("no_visit_since"):
        # Quien NO ha venido desde esa fecha (incluye a quien nunca vino
        # solo si lo pide `include_never`).
        since = _as_date(audience["no_visit_since"])
        if last is not None and last >= since:
            return False
        if last is None and not audience.get("include_never"):
            return False

    visits = fields.get("visitas")
    if audience.get("visits_min") is not None and (visits is None or int(visits) < int(audience["visits_min"])):
        return False
    if audience.get("visits_max") is not None and (visits is not None and int(visits) > int(audience["visits_max"])):
        return False

    if audience.get("attended_by") and fields.get("ultima_atencion_con") != audience["attended_by"]:
        return False

    if audience.get("tags_any") and not tags.intersection(audience["tags_any"]):
        return False
    if audience.get("tags_none") and tags.intersection(audience["tags_none"]):
        return False

    return True


async def template_category(session: AsyncSession, app_id: str, name: str, language: str) -> str | None:
    row = (
        await session.execute(
            select(WhatsAppTemplate).where(
                WhatsAppTemplate.app_id == app_id,
                WhatsAppTemplate.name == name,
                WhatsAppTemplate.language == language,
            )
        )
    ).scalars().first()
    return row.category if row else None


async def audience_contacts(session: AsyncSession, broadcast: Broadcast) -> list[Contact]:
    """Los contactos que hoy cumplen el criterio (uno por telefono)."""
    category = await template_category(session, broadcast.app_id, broadcast.template_name, broadcast.template_language)
    marketing = (category or "").upper() == "MARKETING"

    query = select(Contact).where(Contact.app_id == broadcast.app_id)
    if broadcast.business_id:
        query = query.where(Contact.business_id.in_([broadcast.business_id, ""]))
    contacts = (await session.execute(query.order_by(Contact.created_at))).scalars().all()

    seen: set[str] = set()
    result: list[Contact] = []
    for contact in contacts:
        if not contact.phone or contact.phone in seen:
            continue
        # Uno sin negocio ("" del numero compartido) es de este salon solo
        # si el salon lo publico como suyo (PUT /v1/contacts/bulk).
        if broadcast.business_id and contact.business_id != broadcast.business_id:
            if (contact.fields or {}).get("negocio") != broadcast.business_id:
                continue
        if matches(contact, broadcast.audience or {}, marketing):
            seen.add(contact.phone)
            result.append(contact)
    return result


def render_param(raw: str, name: str) -> str:
    """"{nombre}" -> el nombre de pila, o nada si no sirve para saludar;
    "{nombre|hermosa}" -> con respaldo. Y se limpia lo que deja el hueco
    ("Hola , vuelve"), igual que en el Spa (BroadcastService::render).

    Meta no acepta un parametro vacio: si queda vacio sale "-", y por eso
    el panel sugiere poner el saludo entero en el parametro ("Hola {nombre}")
    o un respaldo."""
    pila = first_name(name)
    text = re.sub(r"\{nombre(?:\|([^}]*))?\}", lambda m: pila or (m.group(1) or ""), str(raw))
    text = re.sub(r"\s{2,}", " ", re.sub(r"\s+,", ",", text)).strip()
    return text or "-"


def _params_for(broadcast: Broadcast, contact: Contact) -> list[str]:
    return [render_param(raw, contact.name) for raw in broadcast.template_params or []]


async def dispatch(broadcast_id: str) -> int:
    """Envia una difusion (congela la lista y manda). Devuelve cuantos salieron."""
    from nexolu_comms_api.core.auth.apps import resolve_by_app_id
    from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity
    from nexolu_comms_api.core.channels.registry import get_channel_registry
    from nexolu_comms_api.core.chats import log_outbound_chat

    async with get_sessionmaker()() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None or broadcast.status not in ("scheduled", "sending"):
            return 0

        app = await resolve_by_app_id(session, broadcast.app_id)
        if app is None or app.whatsapp is None:
            logger.warning("broadcasts.no_whatsapp", extra={"broadcast_id": broadcast_id})
            return 0

        category = await template_category(
            session, broadcast.app_id, broadcast.template_name, broadcast.template_language
        )

        if broadcast.status == "scheduled":
            # Reclamo atomico: si otro proceso (o un "cancelar" del panel)
            # llego primero, esta vuelta no hace nada.
            claimed = await session.execute(
                update(Broadcast)
                .where(Broadcast.id == broadcast.id, Broadcast.status == "scheduled")
                .values(status="sending")
            )
            if claimed.rowcount != 1:
                await session.rollback()
                return 0
            await session.refresh(broadcast)
            for contact in await audience_contacts(session, broadcast):
                session.add(BroadcastRecipient(broadcast_id=broadcast.id, contact_id=contact.id, phone=contact.phone))
            await session.commit()

        identity, _ = await resolve_whatsapp_identity(session, app, broadcast.business_id or app.app_id)
        sender = get_channel_registry().resolve("whatsapp")

        pending = (
            await session.execute(
                select(BroadcastRecipient).where(
                    BroadcastRecipient.broadcast_id == broadcast.id, BroadcastRecipient.status == "pending"
                )
            )
        ).scalars().all()

        sent = 0
        for recipient in pending:
            contact = await session.get(Contact, recipient.contact_id)
            if contact is None:
                recipient.status = "failed"
                recipient.error = "El contacto ya no existe."
                continue

            params = _params_for(broadcast, contact)
            message = OutboundMessage(
                to=contact.phone,
                category=(category or "marketing").lower(),
                template_name=broadcast.template_name,
                template_language=broadcast.template_language,
                template_components=[{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
                if params
                else [],
            )
            try:
                result = await sender.send(identity, message)
            except Exception as exc:  # una falla no detiene la difusion
                logger.exception("broadcasts.send_error")
                recipient.status = "failed"
                recipient.error = str(exc)[:300]
                await session.commit()
                continue

            recipient.status = "sent" if result.status == "sent" else "failed"
            recipient.wamid = result.provider_message_id
            recipient.error = (result.error or None) and result.error[:300]
            recipient.sent_at = datetime.utcnow()
            log_outbound_chat(session, contact=contact, message=message, result=result, origin="broadcast")
            sent += recipient.status == "sent"
            await session.commit()
            await asyncio.sleep(SEND_PAUSE_SECONDS)

        total = (
            await session.execute(
                select(func.count()).select_from(BroadcastRecipient).where(BroadcastRecipient.broadcast_id == broadcast.id)
            )
        ).scalar_one()
        broadcast.status = "sent"
        broadcast.sent_at = datetime.utcnow()
        broadcast.recipients = int(total)
        await session.commit()
        logger.info("broadcasts.sent", extra={"broadcast_id": broadcast.id, "sent": sent})
        return sent


async def dispatch_due(now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    async with get_sessionmaker()() as session:
        due = (
            await session.execute(
                select(Broadcast.id).where(
                    Broadcast.status.in_(["scheduled", "sending"]),
                    Broadcast.scheduled_at.is_not(None),
                    Broadcast.scheduled_at <= now,
                )
            )
        ).scalars().all()
    total = 0
    for broadcast_id in due:
        total += await dispatch(broadcast_id)
    return total


async def broadcast_worker_loop() -> None:
    """Task de proceso (lifespan), como los otros workers."""
    while True:
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
        try:
            await dispatch_due()
        except Exception:
            logger.exception("broadcasts.worker_error")


async def report(session: AsyncSession, broadcast: Broadcast) -> dict[str, Any]:
    """Como le fue: enviados, entregados, leidos, fallidos (con motivo),
    respuestas y que botones tocaron."""
    recipients = (
        await session.execute(select(BroadcastRecipient).where(BroadcastRecipient.broadcast_id == broadcast.id))
    ).scalars().all()
    wamids = [r.wamid for r in recipients if r.wamid]
    chats = {
        m.wamid: m
        for m in (
            await session.execute(select(ChatMessage).where(ChatMessage.wamid.in_(wamids)))
        ).scalars()
    } if wamids else {}

    counts = {"total": len(recipients), "sent": 0, "delivered": 0, "read": 0, "failed": 0, "pending": 0}
    failures: dict[str, int] = {}
    for r in recipients:
        chat = chats.get(r.wamid or "")
        state = chat.status if chat and chat.status else r.status
        if r.status == "pending":
            counts["pending"] += 1
        elif r.status == "failed" or state == "failed":
            counts["failed"] += 1
            reason = ((chat.payload or {}).get("error") if chat else None) or r.error or "Sin detalle"
            failures[reason] = failures.get(reason, 0) + 1
        elif state == "read":
            counts["read"] += 1
        elif state == "delivered":
            counts["delivered"] += 1
        else:
            counts["sent"] += 1

    replies: dict[str, int] = {}
    responders: set[str] = set()
    if broadcast.sent_at and recipients:
        from datetime import timedelta

        contact_ids = [r.contact_id for r in recipients]
        rows = (
            await session.execute(
                select(ChatMessage).where(
                    ChatMessage.contact_id.in_(contact_ids),
                    ChatMessage.direction == "in",
                    ChatMessage.created_at >= broadcast.sent_at - timedelta(minutes=5),
                    ChatMessage.created_at <= broadcast.sent_at + timedelta(days=REPLY_WINDOW_DAYS),
                )
            )
        ).scalars().all()
        for m in rows:
            responders.add(m.contact_id)
            if m.message_type in ("button", "interactive") and m.body:
                replies[m.body[:40]] = replies.get(m.body[:40], 0) + 1

    return {
        "counts": counts,
        # Entregados incluye los leidos: lo que interesa es a cuantas les llego.
        "delivered_total": counts["delivered"] + counts["read"],
        "failures": [{"reason": k, "count": v} for k, v in sorted(failures.items(), key=lambda x: -x[1])],
        "responders": len(responders),
        "buttons": [{"text": k, "count": v} for k, v in sorted(replies.items(), key=lambda x: -x[1])],
    }
