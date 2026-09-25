"""Webhook de WhatsApp Cloud API, uno por app (no por negocio dentro de una
app: Meta registra el webhook a nivel de App/WABA, y el numero compartido
de una app multi-tenant - ver Nexolu POS - ya resuelve el negocio del lado
de la app, no del lado de Meta).

Este servicio NUNCA interpreta el contenido de un evento entrante (texto,
respuesta de un Flow, etc.) - eso es logica de cada app, no de un servicio
producto-agnostico. `verify()` responde el handshake de Meta;
`receive_event()` verifica la firma de Meta, PERSISTE el evento crudo en
`webhook_events`, responde 200 de inmediato (Meta reintenta si no responde
rapido) y dispara el primer intento de reenvio en segundo plano. Si ese
intento falla, el worker de core/webhooks/forwarder.py lo reintenta con
backoff - un evento ya no se pierde por un callback caido.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.channels.business_channels import BusinessChannelRepository
from nexolu_comms_api.core.db.entities import WebhookEvent
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.flows.engine import handle_inbound_event
from nexolu_comms_api.core.chats import apply_chat_statuses_from_event
from nexolu_comms_api.core.templates.service import apply_status_update_from_event
from nexolu_comms_api.core.webhooks import forwarder
from nexolu_comms_api.core.webhooks.events import classify
from nexolu_comms_api.core.webhooks.retired_numbers import answer_on_retired_number, reply_for
from nexolu_comms_api.core.webhooks.signing import verify_meta_signature

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])
logger = logging.getLogger(__name__)

# app_id reservado para eventos del webhook de PLATAFORMA que no se pudieron
# atribuir a ningun BusinessChannel (numero desconocido, firma invalida).
PLATFORM_APP_ID = "platform"


@router.get("/platform")
async def verify_platform(request: Request) -> Response:
    """Handshake de Meta para el webhook de la App de plataforma (los
    numeros propios conectados por Embedded Signup entran todos por aca:
    Meta registra el webhook a nivel de App, no de numero)."""
    settings = get_settings()
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge") or ""

    expected = settings.meta_platform_webhook_verify_token
    if mode == "subscribe" and expected and token == expected:
        return Response(content=challenge, media_type="text/plain")

    logger.warning("webhooks.whatsapp.platform_verify_failed")
    return Response(content="Forbidden", status_code=403)


@router.post("/platform")
async def receive_platform_event(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Evento de un numero propio de un negocio. A diferencia del webhook
    por app, aca la firma es OBLIGATORIA (endpoint nuevo, sin apps legadas
    que acomodar: falla cerrado) y el negocio se resuelve por el
    `phone_number_id` del payload contra `business_channels`."""
    settings = get_settings()
    if not settings.meta_platform_app_secret:
        raise HTTPException(status_code=503, detail="El webhook de plataforma no esta configurado.")

    body = await request.body()
    event_type, phone_number_id = classify(body)

    header = request.headers.get("x-hub-signature-256")
    if not verify_meta_signature(body, header, settings.meta_platform_app_secret):
        session.add(
            WebhookEvent(
                app_id=PLATFORM_APP_ID,
                event_type=event_type,
                phone_number_id=phone_number_id,
                payload=body.decode("utf-8", errors="replace"),
                signature_valid=False,
                forward_status=forwarder.STATUS_REJECTED,
                last_error="Firma de Meta invalida.",
            )
        )
        await session.commit()
        logger.warning("webhooks.whatsapp.platform_rejected", extra={"phone_number_id": phone_number_id})
        raise HTTPException(status_code=401, detail="Firma de Meta invalida.")

    channel = (
        await BusinessChannelRepository(session).get_by_phone_number_id(phone_number_id)
        if phone_number_id
        else None
    )

    event = WebhookEvent(
        app_id=channel.app_id if channel else PLATFORM_APP_ID,
        business_channel_id=channel.id if channel else None,
        event_type=event_type,
        phone_number_id=phone_number_id,
        payload=body.decode("utf-8", errors="replace"),
        signature_valid=True,
        # Sin canal conocido no hay a quien reenviar: queda `skipped`,
        # visible en el panel (un numero suscrito que nadie reclama es un
        # sintoma de onboarding a medias, no algo que ocultar).
        forward_status=forwarder.STATUS_PENDING if channel else forwarder.STATUS_SKIPPED,
    )
    session.add(event)
    await session.commit()

    if event_type == "template":
        # Side-effect interno: reflejar el estado en el espejo de
        # plantillas. El reenvio a la app duena no cambia.
        background_tasks.add_task(apply_status_update_from_event, event.id)
    elif event_type == "message":
        # Motor de flujos (opt-in por flujos activos): puede responder a un
        # boton o a un keyword. El reenvio a la app duena no cambia.
        background_tasks.add_task(handle_inbound_event, event.id)
    elif event_type == "status":
        background_tasks.add_task(apply_chat_statuses_from_event, event.id)

    if channel:
        background_tasks.add_task(forwarder.attempt_forward, event.id)
    else:
        logger.warning(
            "webhooks.whatsapp.platform_unknown_number", extra={"phone_number_id": phone_number_id}
        )

    return {"ok": True}


