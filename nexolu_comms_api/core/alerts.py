"""Avisos de bandeja: "hay gente esperando y nadie ha contestado".

El problema que resuelve: el panel solo avisa mientras alguien lo tiene
abierto. Si el numero del negocio no tiene app movil (el caso de Luxury),
una clienta puede quedarse horas sin respuesta sin que nadie se entere.

Tres decisiones que le dan forma a esto:

1. **Se avisa de lo NO RESPONDIDO, no de lo que entra.** Un mensaje nuevo
   normalmente lo contesta el bot; avisar por cada uno seria ruido, y el
   ruido termina en que todo el mundo silencia los avisos. Solo entra al
   aviso lo que lleva `quiet_minutes` sin que nadie lo lea.

2. **Agrupado, no uno por mensaje.** Un correo con las 3 conversaciones
   pendientes, no 3 correos.

3. **WhatsApp solo si sale gratis.** Meta cobra plantilla fuera de la
   ventana de 24h. Si el duenio mantiene su ventana abierta (p.ej. con un
   atajo del telefono que le escribe al numero del negocio una vez al dia),
   el aviso sale como texto libre y no cuesta. Si esta cerrada, el correo
   sale igual y la plantilla queda SOLO para lo urgente -- y solo si esta
   configurada.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import resolve_by_app_id
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity
from nexolu_comms_api.core.channels.registry import get_channel_registry
from nexolu_comms_api.core.chats import WindowChecker
from nexolu_comms_api.core.db.entities import ChatMessage, Contact, InboxAlertConfig
from nexolu_comms_api.core.db.repository import NotificationRepository
from nexolu_comms_api.core.db.session import get_sessionmaker

logger = logging.getLogger(__name__)

WINDOW_HOURS = 24

# Tope de conversaciones nombradas en un aviso. Mas que esto no se lee: el
# aviso dice "y N mas" y el panel tiene la lista completa.
MAX_LISTED = 5

PANEL_URL = "https://connect.nexolu.co/chat"


async def pending_conversations(
    session: AsyncSession, config: InboxAlertConfig, now: datetime | None = None
) -> list[tuple[Contact, ChatMessage]]:
    """Las conversaciones que llevan rato esperando y de las que aun no se
    ha avisado (o llego algo nuevo desde el ultimo aviso)."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(minutes=max(1, config.quiet_minutes))

    # Una respuesta que Meta rechazó no es una respuesta. Si contara, la
    # conversación quedaba como atendida y nadie recibía el aviso -- pasó
    # con una clienta a la que el bot le "contestó" tres veces desde el
    # número de prueba y no le llegó ninguna.
    efectivos = ChatMessage.status != "failed"

    last = (
        select(ChatMessage.contact_id, func.max(ChatMessage.created_at).label("last_at"))
        .where(efectivos)
        .group_by(ChatMessage.contact_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ChatMessage, Contact)
            .join(
                last,
                (ChatMessage.contact_id == last.c.contact_id)
                & (ChatMessage.created_at == last.c.last_at),
            )
            .join(Contact, Contact.id == ChatMessage.contact_id)
            .where(Contact.app_id == config.app_id)
            .where(efectivos)
            .order_by(ChatMessage.created_at.desc())
            .limit(200)
        )
    ).all()

    pending: list[tuple[Contact, ChatMessage]] = []
    seen: set[str] = set()
    for message, contact in rows:
        if contact.id in seen:
            continue
        seen.add(contact.id)
        if config.business_id and contact.business_id != config.business_id:
            continue
        # Lo ultimo tiene que ser de ELLA, sin leer y con rato encima.
        if message.direction != "in" or message.created_at > cutoff:
            continue
        if contact.last_read_at is not None and contact.last_read_at >= message.created_at:
            continue
        # Ya se aviso de este mismo mensaje: no se repite hasta que escriba
        # otra vez (o alguien lea y vuelva a quedarse sin responder).
        if contact.alerted_at is not None and contact.alerted_at >= message.created_at:
            continue
        pending.append((contact, message))

    return pending


def compose(pending: list[tuple[Contact, ChatMessage]]) -> tuple[str, str]:
    """@return (asunto, cuerpo) del aviso, ya en español."""
    total = len(pending)
    asunto = (
        "1 conversación sin responder en WhatsApp"
        if total == 1
        else f"{total} conversaciones sin responder en WhatsApp"
    )

    lineas = []
    for contact, message in pending[:MAX_LISTED]:
        quien = contact.name or contact.phone
        texto = message.body.strip() or "(multimedia)"
        if len(texto) > 90:
            texto = texto[:90] + "…"
        lineas.append(f"• {quien} ({contact.phone}): «{texto}»")
    if total > MAX_LISTED:
        lineas.append(f"• …y {total - MAX_LISTED} más.")

    cuerpo = "\n".join([*lineas, "", f"Responde desde la bandeja: {PANEL_URL}"])
    return asunto, cuerpo


