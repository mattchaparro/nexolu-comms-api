"""Suscripciones Web Push del panel: el celular de cada persona.

Las da de alta el navegador de quien atiende el chat, despues de que
acepto el permiso (connect.nexolu.co, boton de la campana). Son de una
PERSONA, asi que exigen su sesion: ni la platform key ni la bandeja
embebida tienen un celular. Quien recibe que se decide al enviar (ver
core/push.py), no aca.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.dependencies import get_panel_identity
from nexolu_comms_api.core.auth.panel import PanelIdentity
from nexolu_comms_api.core.db.entities import PushSubscription
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.push import is_configured, send_web_push

router = APIRouter(prefix="/v1/push", tags=["push"])


class PublicKeyOut(BaseModel):
    enabled: bool
    public_key: str


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=64)


class SubscriptionIn(BaseModel):
    # La forma de PushSubscription.toJSON() del navegador, tal cual.
    endpoint: str = Field(min_length=10, max_length=512)
    keys: SubscriptionKeys
    content_encoding: str = Field(default="aes128gcm", max_length=16)


class SubscriptionDeleteIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=512)


class SubscriptionOut(BaseModel):
    subscribed: bool


class TestOut(BaseModel):
    sent: int


def _require_configured() -> None:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Las notificaciones no estan configuradas (faltan las llaves VAPID).",
        )


@router.get("/public-key", response_model=PublicKeyOut)
async def public_key() -> PublicKeyOut:
    """La llave publica VAPID: el navegador la necesita para suscribirse.
    Publica por definicion -- viaja en cada suscripcion."""
    settings = get_settings()
    return PublicKeyOut(enabled=is_configured(), public_key=settings.vapid_public_key if is_configured() else "")


@router.put("/subscriptions", response_model=SubscriptionOut)
async def save_subscription(
    payload: SubscriptionIn,
    user_agent: str | None = Header(default=None),
    identity: PanelIdentity = Depends(get_panel_identity),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionOut:
    """Alta o puesta al dia por `endpoint`. Si otra persona se suscribe
    desde el mismo navegador, la fila pasa a ser suya: el celular es de
    quien tiene la sesion abierta ahora."""
    _require_configured()
    if not payload.endpoint.startswith("https://"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Endpoint invalido.")

    row = (
        await session.execute(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))
    ).scalar_one_or_none()
    if row is None:
        row = PushSubscription(endpoint=payload.endpoint)
        session.add(row)

    row.user_email = identity.email
    row.p256dh = payload.keys.p256dh
    row.auth = payload.keys.auth
    row.content_encoding = payload.content_encoding or "aes128gcm"
    row.user_agent = (user_agent or "")[:255]
    await session.commit()
    return SubscriptionOut(subscribed=True)


@router.delete("/subscriptions", response_model=SubscriptionOut)
async def delete_subscription(
    payload: SubscriptionDeleteIn,
    identity: PanelIdentity = Depends(get_panel_identity),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionOut:
    """Desactivar desde el boton, o al cerrar sesion: en el celular del
    mostrador no deben seguir llegando los mensajes de quien ya se fue."""
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == payload.endpoint,
            PushSubscription.user_email == identity.email,
        )
    )
    await session.commit()
    return SubscriptionOut(subscribed=False)


@router.post("/test", response_model=TestOut)
async def send_test(
    identity: PanelIdentity = Depends(get_panel_identity),
    session: AsyncSession = Depends(get_session),
) -> TestOut:
    """Un aviso de prueba a los celulares de quien lo pide: para saber que
    quedo bien sin esperar a que alguien escriba."""
    _require_configured()
    subs = list(
        (
            await session.execute(select(PushSubscription).where(PushSubscription.user_email == identity.email))
        ).scalars()
    )
    data = {
        "title": "Nexolú Connect",
        "body": "Así te van a llegar los mensajes nuevos.",
        "tag": "connect-test",
        "url": "/chat",
    }
    results = await asyncio.gather(*(asyncio.to_thread(send_web_push, sub, data) for sub in subs))
    gone = [sub.id for sub, r in zip(subs, results, strict=True) if r.status_code in (404, 410)]
    if gone:
        await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
        await session.commit()
    return TestOut(sent=sum(1 for r in results if 200 <= r.status_code < 300))
