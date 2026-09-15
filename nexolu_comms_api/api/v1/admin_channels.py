"""Gestion de canales por negocio (`business_channels`) desde el panel.

Protegido con `require_platform_access`, como el resto de /v1/admin/*.
Nunca devuelve el access_token ni el PIN: no hay endpoint de "reveal" aca a
proposito - a diferencia de las credenciales por app (que el operador pego a
mano y podria necesitar recuperar), estas las obtuvo el servicio via
Embedded Signup y ningun humano las necesita ver; si se pierden, el camino
es reconectar el canal, no copiar el token viejo.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.channels.business_channels import (
    STATUS_DISCONNECTED,
    BusinessChannelRepository,
)
from nexolu_comms_api.core.db.entities import BusinessChannel
from nexolu_comms_api.core.db.session import get_session

# Autorizado por SCOPE: un cliente externo del panel Connect solo ve/opera
# los canales de SUS apps; lo ajeno responde 404, como si no existiera.
router = APIRouter(prefix="/v1/admin/business-channels", tags=["admin"])


class BusinessChannelOut(BaseModel):
    id: str
    app_id: str
    business_id: str
    waba_id: str
    phone_number_id: str
    display_phone_number: str | None
    catalog_id: str | None
    status: str
    last_error: str | None
    connected_at: datetime | None
    disconnected_at: datetime | None
    created_at: datetime


class BusinessChannelListOut(BaseModel):
    items: list[BusinessChannelOut]


def _to_out(channel: BusinessChannel) -> BusinessChannelOut:
    return BusinessChannelOut(
        id=channel.id,
        app_id=channel.app_id,
        business_id=channel.business_id,
        waba_id=channel.waba_id,
        phone_number_id=channel.phone_number_id,
        display_phone_number=channel.display_phone_number,
        catalog_id=channel.catalog_id,
        status=channel.status,
        last_error=channel.last_error,
        connected_at=channel.connected_at,
        disconnected_at=channel.disconnected_at,
        created_at=channel.created_at,
    )


@router.get("", response_model=BusinessChannelListOut)
async def list_channels(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> BusinessChannelListOut:
    channels = await BusinessChannelRepository(session).list_channels(app_id)
    channels = [c for c in channels if scope.allows(c.app_id)]
    return BusinessChannelListOut(items=[_to_out(c) for c in channels])


@router.get("/{channel_id}", response_model=BusinessChannelOut)
async def get_channel(
    channel_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> BusinessChannelOut:
    channel = await BusinessChannelRepository(session).get(channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Canal desconocido.")
    require_scope_for_app(scope, channel.app_id)
    return _to_out(channel)


@router.post("/{channel_id}/disconnect", response_model=BusinessChannelOut)
async def disconnect_channel(
    channel_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> BusinessChannelOut:
    """Desconexion MANUAL desde el panel: los envios de ese negocio vuelven
    de inmediato al numero compartido de su app (o fallan si no hay). No
    toca nada en Meta - revocar el acceso de verdad se hace desde el Meta
    Business Suite del cliente; esto solo deja de usar el canal. Reconectar
    es repetir el Embedded Signup (la fila se conserva)."""
    repo = BusinessChannelRepository(session)
    channel = await repo.get(channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Canal desconocido.")
    require_scope_for_app(scope, channel.app_id)
    if channel.status == STATUS_DISCONNECTED:
        return _to_out(channel)

    repo.mark_disconnected(channel, "Desconectado manualmente desde el panel.")
    await session.commit()
    return _to_out(channel)
