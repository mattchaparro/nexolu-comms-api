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
    # None = ve todos los negocios de sus apps. Una tupla = solo esos: la
    # recepcionista de un salon que entro desde el Spa (ver PanelMembership).
    business_ids: tuple[str, ...] | None = None
    # La app de donde viene (usuarios que entran con pase, ver PanelUser).
    origin_app_id: str | None = None

    @property
    def is_platform(self) -> bool:
        return self.role == ROLE_PLATFORM

    @property
    def scope(self) -> PanelScope:
        if self.is_platform:
            return UNRESTRICTED_SCOPE
        return PanelScope(app_ids=self.app_ids, business_ids=self.business_ids)


@dataclass(frozen=True)
class PanelScope:
    """app_ids=None -> acceso total; lista -> solo esas apps.

    `business_ids` recorta un escalon mas adentro y existe por el chat
    embebido: el panel del Spa muestra la bandeja de Connect dentro de su
    propia pantalla, y ahi quien mira no es "el que administra la app
    spa" sino UN salon. Sin este segundo filtro, embeber la bandeja en
    Luxury Nails le mostraria las conversaciones de todos los spas del
    sistema.

    None = sin restriccion de negocio (el panel de Nexolu, que si los ve
    todos). Una lista = solo esos, y la lista vacia no ve nada -- fallar
    cerrado, igual que con las apps.
    """

    app_ids: tuple[str, ...] | None
    business_ids: tuple[str, ...] | None = None

    def allows(self, app_id: str) -> bool:
        return self.app_ids is None or app_id in self.app_ids

    def allows_business(self, business_id: str | None) -> bool:
        if self.business_ids is None:
            return True
        # Un contacto sin negocio no pertenece a ninguno, asi que no se le
        # muestra a quien solo puede ver el suyo.
        return business_id is not None and business_id in self.business_ids

    def allows_contact(self, app_id: str, business_id: str | None) -> bool:
        return self.allows(app_id) and self.allows_business(business_id)

    @property
    def is_business_restricted(self) -> bool:
        return self.business_ids is not None

    def allows_shared(self, app_id: str, business_id: str | None) -> bool:
        """Para cosas que pueden ser de toda la app o de un negocio
        (respuestas rapidas, avisos): lo de toda la app ("") lo ve
        cualquiera con la app; lo de un negocio, solo quien ve ese negocio."""
        if not self.allows(app_id):
            return False
        return not business_id or self.allows_business(business_id)


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
    # Si CUALQUIER membresia es de un negocio, el recorte por negocio
    # aplica a todas: mezclar "toda la app X" con "solo el salon Y de la
    # app Z" no cabe en un PanelScope, y ante la duda se falla cerrado.
    business_ids = tuple(m.business_id for m in user.memberships if m.business_id)
    return PanelIdentity(
        email=user.email,
        full_name=user.full_name or user.email,
        role=user.role,
        app_ids=tuple(m.app_id for m in user.memberships),
        user_id=user.id,
        business_ids=business_ids or None,
        origin_app_id=user.origin_app_id,
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


def embed_scope_from_token(token: str) -> PanelScope | None:
    """Token de bandeja embebida -> alcance de UN negocio dentro de UNA app.

    Devuelve None si el token no es de este tipo, para que quien llama
    siga probando las otras puertas. No toca la BD a proposito: lo que
    autoriza no es quien es una persona sino que el Spa -- ya autenticado
    con su API key cuando pidio este token -- dijo que ese negocio puede
    ver su propia bandeja.
    """
    try:
        claims = decode_panel_token(token)
    except InvalidPanelTokenError:
        return None

    if claims.get("typ") != "embed":
        return None

    app_id = str(claims.get("app") or "")
    business_id = str(claims.get("biz") or "")
    if not app_id or not business_id:
        return None

    return PanelScope(app_ids=(app_id,), business_ids=(business_id,))


async def identity_by_email(session: AsyncSession, email: str) -> PanelIdentity | None:
    """Identidad vigente de un email ya autenticado, SIN efectos (no toca
    last_login_at): para decidir a quien le llega un push."""
    settings = get_settings()
    if settings.panel_email and email.strip().lower() == settings.panel_email.strip().lower():
        return _emergency_identity()

    user = await PanelUserRepository(session).get_by_email(email)
    if user is None or not user.is_active:
        return None
    return identity_for_user(user)


async def resolve_identity_by_token(session: AsyncSession, token: str) -> PanelIdentity | None:
    """JWT de panel -> identidad vigente. Se resuelve contra la BD en cada
    request (no contra los claims): desactivar un usuario o quitarle una
    membresia surte efecto de inmediato, no cuando expire su token."""
    try:
        claims = decode_panel_token(token)
    except InvalidPanelTokenError:
        return None

    # Un token de bandeja embebida NO es una sesion de panel. Entra por el
    # mismo header, asi que se rechaza aqui explicitamente: si algun dia
    # existiera un usuario cuyo email coincidiera con su `sub`, heredaria
    # el alcance de esa persona sin que nadie lo notara.
    if claims.get("typ") == "embed":
        return None

    email = str(claims.get("sub") or "")
    if not email:
        return None

    return await identity_by_email(session, email)
