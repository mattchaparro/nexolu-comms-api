"""Web Push: el aviso al celular cada vez que alguien escribe.

Por que existe. El numero del negocio no tiene app de WhatsApp ni SIM: el
chat de Connect ES su WhatsApp. Sin esto, enterarse de que alguien
escribio depende de tener la pestana abierta y mirarla.

Como decide a quien. Cada suscripcion es de una persona (`user_email`, el
`sub` de su sesion), y el alcance se resuelve AL ENVIAR, contra la BD:
la recepcionista de un salon recibe solo lo de su salon, el admin de
Nexolu recibe todo, y a quien desactivaron no le llega nada (y sus
suscripciones se borran). Es la misma regla que recorta la bandeja
(`PanelScope.allows_contact`), no una segunda.

Una notificacion por conversacion: el `tag` es el contacto, asi que diez
mensajes seguidos de la misma persona reemplazan el aviso en vez de
apilar diez.

Nunca lanza: un push que falla no puede tumbar el procesamiento del
webhook.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.panel import identity_by_email
from nexolu_comms_api.core.db.entities import ChatMessage, Contact, PushSubscription, WebhookEvent
from nexolu_comms_api.core.db.session import get_sessionmaker

logger = logging.getLogger(__name__)

# Si el celular esta apagado, el servicio de push guarda el aviso hasta
# 12 h. Mas tarde ya no sirve: para entonces se ve en la bandeja.
PUSH_TTL_SECONDS = 12 * 3600

# Lo que se ve cuando el mensaje no es texto.
_MEDIA_LABELS = {
    "image": "📷 Foto",
    "video": "🎥 Video",
    "audio": "🎤 Audio",
    "voice": "🎤 Audio",
    "document": "📄 Documento",
    "sticker": "Sticker",
    "location": "📍 Ubicación",
    "contacts": "👤 Contacto",
}


@dataclass(frozen=True)
class PushResult:
    status_code: int  # 0 = no hubo respuesta (red caida, llave mala)


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def send_web_push(subscription: PushSubscription, data: dict[str, Any]) -> PushResult:
    """Un envio, sincrono (pywebpush usa requests): se llama en un hilo.
    Separado para que los tests lo reemplacen sin tocar la red."""
    from pywebpush import WebPushException, webpush

    settings = get_settings()
    try:
        response = webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(data, ensure_ascii=False),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
            content_encoding=subscription.content_encoding or "aes128gcm",
            ttl=PUSH_TTL_SECONDS,
            headers={"Urgency": "high"},
            timeout=10,
        )
        return PushResult(status_code=getattr(response, "status_code", 201))
    except WebPushException as error:
        status_code = error.response.status_code if error.response is not None else 0
        return PushResult(status_code=status_code)
    except Exception:
        logger.exception("push.send_error")
        return PushResult(status_code=0)


def message_preview(message: ChatMessage) -> str:
    body = (message.body or "").strip()
    if body:
        return body if len(body) <= 140 else body[:139] + "…"
    return _MEDIA_LABELS.get(message.message_type, "Nuevo mensaje")


def build_payload(message: ChatMessage, contact: Contact) -> dict[str, Any]:
    title = contact.name.strip() if contact.name else ""
    if not title:
        title = f"+{contact.phone}" if contact.phone and not contact.phone.startswith("+") else contact.phone
    return {
        "title": title,
        "body": message_preview(message),
        "tag": f"chat-{contact.id}",
        # Ruta del panel: el service worker la resuelve contra su origen.
        "url": f"/chat?c={contact.id}",
        "contact_id": contact.id,
        "app_id": contact.app_id,
        "business_id": contact.business_id,
    }


async def notify_chat_message(session: AsyncSession, message: ChatMessage) -> int:
    """Avisa a quien pueda ver esta conversacion. Devuelve a cuantos
    celulares salio (para logs y tests)."""
    if not is_configured() or message.direction != "in":
        return 0

    contact = await session.get(Contact, message.contact_id)
    if contact is None:
        return 0

    by_email: dict[str, list[PushSubscription]] = defaultdict(list)
    for sub in (await session.execute(select(PushSubscription))).scalars():
        by_email[sub.user_email].append(sub)
    if not by_email:
        return 0

    targets: list[PushSubscription] = []
    gone: list[str] = []
    for email, subs in by_email.items():
        identity = await identity_by_email(session, email)
        if identity is None:
            # Desactivada o borrada: sus celulares no deben seguir avisando.
            gone.extend(s.id for s in subs)
            continue
        if identity.scope.allows_contact(contact.app_id, contact.business_id):
            targets.extend(subs)

    payload = build_payload(message, contact)
    results = await asyncio.gather(*(asyncio.to_thread(send_web_push, sub, payload) for sub in targets))

    sent = 0
    now = datetime.utcnow()
    for sub, result in zip(targets, results, strict=True):
        if result.status_code in (404, 410):
            # El navegador retiro la suscripcion (desinstalo, borro datos,
            # revoco el permiso): no vuelve a servir nunca.
            gone.append(sub.id)
        elif 200 <= result.status_code < 300:
            sub.last_sent_at = now
            sent += 1
        else:
            logger.warning("push.failed", extra={"status": result.status_code, "subscription_id": sub.id})

    if gone:
        await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
    await session.commit()
    return sent


def _inbound_wamid(raw_payload: str) -> str | None:
    try:
        return str(json.loads(raw_payload)["entry"][0]["changes"][0]["value"]["messages"][0]["id"]) or None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def notify_inbound_event(event_id: str) -> None:
    """Punto de entrada desde el webhook, DESPUES de que el motor guardo
    el mensaje en la bandeja. Nunca lanza."""
    if not is_configured():
        return
    try:
        async with get_sessionmaker()() as session:
            event = await session.get(WebhookEvent, event_id)
            if event is None or event.event_type != "message":
                return
            wamid = _inbound_wamid(event.payload)
            if wamid is None:
                return
            message = (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.wamid == wamid, ChatMessage.direction == "in")
                    .order_by(ChatMessage.created_at.desc())
                )
            ).scalars().first()
            if message is None:
                return
            await notify_chat_message(session, message)
    except Exception:
        logger.exception("push.inbound_error", extra={"event_id": event_id})
