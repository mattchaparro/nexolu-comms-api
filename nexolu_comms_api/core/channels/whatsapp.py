"""Canal de WhatsApp, via WhatsApp Cloud API (Meta) directo - sin SDK, la
misma llamada HTTP que ya hacia `App\\Services\\WhatsApp\\WhatsAppCloudClient`
en el POS antes de que este servicio existiera.

Envia texto libre (solo funciona dentro de la ventana de 24h desde el
ultimo mensaje del usuario), plantilla (`template_name`, requerido fuera de
esa ventana o para mensajes que el negocio inicia), o un Flow (`flow_id`,
formulario nativo con datos prellenados - se usa para confirmar un
borrador de escritura sin salir de WhatsApp). El costo se estima por
categoria (ver Settings.whatsapp_rate_*_micros); un mensaje sin categoria
declarada queda con costo desconocido (cost_micros=None), no en cero.
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


class WhatsAppChannel(ChannelSender):
    name = "whatsapp"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def send(self, app: AppIdentity, message: OutboundMessage) -> ChannelSendResult:
        if app.whatsapp is None:
            return ChannelSendResult(status=STATUS_SKIPPED, error="WhatsApp no configurado para esta app.")

        payload = self._build_payload(message, default_catalog_id=app.whatsapp.catalog_id)
        if payload is None:
            return ChannelSendResult(status=STATUS_FAILED, error="El mensaje no trae texto ni plantilla para WhatsApp.")

        response, error = await self._post(app, payload)
        if error is not None:
            return error

        body = response.json()
        message_id = (body.get("messages") or [{}])[0].get("id")

        return ChannelSendResult(
            status=STATUS_SENT,
            provider_message_id=message_id,
            cost_micros=self._estimate_cost_micros(message.category),
        )

    async def mark_as_read_with_typing(self, app: AppIdentity, to: str, message_id: str) -> bool:
        """Marca leido + activa el indicador de "escribiendo...". Mismo
        endpoint de mensajes que `send()`, con status:read + typing_indicator
        - no un endpoint aparte en la Graph API."""
        if app.whatsapp is None:
            return False

        _, error = await self._post(
            app,
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            },
        )

        return error is None

    async def _post(self, app: AppIdentity, payload: dict) -> tuple[httpx.Response | None, ChannelSendResult | None]:
        """@return (response, None) si Meta acepto, o (None, resultado_fallido) si no."""
        assert app.whatsapp is not None
        url = f"{self._settings.whatsapp_api_base_url}/{app.whatsapp.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {app.whatsapp.access_token}"}

        try:
            async with httpx.AsyncClient(timeout=self._settings.http_timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("whatsapp.send_failed", extra={"app_id": app.app_id, "error": str(exc)})
            return None, ChannelSendResult(status=STATUS_FAILED, error=f"No se pudo contactar a WhatsApp Cloud API: {exc}")

        if response.is_error:
            detail = self._error_detail(response)
            logger.warning(
                "whatsapp.send_rejected",
                extra={"app_id": app.app_id, "status_code": response.status_code, "detail": detail},
            )
            return None, ChannelSendResult(
                status=STATUS_FAILED, error=detail, provider_status_code=response.status_code
            )

        return response, None

    def _build_payload(self, message: OutboundMessage, default_catalog_id: str | None = None) -> dict | None:
        catalog_id = message.catalog_id or default_catalog_id

        if message.product_retailer_id:
            # SPM (analisis H.2). catalog_id es obligatorio en el payload.
            if not catalog_id:
                return None
            interactive: dict = {
                "type": "product",
                "action": {"catalog_id": catalog_id, "product_retailer_id": message.product_retailer_id},
            }
            if message.text:
                interactive["body"] = {"text": message.text}
            if message.product_footer:
                interactive["footer"] = {"text": message.product_footer}
            return {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": message.to,
                "type": "interactive",
                "interactive": interactive,
            }

        if message.product_sections:
            # MPM (analisis H.3): header y body obligatorios, max 30 items.
            if not catalog_id:
                return None
            interactive = {
                "type": "product_list",
                "header": {"type": "text", "text": message.product_header or ""},
                "body": {"text": message.text or ""},
                "action": {
                    "catalog_id": catalog_id,
                    "sections": [
                        {
                            "title": section.get("title", ""),
                            "product_items": [
                                {"product_retailer_id": rid}
                                for rid in section.get("product_retailer_ids", [])
                            ],
                        }
                        for section in message.product_sections
                    ],
                },
            }
            if message.product_footer:
                interactive["footer"] = {"text": message.product_footer}
            return {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": message.to,
                "type": "interactive",
                "interactive": interactive,
            }

        if message.send_catalog:
            # Catalogo completo (analisis H.4): body max 1024, footer max 60.
            parameters: dict = {}
            if message.catalog_thumbnail_retailer_id:
                parameters["thumbnail_product_retailer_id"] = message.catalog_thumbnail_retailer_id
            interactive = {
                "type": "catalog_message",
                "body": {"text": (message.text or "")[:1024]},
                "action": {"name": "catalog_message", "parameters": parameters},
            }
            if message.product_footer:
                interactive["footer"] = {"text": message.product_footer[:60]}
            return {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": message.to,
                "type": "interactive",
                "interactive": interactive,
            }

        if message.flow_id:
            return {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "interactive",
                "interactive": {
                    "type": "flow",
                    "body": {"text": message.text or ""},
                    "action": {
                        "name": "flow",
                        "parameters": {
                            "flow_message_version": "3",
                            "flow_token": message.flow_token or "",
                            "flow_id": message.flow_id,
                            "flow_cta": message.flow_cta or "",
                            "flow_action": "navigate",
                            "flow_action_payload": {
                                "screen": message.flow_screen or "",
                                "data": message.flow_data,
                            },
                        },
                    },
                },
            }

        if message.media_kind and message.media_url:
            # Payload oficial de media por link: {"type":"image","image":
            # {"link":...,"caption":...}}. Meta descarga el archivo; si el
            # link no es publico el mensaje falla alla, no aca.
            media: dict = {"link": message.media_url}
            if message.media_caption and message.media_kind in ("image", "video", "document"):
                media["caption"] = message.media_caption
            if message.media_filename and message.media_kind == "document":
                media["filename"] = message.media_filename
            return {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": message.media_kind,
                message.media_kind: media,
            }

        if message.list_rows:
            # interactive.list (analisis A.4): hasta 10 filas; titulos max
            # 24 y descripciones max 72 - se recortan aca en vez de dejar
            # que Meta rechace el mensaje completo.
            rows = []
            for row in message.list_rows[:10]:
                item: dict = {"id": row["id"], "title": row["title"][:24]}
                if row.get("description"):
                    item["description"] = row["description"][:72]
                rows.append(item)
            return {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": message.text or ""},
                    "action": {
                        "button": (message.list_button or "Ver opciones")[:20],
                        "sections": [{"title": (message.list_button or "Opciones")[:24], "rows": rows}],
                    },
                },
            }

        if message.template_name:
            return {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "template",
                "template": {
                    "name": message.template_name,
                    "language": {"code": message.template_language or "es"},
                    "components": message.template_components,
                },
            }

        if message.buttons:
            return {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": message.text or ""},
                    "action": {
                        "buttons": [
                            # Reglas de Meta: max 3 botones, titulos de max
                            # 20 caracteres - se recortan aca en vez de dejar
                            # que Meta rechace el mensaje completo.
                            {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                            for b in message.buttons[:3]
                        ]
                    },
                },
            }

        if message.cta_url:
            return {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "interactive",
                "interactive": {
                    "type": "cta_url",
                    "body": {"text": message.text or ""},
                    "action": {
                        "name": "cta_url",
                        "parameters": {
                            "display_text": (message.cta_title or "Abrir")[:20],
                            "url": message.cta_url,
                        },
                    },
                },
            }

        if message.text:
            return {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "text",
                "text": {"body": message.text},
            }

        return None

    def _estimate_cost_micros(self, category: str | None) -> int | None:
        rates = {
            "marketing": self._settings.whatsapp_rate_marketing_micros,
            "utility": self._settings.whatsapp_rate_utility_micros,
            "authentication": self._settings.whatsapp_rate_authentication_micros,
            "service": self._settings.whatsapp_rate_service_micros,
        }
        return rates.get(category or "") if category else None

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            return str(response.json().get("error", {}).get("message", response.text))
        except ValueError:
            return response.text
