"""Acceso a datos de `BusinessChannel` y su traduccion a la config que el
canal de WhatsApp ya sabe usar.

La decision central esta en `resolve_whatsapp_identity()`: el envio de una
app con `business_id` mira primero si ese negocio tiene numero PROPIO
(canal activo) y solo si no, cae al numero compartido de la app - el
comportamiento de siempre. Los canales de envio (`core/channels/whatsapp.py`)
no saben que esto existe: reciben un `AppIdentity` ya resuelto.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import WhatsAppAppConfig
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.db.entities import BusinessChannel

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISCONNECTED = "disconnected"


class BusinessChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, channel_id: str) -> BusinessChannel | None:
        return await self._session.get(BusinessChannel, channel_id)

    async def get_for_business(self, app_id: str, business_id: str) -> BusinessChannel | None:
        return (
            await self._session.execute(
                select(BusinessChannel).where(
                    BusinessChannel.app_id == app_id, BusinessChannel.business_id == business_id
                )
            )
        ).scalar_one_or_none()

    async def get_active_for_business(self, app_id: str, business_id: str) -> BusinessChannel | None:
        channel = await self.get_for_business(app_id, business_id)
        return channel if channel is not None and channel.status == STATUS_ACTIVE else None

    async def get_by_phone_number_id(self, phone_number_id: str) -> BusinessChannel | None:
        # Sin filtrar por status: un canal desconectado puede seguir
        # recibiendo webhooks (la suscripcion de la app Meta a la WABA es
        # independiente del token) y la app duena los sigue queriendo.
        return (
            await self._session.execute(
                select(BusinessChannel).where(BusinessChannel.phone_number_id == phone_number_id)
            )
        ).scalar_one_or_none()

    async def list_channels(self, app_id: str | None = None) -> list[BusinessChannel]:
        query = select(BusinessChannel).order_by(BusinessChannel.created_at.desc())
        if app_id:
            query = query.where(BusinessChannel.app_id == app_id)
        return list((await self._session.execute(query)).scalars())

    def mark_disconnected(self, channel: BusinessChannel, reason: str) -> None:
        channel.status = STATUS_DISCONNECTED
        channel.last_error = reason
        channel.disconnected_at = datetime.utcnow()


def whatsapp_identity_for(app: AppIdentity, channel: BusinessChannel) -> AppIdentity:
    """El mismo AppIdentity, con el numero/token del canal del negocio en
    lugar del compartido de la app. Los campos de webhook (callback_url,
    callback_secret) se heredan de la app: el reenvio de eventos sigue
    llegando al mismo endpoint de la app duena."""
    base = app.whatsapp
    return replace(
        app,
        whatsapp=WhatsAppAppConfig(
            phone_number_id=channel.phone_number_id,
            access_token=channel.access_token,
            waba_id=channel.waba_id,
            webhook_verify_token=base.webhook_verify_token if base else None,
            meta_app_secret=base.meta_app_secret if base else None,
            enforce_meta_signature=base.enforce_meta_signature if base else False,
            catalog_id=channel.catalog_id,
            callback_secret=base.callback_secret if base else None,
            callback_url=base.callback_url if base else None,
        ),
    )


async def resolve_whatsapp_identity(
    session: AsyncSession, app: AppIdentity, business_id: str
) -> tuple[AppIdentity, BusinessChannel | None]:
    """@return (identidad efectiva para enviar, canal propio si se uso)."""
    channel = await BusinessChannelRepository(session).get_active_for_business(app.app_id, business_id)
    if channel is None:
        return app, None
    return whatsapp_identity_for(app, channel), channel