async def _whatsapp_window_open(
    session: AsyncSession, app_id: str, phone: str, now: datetime
) -> bool:
    """¿El destinatario del aviso nos escribió en las ultimas 24h? Si si, el
    aviso sale como texto libre (gratis); si no, hay que plantilla."""
    contact = (
        await session.execute(
            select(Contact).where(Contact.app_id == app_id, Contact.phone == phone.lstrip("+"))
        )
    ).scalars().first()
    if contact is None:
        return False
    # Con el numero que hoy envia: la ventana es con UN numero del negocio.
    return await WindowChecker(session).is_open(contact, now)


async def send_alerts(now: datetime | None = None) -> int:
    """Una pasada del worker. @return cuantos avisos se mandaron."""
    now = now or datetime.utcnow()
    enviados = 0

    async with get_sessionmaker()() as session:
        configs = (
            await session.execute(
                select(InboxAlertConfig).where(InboxAlertConfig.is_active.is_(True))
            )
        ).scalars().all()

        for config in configs:
            pending = await pending_conversations(session, config, now)
            if not pending:
                continue

            asunto, cuerpo = compose(pending)
            app = await resolve_by_app_id(session, config.app_id)
            if app is None:
                continue

            repo = NotificationRepository(session)
            registry = get_channel_registry()

            for email in config.emails:
                result = await registry.resolve("email").send(
                    app, OutboundMessage(to=email, subject=f"[Connect] {asunto}", text=cuerpo)
                )
                repo.log(
                    app_id=config.app_id,
                    business_id=config.business_id or config.app_id,
                    channel="email",
                    recipient=email,
                    status=result.status,
                    reference="inbox_alert",
                    provider_message_id=result.provider_message_id,
                    error=result.error,
                    cost_micros=result.cost_micros,
                )
                enviados += 1

            if config.whatsapp_to:
                await _alert_whatsapp(session, app, config, asunto, cuerpo, now)
                enviados += 1

            for contact, _ in pending:
                contact.alerted_at = now

            await session.commit()

    return enviados


async def _alert_whatsapp(
    session: AsyncSession,
    app,
    config: InboxAlertConfig,
    asunto: str,
    cuerpo: str,
    now: datetime,
) -> None:
    abierta = await _whatsapp_window_open(session, config.app_id, config.whatsapp_to, now)

    if abierta:
        message = OutboundMessage(
            to=config.whatsapp_to, text=f"🔔 {asunto}\n\n{cuerpo}", category="service"
        )
    elif config.urgent_template:
        # Ventana cerrada: la plantilla es lo unico que entrega, y se cobra.
        # Por eso solo va el titular, no el detalle: el detalle esta en el
        # panel y en el correo.
        message = OutboundMessage(
            to=config.whatsapp_to,
            category="utility",
            template_name=config.urgent_template,
            template_language=config.urgent_template_language,
            template_components=[
                {"type": "body", "parameters": [{"type": "text", "text": asunto}]}
            ],
        )
    else:
        # Sin ventana y sin plantilla: el correo ya salio, no se inventa un
        # envio que Meta va a rechazar.
        logger.info(
            "alerts.whatsapp_skipped_closed_window",
            extra={"app_id": config.app_id},
        )
        return

    identity, _ = await resolve_whatsapp_identity(
        session, app, config.business_id or config.app_id
    )
    result = await get_channel_registry().resolve("whatsapp").send(identity, message)
    NotificationRepository(session).log(
        app_id=config.app_id,
        business_id=config.business_id or config.app_id,
        channel="whatsapp",
        recipient=config.whatsapp_to,
        status=result.status,
        reference="inbox_alert",
        provider_message_id=result.provider_message_id,
        error=result.error,
        cost_micros=result.cost_micros,
    )


async def alert_worker_loop() -> None:
    """Task de proceso (lifespan), mismo patron que los otros workers:
    duerme, avisa, y nunca muere en silencio."""
    from nexolu_comms_api.config import get_settings

    settings = get_settings()
    while True:
        await asyncio.sleep(settings.inbox_alert_interval_seconds)
        try:
            await send_alerts()
        except Exception:
            logger.exception("alerts.worker_error")
