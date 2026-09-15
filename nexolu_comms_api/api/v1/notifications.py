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
from nexolu_comms_api.core.db.entities import IdempotencyRecord
from nexolu_comms_api.core.db.repository import NotificationRepository
from nexolu_comms_api.core.db.session import get_session

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

    for channel_name in payload.channels:
        recipient = payload.to.get(channel_name)
        effective_app = whatsapp_identity if channel_name == "whatsapp" else app
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

    flow = payload.whatsapp_flow
    message = OutboundMessage(
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
    )

    try:
        return await sender.send(app, message)
    except Exception as exc:
        logger.exception("notifications.channel_error", extra={"app_id": app.app_id, "channel": channel_name})
        return ChannelSendResult(status=STATUS_FAILED, error=f"Error inesperado en el canal '{channel_name}': {exc}")
