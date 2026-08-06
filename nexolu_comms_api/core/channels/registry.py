"""Fabrica de canales de envio. Agregar un canal nuevo (SMS, push...) es:

1. escribir la clase (heredando de `ChannelSender`),
2. agregar una entrada aca.

Nada mas del servicio necesita cambiar: el endpoint de envio y el catalogo
de canales solo piden canales por nombre.
"""
from __future__ import annotations

from functools import lru_cache

from nexolu_comms_api.core.channels.base import ChannelSender
from nexolu_comms_api.core.channels.email import EmailChannel
from nexolu_comms_api.core.channels.exceptions import UnknownChannelError
from nexolu_comms_api.core.channels.whatsapp import WhatsAppChannel


class ChannelRegistry:
    def __init__(self) -> None:
        self._senders: dict[str, ChannelSender] = {
            "whatsapp": WhatsAppChannel(),
            "email": EmailChannel(),
        }

    def available(self) -> list[str]:
        return sorted(self._senders.keys())

    def resolve(self, name: str) -> ChannelSender:
        sender = self._senders.get(name)
        if sender is None:
            raise UnknownChannelError(f"Canal desconocido: {name}. Disponibles: {', '.join(self.available())}.")
        return sender


@lru_cache
def get_channel_registry() -> ChannelRegistry:
    return ChannelRegistry()
