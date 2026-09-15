"""Identidad y alcance de los usuarios del panel Connect.

Dos sujetos posibles detras de un JWT de panel:

- El **operador de emergencia** (PANEL_EMAIL de env): no vive en BD, tiene
  rol `platform` implicito. Es el break-glass que funciona con la BD vacia
  - por eso el bootstrap de usuarios no tiene el problema del huevo y la
  gallina: el admin entra con el, y crea a los demas desde el panel.
- Un **PanelUser de BD**: `platform` (admin de Nexolu, ve todo) o `client`
  (negocio externo, ve SOLO las apps de sus `panel_memberships`).

`PanelScope` es lo que consumen los endpoints: `app_ids=None` significa
"sin restriccion" (plataforma o platform key server-side); una lista
significa "solo estas". La regla de oro del scoping: el filtro se aplica
SIEMPRE del lado del servidor - el front del cliente puede pedir lo que
quiera, la respuesta ya viene recortada.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.db.entities import PanelUser
from nexolu_comms_api.core.security.panel import (
    InvalidPanelTokenError,
    decode_panel_token,
)

ROLE_PLATFORM = "platform"
ROLE_CLIENT = "client"


@dataclass(frozen=True)
class PanelIdentity:
    email: str
    full_name: str
    role: str
    # Solo para `client`: apps a las que pertenece. Para `platform` queda
    # vacia (su acceso no se expresa como lista, es total).
    app_ids: tuple[str, ...] = ()
    user_id: str | None = None  # None = operador de emergencia (env)

    @property
    def is_platform(self) -> bool:
        return self.role == ROLE_PLATFORM


@dataclass(frozen=True)
class PanelScope:
    """app_ids=None -> acceso total; lista -> solo esas apps."""

    app_ids: tuple[str, ...] | None

    def allows(self, app_id: str) -> bool:
        return self.app_ids is None or app_id in self.app_ids


UNRESTRICTED_SCOPE = PanelScope(app_ids=None)


class PanelUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: str) -> PanelUser | None:
        return (
            await self._session.execute(
                select(PanelUser).options(selectinload(PanelUser.memberships)).where(PanelUser.id == user_id)
            )
        ).scalar_one_or_none()

    async def get_by_email(self, email: str) -> PanelUser | None:
        return (
            await self._session.execute(
                select(PanelUser)
                .options(selectinload(PanelUser.memberships))
                .where(PanelUser.email == email.strip().lower())
            )
        ).scalar_one_or_none()

    async def list_all(self) -> list[PanelUser]:
        return list(
            (
                await self._session.execute(
                    select(PanelUser).options(selectinload(PanelUser.memberships)).order_by(PanelUser.created_at)
                )
            ).scalars()
        )


def identity_for_user(user: PanelUser) -> PanelIdentity:
    return PanelIdentity(
        email=user.email,
        full_name=user.full_name or user.email,
        role=user.role,
        app_ids=tuple(m.app_id for m in user.memberships),
        user_id=user.id,
    )


def _emergency_identity() -> PanelIdentity:
    settings = get_settings()
    return PanelIdentity(
        email=settings.panel_email,
        full_name=settings.panel_full_name,
        role=ROLE_PLATFORM,
        user_id=None,
    )


async def resolve_identity_by_email(session: AsyncSession, email: str) -> PanelIdentity | None:
    """Email (ya autenticado por quien llama: password valido o asercion
    SSO verificada) -> identidad. None = nadie con ese email puede entrar."""
    settings = get_settings()
    normalized = email.strip().lower()

    if settings.panel_email and normalized == settings.panel_email.strip().lower():
        return _emergency_identity()

    user = await PanelUserRepository(session).get_by_email(normalized)
    if user is None or not user.is_active:
        return None

    user.last_login_at = datetime.utcnow()
    return identity_for_user(user)


async def resolve_identity_by_token(session: AsyncSession, token: str) -> PanelIdentity | None:
    """JWT de panel -> identidad vigente. Se resuelve contra la BD en cada
    request (no contra los claims): desactivar un usuario o quitarle una
    membresia surte efecto de inmediato, no cuando expire su token."""
    try:
        claims = decode_panel_token(token)
    except InvalidPanelTokenError:
        return None

    email = str(claims.get("sub") or "")
    if not email:
        return None

    settings = get_settings()
    if settings.panel_email and email.strip().lower() == settings.panel_email.strip().lower():
        return _emergency_identity()

    user = await PanelUserRepository(session).get_by_email(email)
    if user is None or not user.is_active:
        return None
    return identity_for_user(user)