@router.get("/{app_id}")
async def verify(app_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    identity = await _resolve(session, app_id)

    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge") or ""

    expected_token = identity.whatsapp.webhook_verify_token if identity and identity.whatsapp else None

    if mode == "subscribe" and expected_token and token == expected_token:
        return Response(content=challenge, media_type="text/plain")

    logger.warning("webhooks.whatsapp.verify_failed", extra={"app_id": app_id})

    return Response(content="Forbidden", status_code=403)


@router.post("/{app_id}")
async def receive_event(
    app_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    identity = await _resolve(session, app_id)
    if identity is None or identity.whatsapp is None:
        raise HTTPException(status_code=404, detail="App desconocida o sin WhatsApp configurado.")

    body = await request.body()
    event_type, phone_number_id = classify(body)

    signature_valid: bool | None = None
    rejection: str | None = None

    if identity.whatsapp.meta_app_secret:
        header = request.headers.get("x-hub-signature-256")
        signature_valid = verify_meta_signature(body, header, identity.whatsapp.meta_app_secret)
        if not signature_valid:
            rejection = "Firma de Meta invalida."
    elif identity.whatsapp.enforce_meta_signature:
        # La app exige firma pero no tiene secret configurado: configuracion
        # incompleta. Fallar cerrado - aceptar seria exactamente el agujero
        # que el flag existe para tapar.
        rejection = "La app exige firma de Meta pero no tiene meta_app_secret configurado."

    # El evento se persiste TAMBIEN cuando se rechaza: un rechazo es una
    # senal operativa (ataque, o secret mal configurado) que el panel debe
    # poder mostrar; `rejected` nunca se reenvia ni se reintenta.
    event = WebhookEvent(
        app_id=app_id,
        event_type=event_type,
        phone_number_id=phone_number_id,
        payload=body.decode("utf-8", errors="replace"),
        signature_valid=signature_valid,
        forward_status=forwarder.STATUS_REJECTED if rejection else forwarder.STATUS_PENDING,
        last_error=rejection,
    )
    session.add(event)
    await session.commit()

    if rejection:
        logger.warning(
            "webhooks.whatsapp.rejected",
            extra={"app_id": app_id, "event_id": event.id, "reason": rejection},
        )
        raise HTTPException(status_code=401, detail=rejection)

    # Un numero que el negocio ya no usa: se le contesta desde ese mismo
    # numero a donde escribir, y el mensaje no sigue a la app ni a los
    # flujos (responderian desde el numero nuevo, y eso no se entrega).
    if event_type == "message" and reply_for(phone_number_id) is not None:
        event.forward_status = forwarder.STATUS_SKIPPED
        await session.commit()
        background_tasks.add_task(answer_on_retired_number, identity, phone_number_id, event.payload)
        return {"ok": True}

    if event_type == "template":
        # Side-effect interno: reflejar el estado en el espejo de
        # plantillas. El reenvio a la app duena no cambia.
        background_tasks.add_task(apply_status_update_from_event, event.id)
    elif event_type == "message":
        # Motor de flujos, misma nota que en el webhook de plataforma.
        background_tasks.add_task(handle_inbound_event, event.id)
    elif event_type == "status":
        # Que la bandeja sepa si llego, se leyo o Meta lo rechazo despues de
        # aceptarlo. El reenvio a la app duena no cambia.
        background_tasks.add_task(apply_chat_statuses_from_event, event.id)

    if identity.whatsapp.callback_url and identity.whatsapp.callback_secret:
        # Primer intento inmediato, fuera del request: responder rapido y
        # siempre 200 - si esta llamada se demorara, Meta consideraria la
        # entrega fallida y reintentaria, duplicando el evento del lado de
        # la app. Si el proceso muere antes del intento, el worker adopta el
        # evento `pending` (ver PENDING_GRACE_SECONDS).
        background_tasks.add_task(forwarder.attempt_forward, event.id)
    else:
        event.forward_status = forwarder.STATUS_SKIPPED
        await session.commit()
        logger.warning("webhooks.whatsapp.no_callback_configured", extra={"app_id": app_id})

    return {"ok": True}


async def _resolve(session: AsyncSession, app_id: str) -> AppIdentity | None:
    return await resolve_by_app_id(session, app_id)
