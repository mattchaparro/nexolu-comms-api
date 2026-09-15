"""Onboarding de numeros propios por negocio, via Embedded Signup.

El flujo completo (analisis, seccion I): el front de la app (POS/Spa) abre
el popup de Embedded Signup con la config de login de la plataforma; el
popup devuelve un `code` + los ids de la WABA y el numero que el cliente
eligio; la app se los manda a este endpoint, que hace el lado servidor
contra Meta (intercambiar el code por el token del cliente, suscribir la
app de plataforma a la WABA, registrar el numero con PIN) y guarda el
`BusinessChannel`. Desde ese momento, los envios de esa app con ese
`business_id` salen por el numero propio del negocio.

Autentica la APP llamante (no la plataforma): conectar un negocio es una
operacion de la app duena, iniciada desde su propio panel de configuracion.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.channels.business_channels import (
    STATUS_ACTIVE,
    BusinessChannelRepository,
)
from nexolu_comms_api.core.db.entities import BusinessChannel
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.meta.graph import MetaGraphClient, MetaGraphError

router = APIRouter(prefix="/v1/onboarding/whatsapp", tags=["onboarding"])
logger = logging.getLogger(__name__)


class OnboardingConfigOut(BaseModel):
    """Lo que el front de una app necesita para abrir el popup de Embedded
    Signup. Ninguno de estos valores es secreto (van en el JS del navegador
    de todas formas)."""

    configured: bool
    meta_app_id: str | None = None
    login_config_id: str | None = None


class CompleteSignupIn(BaseModel):
    business_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, description="El code que devuelve el popup de Embedded Signup.")
    waba_id: str = Field(min_length=1)
    phone_number_id: str = Field(min_length=1)
    display_phone_number: str | None = None


class ChannelStatusOut(BaseModel):
    business_id: str
    status: str  # pending | active | disconnected | not_connected
    waba_id: str | None = None
    phone_number_id: str | None = None
    display_phone_number: str | None = None
    catalog_id: str | None = None
    last_error: str | None = None
    connected_at: datetime | None = None


def _to_status(business_id: str, channel: BusinessChannel | None) -> ChannelStatusOut:
    if channel is None:
        return ChannelStatusOut(business_id=business_id, status="not_connected")
    return ChannelStatusOut(
        business_id=business_id,
        status=channel.status,
        waba_id=channel.waba_id,
        phone_number_id=channel.phone_number_id,
        display_phone_number=channel.display_phone_number,
        catalog_id=channel.catalog_id,
        last_error=channel.last_error,
        connected_at=channel.connected_at,
    )


@router.get("/config", response_model=OnboardingConfigOut)
async def get_config(_: AppIdentity = Depends(get_current_app)) -> OnboardingConfigOut:
    settings = get_settings()
    configured = bool(settings.meta_platform_app_id and settings.meta_platform_app_secret)
    return OnboardingConfigOut(
        configured=configured,
        meta_app_id=settings.meta_platform_app_id or None,
        login_config_id=settings.meta_login_config_id or None,
    )


@router.get("/channels/{business_id}", response_model=ChannelStatusOut)
async def get_channel_status(
    business_id: str,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> ChannelStatusOut:
    channel = await BusinessChannelRepository(session).get_for_business(app.app_id, business_id)
    return _to_status(business_id, channel)


@router.post("/complete", response_model=ChannelStatusOut)
async def complete_signup(
    payload: CompleteSignupIn,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> ChannelStatusOut:
    settings = get_settings()
    if not (settings.meta_platform_app_id and settings.meta_platform_app_secret):
        raise HTTPException(
            status_code=503,
            detail="La App Meta de plataforma no esta configurada (META_PLATFORM_APP_ID/SECRET).",
        )

    repo = BusinessChannelRepository(session)
    existing = await repo.get_for_business(app.app_id, payload.business_id)

    graph = MetaGraphClient(settings)
    # El PIN de dos pasos lo fija este servicio y se conserva cifrado: hace
    # falta de nuevo para re-registrar o migrar el numero. Al reconectar un
    # canal existente se reusa el suyo.
    pin = existing.pin if existing and existing.pin else f"{secrets.randbelow(1_000_000):06d}"

    try:
        access_token = await graph.exchange_code(payload.code)
        await graph.subscribe_app(payload.waba_id, access_token)
        await graph.register_phone(payload.phone_number_id, access_token, pin)
    except MetaGraphError as exc:
        logger.warning(
            "onboarding.meta_rejected",
            extra={"app_id": app.app_id, "business_id": payload.business_id, "detail": exc.detail},
        )
        raise HTTPException(status_code=502, detail=exc.detail) from exc

    if existing is None:
        channel = BusinessChannel(
            app_id=app.app_id,
            business_id=payload.business_id,
            waba_id=payload.waba_id,
            phone_number_id=payload.phone_number_id,
            display_phone_number=payload.display_phone_number,
            access_token=access_token,
            pin=pin,
            status=STATUS_ACTIVE,
            connected_at=datetime.utcnow(),
        )
        session.add(channel)
    else:
        # Reconexion (o re-signup con otra WABA/numero): la fila se conserva
        # - el historial de envios/webhooks de ese negocio no queda huerfano.
        channel = existing
        channel.waba_id = payload.waba_id
        channel.phone_number_id = payload.phone_number_id
        channel.display_phone_number = payload.display_phone_number
        channel.access_token = access_token
        channel.pin = pin
        channel.status = STATUS_ACTIVE
        channel.last_error = None
        channel.connected_at = datetime.utcnow()
        channel.disconnected_at = None

    await session.commit()
    logger.info(
        "onboarding.channel_connected",
        extra={"app_id": app.app_id, "business_id": payload.business_id, "channel_id": channel.id},
    )

    return _to_status(payload.business_id, channel)
