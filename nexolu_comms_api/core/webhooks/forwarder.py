"""Reenvio de eventos de webhook con reintentos, desde `webhook_events`.

Antes el reenvio era un BackgroundTask sin memoria: si el callback de la app
no respondia, el evento se perdia (limitacion conocida del README). Ahora el
flujo es: el endpoint persiste el evento crudo y responde 200 a Meta; el
primer intento de reenvio sale de inmediato (BackgroundTask, igual de rapido
que antes en el caso feliz); si falla, el evento queda `failed` con
`next_retry_at` y un worker asyncio del propio proceso lo reintenta con
backoff hasta entregarlo o declararlo `dead`.

El backoff es deliberadamente corto al principio (un deploy de la app
tarda segundos) y largo al final (una app caida horas no necesita un
martilleo cada minuto). Tras agotar RETRY_DELAYS el evento queda `dead` -
visible y re-lanzable a mano desde el panel, nunca borrado en silencio.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import Settings, get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.db.entities import BusinessChannel, WebhookEvent
from nexolu_comms_api.core.db.session import get_sessionmaker
from nexolu_comms_api.core.webhooks.signing import build_forward_headers

logger = logging.getLogger(__name__)

# Espera antes del intento N+1 (el intento 1 es inmediato). 60s cubre un
# deploy; 6h de cola total cubre una caida seria sin martillar.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 300, 1_500, 7_200, 21_600)

# Un evento `pending` mas viejo que esto es un intento inmediato que nunca
# corrio (proceso reiniciado entre persistir y reenviar): el worker lo
# adopta como si fuera un `failed` listo para reintento.
PENDING_GRACE_SECONDS = 120

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"
STATUS_SKIPPED = "skipped"
STATUS_REJECTED = "rejected"


async def attempt_forward(event_id: str, settings: Settings | None = None) -> None:
    """Un intento de reenvio, con su propia sesion (corre fuera del request
    que persistio el evento). Nunca lanza: todo desenlace queda escrito en
    la fila del evento."""
    settings = settings or get_settings()
    async with get_sessionmaker()() as session:
        event = await session.get(WebhookEvent, event_id)
        if event is None or event.forward_status in (STATUS_DELIVERED, STATUS_REJECTED, STATUS_SKIPPED):
            return

        identity = await resolve_by_app_id(session, event.app_id)
        if identity is None or identity.whatsapp is None:
            _mark_failure(event, "App desconocida o sin WhatsApp configurado al momento del reenvio.")
            await session.commit()
            return

        if not (identity.whatsapp.callback_url and identity.whatsapp.callback_secret):
            # Sin callback no hay a quien entregar: no es un fallo a
            # reintentar, es una app que (aun) no consume webhooks.
            event.forward_status = STATUS_SKIPPED
            await session.commit()
            return

        # Evento de un numero propio (webhook de plataforma): el reenvio
        # lleva el negocio ya resuelto, para que la app no tenga que mapear
        # phone_number_id -> negocio por su cuenta.
        extra_headers: dict[str, str] = {}
        if event.business_channel_id:
            channel = await session.get(BusinessChannel, event.business_channel_id)
            if channel is not None:
                extra_headers["X-Nexolu-Business-Id"] = channel.business_id

        # Coordinacion flujos+bot: si el motor de Connect atendio este
        # mensaje (avanzo una sesion o arranco un flujo), la app lo sabe y
        # su agente IA se calla - dos respuestas a la misma pregunta es
        # peor que ninguna. Sin header = el motor no se pronuncio (evento
        # que no es un mensaje, o motor caido): la app decide sola.
        if event.flow_handled is not None:
            extra_headers["X-Nexolu-Flow-Handled"] = "1" if event.flow_handled else "0"

        await _forward(session, event, identity, settings, extra_headers)


async def _forward(
    session: AsyncSession,
    event: WebhookEvent,
    identity: AppIdentity,
    settings: Settings,
    extra_headers: dict[str, str] | None = None,
) -> None:
    assert identity.whatsapp is not None
    body = event.payload.encode()
    headers = {
        "Content-Type": "application/json",
        **build_forward_headers(body, identity.whatsapp.callback_secret),
        **(extra_headers or {}),
    }

    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            response = await client.post(identity.whatsapp.callback_url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        _mark_failure(event, f"No se pudo contactar el callback: {exc}")
        logger.warning(
            "webhooks.forward_failed",
            extra={"app_id": event.app_id, "event_id": event.id, "attempts": event.attempts},
        )
        await session.commit()
        return

    if response.is_error:
        _mark_failure(event, f"El callback respondio {response.status_code}.")
        logger.warning(
            "webhooks.forward_rejected",
            extra={"app_id": event.app_id, "event_id": event.id, "status_code": response.status_code},
        )
        await session.commit()
        return

    event.attempts += 1
    event.forward_status = STATUS_DELIVERED
    event.delivered_at = datetime.utcnow()
    event.last_error = None
    event.next_retry_at = None
    await session.commit()


def _mark_failure(event: WebhookEvent, error: str) -> None:
    event.attempts += 1
    event.last_error = error
    if event.attempts > len(RETRY_DELAYS_SECONDS):
        event.forward_status = STATUS_DEAD
        event.next_retry_at = None
    else:
        event.forward_status = STATUS_FAILED
        event.next_retry_at = datetime.utcnow() + timedelta(seconds=RETRY_DELAYS_SECONDS[event.attempts - 1])


async def retry_due_events(settings: Settings | None = None) -> int:
    """Reintenta todo lo vencido. @return cuantos eventos se intentaron."""
    settings = settings or get_settings()
    now = datetime.utcnow()
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(WebhookEvent.id)
            .where(
                or_(
                    (WebhookEvent.forward_status == STATUS_FAILED) & (WebhookEvent.next_retry_at <= now),
                    (WebhookEvent.forward_status == STATUS_PENDING)
                    & (WebhookEvent.received_at <= now - timedelta(seconds=PENDING_GRACE_SECONDS)),
                )
            )
            .order_by(WebhookEvent.received_at)
            .limit(50)
        )
        due = [row[0] for row in result]

    for event_id in due:
        await attempt_forward(event_id, settings)

    return len(due)


async def retry_worker_loop() -> None:
    """Task de proceso (arrancada en el lifespan): duerme y reintenta.
    Cualquier excepcion se loguea y el loop sigue - un worker muerto en
    silencio seria volver al comportamiento viejo de perder eventos."""
    settings = get_settings()
    while True:
        await asyncio.sleep(settings.webhook_retry_interval_seconds)
        try:
            await retry_due_events(settings)
        except Exception:
            logger.exception("webhooks.retry_worker_error")
