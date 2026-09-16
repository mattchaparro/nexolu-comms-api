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
    # Mensaje interactivo de botones de respuesta (reply buttons, max 3 por
    # regla de Meta): [{"id": ..., "title": ...}]. Usado por el motor de
    # flujos (core/flows) y disponible tambien para las apps.
    buttons: list[dict[str, str]] = field(default_factory=list)
    # Mensaje interactivo con boton de URL (cta_url): el "gestionar tu cita
    # desde la web" - abre el link sin salir del chat.
    cta_url: str | None = None
    cta_title: str | None = None
    # Mensajes de producto (catalogo conectado a la WABA; payloads oficiales
    # en el analisis, seccion H). `catalog_id` puede omitirse: el canal usa
    # el de la identidad efectiva (app o canal del negocio).
    product_retailer_id: str | None = None  # SPM: un producto
    # MPM: hasta 30 productos en secciones [{"title", "product_retailer_ids": [...]}]
    product_sections: list[dict[str, Any]] = field(default_factory=list)
    product_header: str | None = None
    product_footer: str | None = None
    catalog_id: str | None = None
    # Catalogo completo (interactive.catalog_message)
    send_catalog: bool = False
    catalog_thumbnail_retailer_id: str | None = None
    # Multimedia por LINK publico (Meta lo descarga por su cuenta, nunca se
    # le suben bytes): image | video | audio | document. `caption` aplica a
    # image/video/document; `filename` solo a document.
    media_kind: str | None = None
    media_url: str | None = None
    media_caption: str | None = None
    media_filename: str | None = None
    # Mensaje de lista (interactive.list): un menu de hasta 10 opciones.
    # `list_button` es el texto del boton que despliega la lista (max 20).
    list_button: str | None = None
    list_rows: list[dict[str, str]] = field(default_factory=list)  # {"id","title","description"?}


@dataclass(frozen=True)
class ChannelSendResult:
    status: str  # sent | failed | skipped
    provider_message_id: str | None = None
    # None cuando el proveedor no informa costo para este envio - no es lo
    # mismo que "cost=0" (ver Notification.cost_micros).
    cost_micros: int | None = None
    error: str | None = None
    # Codigo HTTP que respondio el proveedor cuando el envio fallo por
    # rechazo (no por red). Existe para que quien llama distinga un 401
    # (token revocado -> desconectar el canal del negocio) de un 4xx/5xx
    # cualquiera, sin parsear el texto del error.
    provider_status_code: int | None = None


class ChannelSender(ABC):
    name: str

    @abstractmethod
    async def send(self, app: AppIdentity, message: OutboundMessage) -> ChannelSendResult: ...
