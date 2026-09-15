"""Gestion de usuarios del panel Connect. SOLO plataforma: crear/editar
usuarios (y decidir quien es cliente de que negocio) es potestad del admin
de Nexolu - no hay self-signup todavia, a proposito.

La contraseña es opcional al crear: un usuario sin ella entra unicamente
por SSO (auth.nexolu.co). Se acepta en texto plano SOLO en este payload
(viaja por TLS, se hashea con bcrypt de inmediato y jamas se devuelve) -
mismo trato que el password del login.
"""
from __future__ import annotations

from datetime import datetime

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.dependencies import require_platform_access
from nexolu_comms_api.core.auth.panel import ROLE_CLIENT, ROLE_PLATFORM, PanelUserRepository
from nexolu_comms_api.core.auth.repository import CommsAppRepository
from nexolu_comms_api.core.db.entities import PanelMembership, PanelUser
from nexolu_comms_api.core.db.session import get_session

router = APIRouter(
    prefix="/v1/admin/users",
    tags=["admin"],
    dependencies=[Depends(require_platform_access)],
)

_ROLES = (ROLE_PLATFORM, ROLE_CLIENT)


class PanelUserIn(BaseModel):
    # Sin EmailStr para no arrastrar email-validator: la validacion util
    # aca es "tiene pinta de correo", el resto lo decide quien lo usa.
    email: str = Field(min_length=3, max_length=191, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    full_name: str = ""
    role: str = Field(default=ROLE_CLIENT, pattern="^(platform|client)$")
    password: str | None = Field(default=None, min_length=8, max_length=128)
    # Apps (negocios) del usuario. Solo tiene sentido para `client`; para
    # `platform` se ignora (su acceso es total, no una lista).
    app_ids: list[str] = Field(default_factory=list)


class PanelUserPatch(BaseModel):
    full_name: str | None = None
    role: str | None = Field(default=None, pattern="^(platform|client)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    app_ids: list[str] | None = None


class PanelUserAdminOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    has_password: bool  # false = solo puede entrar por SSO
    app_ids: list[str]
    last_login_at: datetime | None
    created_at: datetime


class PanelUserListOut(BaseModel):
    items: list[PanelUserAdminOut]


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _to_out(user: PanelUser) -> PanelUserAdminOut:
    return PanelUserAdminOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        has_password=bool(user.password_hash),
        app_ids=[m.app_id for m in user.memberships],
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


async def _validate_app_ids(session: AsyncSession, app_ids: list[str]) -> None:
    """Una membresia a una app inexistente no es un error inofensivo: es un
    usuario que cree tener un negocio y ve pantallas vacias sin causa."""
    repo = CommsAppRepository(session)
    for app_id in app_ids:
        if await repo.get_by_app_id(app_id) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"La app '{app_id}' no existe.",
            )


@router.get("", response_model=PanelUserListOut)
async def list_users(session: AsyncSession = Depends(get_session)) -> PanelUserListOut:
    users = await PanelUserRepository(session).list_all()
    return PanelUserListOut(items=[_to_out(u) for u in users])


@router.post("", response_model=PanelUserAdminOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: PanelUserIn, session: AsyncSession = Depends(get_session)) -> PanelUserAdminOut:
    repo = PanelUserRepository(session)
    email = payload.email.strip().lower()

    if await repo.get_by_email(email) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"'{email}' ya es usuario del panel.")

    app_ids = payload.app_ids if payload.role == ROLE_CLIENT else []
    await _validate_app_ids(session, app_ids)

    user = PanelUser(
        email=email,
        full_name=payload.full_name,
        role=payload.role,
        password_hash=_hash(payload.password) if payload.password else None,
        memberships=[PanelMembership(app_id=app_id) for app_id in dict.fromkeys(app_ids)],
    )
    session.add(user)
    await session.commit()

    return _to_out(user)


@router.patch("/{user_id}", response_model=PanelUserAdminOut)
async def update_user(
    user_id: str, payload: PanelUserPatch, session: AsyncSession = Depends(get_session)
) -> PanelUserAdminOut:
    repo = PanelUserRepository(session)
    user = await repo.get(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario desconocido.")

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password is not None:
        user.password_hash = _hash(payload.password)

    if payload.app_ids is not None or (payload.role == ROLE_PLATFORM):
        # Cambiar a `platform` limpia membresias (no aplican); para `client`
        # la lista enviada REEMPLAZA la actual - editar es declarar el
        # estado final, no un diff.
        new_ids = [] if user.role == ROLE_PLATFORM else list(dict.fromkeys(payload.app_ids or []))
        await _validate_app_ids(session, new_ids)
        user.memberships = [PanelMembership(app_id=app_id) for app_id in new_ids]

    await session.commit()
    refreshed = await repo.get(user.id)
    assert refreshed is not None
    return _to_out(refreshed)
