"""Numeros que el negocio ya no usa, pero siguen recibiendo mensajes.

Por que existe. Luxury paso del 304 112 8994 al 301 948 9912 (la gente que
ya conocia el 304 no podia escribirle). El 304 sigue conectado a la cuenta
y le siguen llegando mensajes; la app y el bot ya responden desde el 301, y
un texto desde el 301 a quien solo le escribio al 304 no se entrega (la
ventana de 24 h es con el numero). Resultado: silencio.

Aca, el mensaje que entra a un numero retirado NO va a la app ni a los
flujos: se contesta desde ESE mismo numero -- con el que la persona si
tiene la ventana abierta -- diciendo a donde escribir. Una vez al dia por
persona, para no responder cada mensaje de una rafaga.

Configuracion (RETIRED_NUMBER_REPLIES, JSON):
    {"<phone_number_id viejo>": {"text": "...", "cta_url": "...", "cta_title": "..."}}
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.channels.base import OutboundMessage

logger = logging.getLogger(__name__)

ONCE_PER_SECONDS = 24 * 3600

# (numero viejo, telefono) -> cuando se le contesto. En memoria del proceso:
# si reinicia, a lo sumo alguien recibe el aviso dos veces el mismo dia.
_replied: dict[tuple[str, str], float] = {}


def reply_for(phone_number_id: str | None) -> dict[str, Any] | None:
    """La respuesta configurada para ese numero, o None si no esta retirado."""
    if not phone_number_id:
        return None
    raw = get_settings().retired_number_replies
    if not raw:
        return None
    try:
        config = json.loads(raw)
    except ValueError:
        logger.warning("retired_numbers.invalid_json")
        return None
    reply = config.get(phone_number_id)
    return reply if isinstance(reply, dict) and reply.get("text") else None


def _sender_of(payload: str) -> str | None:
    try:
        return str(json.loads(payload)["entry"][0]["changes"][0]["value"]["messages"][0]["from"]) or None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def answer_on_retired_number(app: AppIdentity, phone_number_id: str, payload: str) -> bool:
    """Contesta desde el numero viejo. Nunca lanza. @return si salio."""
    reply = reply_for(phone_number_id)
    to = _sender_of(payload)
    if reply is None or to is None or app.whatsapp is None:
        return False

    now = time.time()
    key = (phone_number_id, to)
    if now - _replied.get(key, 0) < ONCE_PER_SECONDS:
        return False
    _replied[key] = now

    from nexolu_comms_api.core.channels.registry import get_channel_registry

    old_number = dataclasses.replace(app, whatsapp=app.whatsapp.model_copy(update={"phone_number_id": phone_number_id}))
    try:
        result = await get_channel_registry().resolve("whatsapp").send(
            old_number,
            OutboundMessage(
                to=to,
                text=reply["text"],
                category="service",
                cta_url=reply.get("cta_url"),
                cta_title=reply.get("cta_title"),
            ),
        )
    except Exception:
        logger.exception("retired_numbers.send_error")
        return False
    logger.info("retired_numbers.answered", extra={"status": getattr(result, "status", None)})
    return getattr(result, "status", None) == "sent"
