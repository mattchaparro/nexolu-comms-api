"""Contrato comun a todo canal de envio (WhatsApp, email, y lo que se agregue
despues). Agregar un canal nuevo (SMS, push...) es: escribir una clase que
implemente `ChannelSender` y agregar una linea en `registry.py` - nada mas
del servicio necesita cambiar, igual filosofia que `ProviderRegistry` en
Nexolu IA Core.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from nexolu_comms_api.core.auth.apps import AppIdentity

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class OutboundMessage:
    """Mensaje neutral: el mismo objeto se le pasa a cualquier canal, cada
    uno usa los campos que le aplican e ignora el resto (p.ej. `subject` no
    significa nada para WhatsApp)."""

    to: str
    subject: str | None = None
    text: str | None = None
    html: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] = field(default_factory=list)
    # marketing | utility | authentication | service - solo WhatsApp lo usa,
    # para estimar costo por categoria (Meta cobra distinto segun cual sea).
    category: str | None = None
    # WhatsApp Flow (formulario nativo): confirma un borrador de escritura
    # sin salir del canal. `flow_token` es responsabilidad de la app
    # llamante (p.ej. el id de un borrador propio) - este servicio nunca lo
    # interpreta, solo lo reenvia tal cual a Meta.
    flow_id: str | None = None
    flow_screen: str | None = None
    flow_cta: str | None = None
    flow_token: str | None = None
    flow_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelSendResult:
    status: str  # sent | failed | skipped
    provider_message_id: str | None = None
    # None cuando el proveedor no informa costo para este envio - no es lo
    # mismo que "cost=0" (ver Notification.cost_micros).
    cost_micros: int | None = None
    error: str | None = None


class ChannelSender(ABC):
    name: str

    @abstractmethod
    async def send(self, app: AppIdentity, message: OutboundMessage) -> ChannelSendResult: ...
