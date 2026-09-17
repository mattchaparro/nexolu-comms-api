"""Eventos PROPIOS de Connect hacia la app duena (no vienen de Meta).

Mismo sobre firmado que el reenvio de webhooks (`X-Nexolu-Signature`), pero
el cuerpo lo arma este servicio: `flow_notify` (un flujo pide relevo humano)
y `human_reply` (alguien contesto desde la bandeja del panel).

Por que existe `human_reply`: la app duena tiene su propio agente/bot y NO
puede enterarse por Meta de que un humano ya respondio - el saliente del
panel sale por Cloud API y a la app solo le llegan los ENTRANTES. Sin este
aviso, el bot de la app contesta encima de la persona que esta atendiendo:
dos voces contradiciendose en el mismo chat. Es el equivalente a
`X-Nexolu-Flow-Handled`, pero para el humano.

Fail-soft siempre: avisar es mejor-esfuerzo, nunca puede tumbar el envio
que ya ocurrio (el mensaje YA salio hacia la clienta cuando esto corre).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from nexolu_comms_api.config import WhatsAppAppConfig
from nexolu_comms_api.core.webhooks.signing import build_forward_headers

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 6


async def post_app_event(
    whatsapp: WhatsAppAppConfig | None, event: str, body: dict[str, Any]
) -> bool:
    """Manda un evento firmado al callback de la app. @return si lo entrego."""
    if whatsapp is None or not (whatsapp.callback_url and whatsapp.callback_secret):
        logger.info("app_events.skipped_no_callback", extra={"event": event})
        return False

    payload = json.dumps(
        {"object": "nexolu-comms", "event": event, **body}, ensure_ascii=False
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Nexolu-Event": event.replace("_", "-"),
        **build_forward_headers(payload, whatsapp.callback_secret),
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(whatsapp.callback_url, content=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("app_events.failed", extra={"event": event, "error": str(exc)})
        return False

    if response.is_error:
        logger.warning(
            "app_events.rejected", extra={"event": event, "status_code": response.status_code}
        )
        return False
    return True
