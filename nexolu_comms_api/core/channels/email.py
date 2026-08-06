"""Canal de email, via la API transaccional de Brevo (`POST /v3/smtp/email`)
directo - no SMTP: este servicio es el emisor centralizado, no una app con
su propia cola de correo, asi que tiene sentido usar la API HTTP y obtener
el `messageId` de vuelta en la misma respuesta.

`brevo_api_key` sale de la app (identidad de remitente propia) o cae a la
API key de PLATAFORMA si la app no trae la suya - ver
AppRegistration.email en config.py. El costo por email no se estima:
Brevo no lo informa por envio en la respuesta y la mayoria de planes son
por volumen/suscripcion, no por mensaje - queda con cost_micros=None
(desconocido), no en cero.
"""
from __future__ import annotations

import logging

import httpx

from nexolu_comms_api.config import Settings, get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.channels.base import (
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    ChannelSender,
    ChannelSendResult,
    OutboundMessage,
)

logger = logging.getLogger(__name__)


class EmailChannel(ChannelSender):
    name = "email"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def send(self, app: AppIdentity, message: OutboundMessage) -> ChannelSendResult:
        if app.email is None:
            return ChannelSendResult(status=STATUS_SKIPPED, error="Email no configurado para esta app.")

        api_key = app.email.brevo_api_key or self._settings.brevo_api_key
        if not api_key:
            return ChannelSendResult(
                status=STATUS_SKIPPED,
                error="Sin API key de Brevo (ni en la app ni en la plataforma).",
            )

        if not message.html and not message.text:
            return ChannelSendResult(status=STATUS_FAILED, error="El mensaje no trae texto ni html para email.")

        payload = {
            "sender": {"email": app.email.from_email, "name": app.email.from_name or app.name},
            "to": [{"email": message.to}],
            "subject": message.subject or "",
            "htmlContent": message.html or f"<p>{message.text}</p>",
        }
        if message.text:
            payload["textContent"] = message.text

        headers = {"api-key": api_key, "Content-Type": "application/json"}
        url = f"{self._settings.brevo_api_base_url}/smtp/email"

        try:
            async with httpx.AsyncClient(timeout=self._settings.http_timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("email.send_failed", extra={"app_id": app.app_id, "error": str(exc)})
            return ChannelSendResult(status=STATUS_FAILED, error=f"No se pudo contactar a Brevo: {exc}")

        if response.is_error:
            detail = self._error_detail(response)
            logger.warning(
                "email.send_rejected", extra={"app_id": app.app_id, "status_code": response.status_code, "detail": detail}
            )
            return ChannelSendResult(status=STATUS_FAILED, error=detail)

        body = response.json()
        return ChannelSendResult(status=STATUS_SENT, provider_message_id=body.get("messageId"), cost_micros=None)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            return str(response.json().get("message", response.text))
        except ValueError:
            return response.text
