"""Envio multi-canal en una sola llamada: la app cliente indica en `channels`
cuales canales quiere usar para la MISMA notificacion, y cada uno se procesa
de forma independiente - un canal fallido o sin configurar no afecta a los
demas, y la respuesta trae un resultado por canal.

Producto-agnostico a proposito: este servicio no sabe que es una "alerta de
inventario bajo" ni un "recordatorio de cita" - solo sabe enviar texto/html/
plantilla a un destinatario por canal. El significado del mensaje lo arma
la app llamante.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.channels.base import STATUS_FAILED, ChannelSendResult, OutboundMessage
from nexolu_comms_api.core.channels.business_channels import (
    BusinessChannelRepository,
    resolve_whatsapp_identity,
)
from nexolu_comms_api.core.channels.exceptions import UnknownChannelError
from nexolu_comms_api.core.channels.registry import get_channel_registry
from nexolu_comms_api.core.chats import log_outbound_chat, resolve_contact_for_send
from nexolu_comms_api.core.db.entities import IdempotencyRecord
from nexolu_comms_api.core.db.repository import NotificationRepository
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.templates.service import TemplateRepository

router = APIRouter(prefix="/v1", tags=["notifications"])
logger = logging.getLogger(__name__)


class WhatsAppTemplateIn(BaseModel):
    """Plantilla aprobada en Meta para enviar fuera de la ventana de 24h.
    Sin esto, WhatsApp solo entrega `text` si el usuario le escribio a la
    app en las ultimas 24h."""

    name: str
    language: str = "es"
    components: list[dict] = Field(default_factory=list)


class WhatsAppFlowIn(BaseModel):
    """Formulario nativo de WhatsApp, para confirmar un borrador de
    escritura sin salir del canal - ver App\\Jobs\\ProcessWhatsAppFlowReply
    en Nexolu POS. `flow_token` es responsabilidad de la app llamante (p.ej.
    el id de un borrador propio en su servicio de IA); este servicio nunca
    lo interpreta, solo lo reenvia tal cual a Meta y lo recibe de vuelta sin
    tocarlo cuando el usuario responde (ver GET/POST /webhooks/whatsapp)."""

    flow_id: str
    screen: str
    cta: str
    flow_token: str
    data: dict = Field(default_factory=dict)


class WhatsAppProductIn(BaseModel):
    """Producto individual (SPM). `catalog_id` opcional: sin el, se usa el
    catalogo conectado de la identidad que envia (app o negocio)."""

    product_retailer_id: str
    catalog_id: str | None = None
    footer: str | None = None


class WhatsAppProductsIn(BaseModel):
    """Multiples productos (MPM): hasta 30 en total, en secciones. Header
    y body (el `text` del request) son obligatorios segun Meta."""

    header: str
    sections: list[dict] = Field(min_length=1, description="[{'title': ..., 'product_retailer_ids': [...]}]")
    catalog_id: str | None = None
    footer: str | None = None


class WhatsAppCatalogIn(BaseModel):
    """Catalogo completo (catalog_message): invita a explorar todo."""

    thumbnail_product_retailer_id: str | None = None
    footer: str | None = None


class WhatsAppOptionIn(BaseModel):
    id: str = Field(min_length=1, max_length=191)
    title: str = Field(min_length=1, max_length=24)  # tope de Meta
    description: str | None = Field(default=None, max_length=72)


class WhatsAppCtaIn(BaseModel):
    """Un boton que abre un enlace, debajo de `text`.

    Para lo que la clienta tiene que ABRIR, no contestar: el Instagram del
    salon en la confirmacion de la cita. Pegado como texto era un enlace
    largo al final del mensaje; en ManyChat era un boton, y lo notaron.
    WhatsApp corta el titulo a 20 caracteres.
    """

    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(min_length=1, max_length=20)


class WhatsAppOptionsIn(BaseModel):
    """Opciones tocables: botones (hasta 3) o lista (hasta 10).

    Existe para que el BOT de una app pueda ofrecer horas o servicios sin
    obligar a la clienta a escribirlas -- hasta ahora eso solo lo podian
    hacer los flujos, y el bot solo mandaba texto. Elegir por toque es la
    diferencia entre agendar y abandonar.

    La respuesta vuelve por el webhook como el TITULO tocado, asi que la
    app la lee igual que si lo hubieran escrito.
    """

    options: list[WhatsAppOptionIn] = Field(min_length=1, max_length=10)
    # Solo para lista (4+ opciones): el texto del boton que la abre.
    button: str = Field(default="Ver opciones", max_length=20)


class SendRequest(BaseModel):
    # Clave de particion OPACA que la app define para agrupar sus propios
    # reportes de uso (ver GET /v1/usage/*) - este servicio nunca la valida
    # contra nada propio, no tiene que significar "negocio" literal. Una app
    # sin concepto de tenant (un solo cliente, sin sub-negocios) puede
    # omitirla: cae al propio app_id, así toda su actividad queda bajo una
    # sola particion en vez de fallar por falta de un dato que no le aplica.
    business_id: str | None = Field(
        default=None, description="Clave de particion para reportes de uso. Si se omite, cae al app_id de quien llama."
    )
    # Identificador libre de la app llamante para agrupar/rastrear sus
    # propios envios (p.ej. "low_stock_alert:456") - este servicio no le da
    # significado, solo lo guarda.
    reference: str | None = None
    channels: list[str] = Field(min_length=1, description="Canales a usar para esta notificacion, p.ej. ['whatsapp', 'email'].")
    to: dict[str, str] = Field(description="Destinatario por canal: {'whatsapp': '+573...', 'email': 'a@b.com'}.")
    subject: str | None = Field(default=None, description="Usado solo por el canal email.")
    text: str | None = Field(default=None, description="Texto libre. WhatsApp solo lo entrega dentro de la ventana de 24h.")
    html: str | None = Field(default=None, description="HTML del correo. Si falta, se envia `text` envuelto en <p>.")
    category: str | None = Field(
        default=None, description="Categoria de plantilla de WhatsApp: marketing|utility|authentication|service."
    )
    whatsapp_template: WhatsAppTemplateIn | None = None
    whatsapp_flow: WhatsAppFlowIn | None = None
    whatsapp_options: WhatsAppOptionsIn | None = None
    whatsapp_cta: WhatsAppCtaIn | None = None
    whatsapp_product: WhatsAppProductIn | None = None
    whatsapp_products: WhatsAppProductsIn | None = None
    whatsapp_catalog: WhatsAppCatalogIn | None = None


class ChannelResultOut(BaseModel):
    channel: str
    status: str
    provider_message_id: str | None = None
    cost_micros: int | None = None
    error: str | None = None


class SendResponse(BaseModel):
    reference: str | None
    business_id: str  # ya resuelto: nunca None en la respuesta, ver send_notification().
    results: list[ChannelResultOut]


class ChannelsResponse(BaseModel):
    channels: list[str]


@router.get("/channels", response_model=ChannelsResponse)
async def list_channels() -> ChannelsResponse:
    return ChannelsResponse(channels=get_channel_registry().available())


@router.post("/notifications/send", response_model=SendResponse)
async def send_notification(
    payload: SendRequest,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=191),
) -> SendResponse:
    # Idempotencia opt-in por header: la app que reintenta tras un timeout
    # manda la misma clave y recibe la respuesta original, sin enviar nada
    # de nuevo. Sin header, el comportamiento es el de siempre.
    if idempotency_key:
        stored = await _stored_response(session, app.app_id, idempotency_key)
        if stored is not None:
            return stored

    registry = get_channel_registry()
    repo = NotificationRepository(session)
    results: list[ChannelResultOut] = []
    business_id = payload.business_id or app.app_id

    # Numero propio del negocio, si lo tiene (Embedded Signup): el canal de
    # WhatsApp envia con ESA identidad; sin canal propio, con la compartida
    # de la app - el comportamiento historico. Ver core/channels/business_channels.py.
    whatsapp_identity, own_channel = await resolve_whatsapp_identity(session, app, business_id)

    # Aviso temprano del espejo de plantillas: si la plantilla pedida esta
    # en el espejo y NO esta aprobada, se corta antes de llamar a Meta (un
    # envio masivo con una plantilla PAUSED fallaria mensaje a mensaje,
    # gastando rate limit y tiempo). Si el espejo no la conoce, se envia
    # igual - el espejo es opt-in, no un registro obligatorio.
    template_block = await _template_block_reason(session, app, whatsapp_identity, payload)

    for channel_name in payload.channels:
        recipient = payload.to.get(channel_name)
        effective_app = whatsapp_identity if channel_name == "whatsapp" else app
        if channel_name == "whatsapp" and template_block is not None:
            result = ChannelSendResult(status=STATUS_FAILED, error=template_block)
        else:
            result = await _send_one(registry, effective_app, channel_name, recipient, payload)

        if (
            own_channel is not None
            and channel_name == "whatsapp"
            and result.provider_status_code == 401
        ):
            # Token del negocio revocado (p.ej. desde Meta Business Suite):
            # no es un bug, es un estado - el canal queda desconectado y el
            # panel/la app pueden ofrecer reconectar.
            BusinessChannelRepository(session).mark_disconnected(
                own_channel, "Meta rechazo el token (401) al enviar."
            )
            logger.warning(
                "notifications.business_channel_disconnected",
                extra={"app_id": app.app_id, "business_id": business_id, "channel_id": own_channel.id},
            )

        repo.log(
            app_id=app.app_id,
            business_id=business_id,
            channel=channel_name,
            recipient=recipient or "",
            status=result.status,
            reference=payload.reference,
            provider_message_id=result.provider_message_id,
            error=result.error,
            cost_micros=result.cost_micros,
        )
        if channel_name == "whatsapp" and recipient:
            # La bandeja: lo que la app envia por API (el bot del spa, un
            # recordatorio) tambien se lee en el hilo del contacto. El
            # negocio del contacto usa la convencion del motor ("" para el
            # numero compartido sin negocio declarado), NO el fallback
            # app_id de las Notifications. Fail-soft: la bandeja jamas
            # tumba un envio.
            try:
                contact = await resolve_contact_for_send(
                    session, app.app_id, payload.business_id or "", recipient
                )
                log_outbound_chat(
                    session,
                    contact=contact,
                    message=_build_message(recipient, payload),
                    result=result,
                    origin="api",
                )
            except Exception:
                logger.exception("notifications.chat_log_failed", extra={"app_id": app.app_id})
        results.append(
            ChannelResultOut(
                channel=channel_name,
                status=result.status,
                provider_message_id=result.provider_message_id,
                cost_micros=result.cost_micros,
                error=result.error,
            )
        )

    await session.commit()

    response = SendResponse(reference=payload.reference, business_id=business_id, results=results)

    if idempotency_key:
        await _store_response(session, app.app_id, idempotency_key, response)

    return response


async def _stored_response(session: AsyncSession, app_id: str, key: str) -> SendResponse | None:
    row = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.app_id == app_id, IdempotencyRecord.idempotency_key == key
            )
        )
    ).scalar_one_or_none()
    return SendResponse.model_validate_json(row.response_body) if row else None


async def _store_response(session: AsyncSession, app_id: str, key: str, response: SendResponse) -> None:
    """Se guarda DESPUES de enviar: dos llamadas simultaneas con la misma
    clave son una carrera que la restriccion unica resuelve - la segunda
    insercion falla y no pasa nada (el envio de esa segunda llamada ya
    ocurrio de todas formas; la idempotencia protege el caso real, que es
    el retry SECUENCIAL tras un timeout)."""
    session.add(
        IdempotencyRecord(app_id=app_id, idempotency_key=key, response_body=response.model_dump_json())
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def _template_block_reason(
    session: AsyncSession, app: AppIdentity, effective: AppIdentity, payload: SendRequest
) -> str | None:
    if payload.whatsapp_template is None:
        return None
    waba_id = effective.whatsapp.waba_id if effective.whatsapp else None
    row = await TemplateRepository(session).get_for_send(
        app.app_id, waba_id, payload.whatsapp_template.name, payload.whatsapp_template.language
    )
    if row is None or row.status == "APPROVED":
        return None
    detail = f" ({row.reason})" if row.reason else ""
    return (
        f"La plantilla '{row.name}' ({row.language}) esta en estado {row.status}{detail} - "
        "no se envio para no quemar el rate limit contra un rechazo seguro."
    )


async def _send_one(
    registry, app: AppIdentity, channel_name: str, recipient: str | None, payload: SendRequest
) -> ChannelSendResult:
    """Nunca lanza: un canal desconocido, sin destinatario, o que se cae, es
    UN resultado fallido dentro de la respuesta - nunca tumba el resto de
    los canales de la misma notificacion."""
    if not recipient:
        return ChannelSendResult(status=STATUS_FAILED, error=f"Falta el destinatario del canal '{channel_name}' en 'to'.")

    try:
        sender = registry.resolve(channel_name)
    except UnknownChannelError as exc:
        return ChannelSendResult(status=STATUS_FAILED, error=str(exc))

    message = _build_message(recipient, payload)

    try:
        return await sender.send(app, message)
    except Exception as exc:
        logger.exception("notifications.channel_error", extra={"app_id": app.app_id, "channel": channel_name})
        return ChannelSendResult(status=STATUS_FAILED, error=f"Error inesperado en el canal '{channel_name}': {exc}")


def _build_message(recipient: str, payload: SendRequest) -> OutboundMessage:
    """Un solo lugar arma el OutboundMessage: lo usan el envio y el registro
    del hilo en la bandeja (la burbuja debe pintar LO QUE se envio)."""
    flow = payload.whatsapp_flow
    product = payload.whatsapp_product
    products = payload.whatsapp_products
    catalog = payload.whatsapp_catalog
    options = payload.whatsapp_options
    # Meta manda botones hasta 3 y lista de 4 en adelante: son dos payloads
    # distintos, pero para quien llama es lo mismo ("dale a elegir esto").
    as_buttons = bool(options) and len(options.options) <= 3
    return OutboundMessage(
        to=recipient,
        subject=payload.subject,
        text=payload.text,
        html=payload.html,
        category=payload.category,
        template_name=payload.whatsapp_template.name if payload.whatsapp_template else None,
        template_language=payload.whatsapp_template.language if payload.whatsapp_template else None,
        template_components=payload.whatsapp_template.components if payload.whatsapp_template else [],
        flow_id=flow.flow_id if flow else None,
        flow_screen=flow.screen if flow else None,
        flow_cta=flow.cta if flow else None,
        flow_token=flow.flow_token if flow else None,
        flow_data=flow.data if flow else {},
        buttons=[{"id": o.id, "title": o.title} for o in options.options] if as_buttons else [],
        list_rows=[
            {"id": o.id, "title": o.title, **({"description": o.description} if o.description else {})}
            for o in options.options
        ]
        if options and not as_buttons
        else [],
        list_button=options.button if options and not as_buttons else None,
        cta_url=payload.whatsapp_cta.url if payload.whatsapp_cta else None,
        cta_title=payload.whatsapp_cta.title if payload.whatsapp_cta else None,
        product_retailer_id=product.product_retailer_id if product else None,
        product_sections=products.sections if products else [],
        product_header=products.header if products else None,
        product_footer=(product.footer if product else None)
        or (products.footer if products else None)
        or (catalog.footer if catalog else None),
        catalog_id=(product.catalog_id if product else None) or (products.catalog_id if products else None),
        send_catalog=catalog is not None,
        catalog_thumbnail_retailer_id=catalog.thumbnail_product_retailer_id if catalog else None,
    )
