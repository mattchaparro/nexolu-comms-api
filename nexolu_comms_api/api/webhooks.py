"""Webhook de WhatsApp Cloud API, uno por app (no por negocio dentro de una
app: Meta registra el webhook a nivel de App/WABA, y el numero compartido
de una app multi-tenant - ver Nexolu POS - ya resuelve el negocio del lado
de la app, no del lado de Meta).

Este servicio NUNCA interpreta el contenido de un evento entrante (texto,
respuesta de un Flow, etc.) - eso es logica de cada app, no de un servicio
producto-agnostico. `verify()` responde el handshake de Meta;
`receive_event()` verifica la firma de Meta, responde 200 de inmediato (Meta
reintenta si no responde rapido - ver mismo comentario en
WhatsappWebhookController del POS) y reenvia el evento crudo, firmado, al
callback propio de esa app EN SEGUNDO PLANO.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from nexolu_comms_api.config import Settings, get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity, get_app_registry
from nexolu_comms_api.core.webhooks.signing import build_forward_headers, verify_meta_signature

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])
logger = logging.getLogger(__name__)


@router.get("/{app_id}")
async def verify(app_id: str, request: Request) -> Response:
    identity = _resolve(app_id)

    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge") or ""

    expected_token = identity.whatsapp.webhook_verify_token if identity and identity.whatsapp else None

    if mode == "subscribe" and expected_token and token == expected_token:
        return Response(content=challenge, media_type="text/plain")

    logger.warning("webhooks.whatsapp.verify_failed", extra={"app_id": app_id})

    return Response(content="Forbidden", status_code=403)


@router.post("/{app_id}")
async def receive_event(app_id: str, request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    identity = _resolve(app_id)
    if identity is None or identity.whatsapp is None:
        raise HTTPException(status_code=404, detail="App desconocida o sin WhatsApp configurado.")

    body = await request.body()

    if identity.whatsapp.meta_app_secret:
        header = request.headers.get("x-hub-signature-256")
        if not verify_meta_signature(body, header, identity.whatsapp.meta_app_secret):
            logger.warning("webhooks.whatsapp.invalid_meta_signature", extra={"app_id": app_id})
            raise HTTPException(status_code=401, detail="Firma de Meta invalida.")

    if identity.whatsapp.callback_url and identity.whatsapp.callback_secret:
        background_tasks.add_task(_forward, identity, body, get_settings())
    else:
        logger.warning("webhooks.whatsapp.no_callback_configured", extra={"app_id": app_id})

    # Responder rapido y siempre 200: si esta llamada se demora o falla,
    # Meta considera la entrega fallida y reintenta, duplicando el evento
    # del lado de la app - igual comportamiento que WhatsappWebhookController
    # en el POS de hoy.
    return {"ok": True}


def _resolve(app_id: str) -> AppIdentity | None:
    return get_app_registry().resolve_by_app_id(app_id)


async def _forward(identity: AppIdentity, body: bytes, settings: Settings) -> None:
    assert identity.whatsapp is not None  # ya se valido en receive_event()
    headers = {
        "Content-Type": "application/json",
        **build_forward_headers(body, identity.whatsapp.callback_secret),
    }

    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            response = await client.post(identity.whatsapp.callback_url, content=body, headers=headers)
        if response.is_error:
            logger.warning(
                "webhooks.whatsapp.forward_rejected",
                extra={"app_id": identity.app_id, "status_code": response.status_code},
            )
    except httpx.HTTPError as exc:
        # No hay reintento ni cola de por medio en esta primera version: si
        # el callback de la app no responde, el evento se pierde. Documentado
        # como limitacion conocida en el README.
        logger.warning("webhooks.whatsapp.forward_failed", extra={"app_id": identity.app_id, "error": str(exc)})
