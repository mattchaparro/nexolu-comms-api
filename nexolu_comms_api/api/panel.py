"""Sesion del panel Connect (connect.nexolu.co, repo nexolu-comms-front).

Tres formas de entrar, todas terminando en el MISMO JWT propio:

- **SSO con nexolu-auth** (`POST /auth/sso/exchange`): el camino de todos
  los dias. La asercion viene firmada por auth.nexolu.co y se verifica
  100% local (core/auth/sso.py); el email resultante tiene que existir
  como usuario del panel (o ser el operador de emergencia) - nexolu-auth
  autentica QUIEN ES, este servicio decide SI PUEDE ENTRAR y con que rol.
- **Login local** (`POST /auth/login`): contraseña bcrypt de un PanelUser,
  o el break-glass de env (PANEL_EMAIL/PANEL_PASSWORD_HASH) que funciona
  aunque la BD este vacia o nexolu-auth caido.
- El JWT resultante lleva el email en `sub`; rol y membresias se resuelven
  contra la BD EN CADA REQUEST (core/auth/panel.py), nunca desde claims.

Solo sesion aca: lo demas que el panel consume son los endpoints de
plataforma/scope existentes (`/v1/admin/*`, `/v1/platform/*`).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.panel import (
    PanelIdentity,
    PanelUserRepository,
    resolve_identity_by_email,
    resolve_identity_by_token,
)
from nexolu_comms_api.core.auth.sso import (
    AssertionNotConfiguredError,
    InvalidAssertionError,
    verify_assertion,
)
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.security.panel import create_panel_token, verify_password

router = APIRouter(prefix="/panel", tags=["panel"])
logger = logging.getLogger(__name__)

_bearer = HTTPBearer(description="JWT emitido por POST /panel/auth/login o /panel/auth/sso/exchange.")


class LoginRequest(BaseModel):
    email: str
    password: str


class SsoExchangeRequest(BaseModel):
    assertion: str


class PanelUserOut(BaseModel):
    email: str
    full_name: str
    roles: list[str]
    # Solo para clientes externos: las apps (negocios) a las que pertenecen.
    # Vacia para plataforma - su acceso es total, no una lista.
    app_ids: list[str]


class LoginResponse(BaseModel):
    token: str
    user: PanelUserOut


def _user_out(identity: PanelIdentity) -> PanelUserOut:
    return PanelUserOut(
        email=identity.email,
        full_name=identity.full_name,
        roles=[identity.role],
        app_ids=list(identity.app_ids),
    )


@router.post("/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> LoginResponse:
    settings = get_settings()
    if not settings.panel_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El panel no esta configurado (falta PANEL_JWT_SECRET).",
        )

    email = payload.email.strip().lower()

    # Un solo mensaje generico para email-no-existe, clave-incorrecta y
    # usuario-sin-contraseña (solo-SSO) - no se regala cual de los tres fue.
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Las credenciales son incorrectas."
    )

    is_emergency = bool(settings.panel_email) and email == settings.panel_email.strip().lower()
    if is_emergency:
        if not verify_password(payload.password, settings.panel_password_hash):
            logger.warning("panel.login_failed", extra={"kind": "emergency"})
            raise unauthorized
    else:
        user = await PanelUserRepository(session).get_by_email(email)
        if (
            user is None
            or not user.is_active
            or not user.password_hash
            or not verify_password(payload.password, user.password_hash)
        ):
            logger.warning("panel.login_failed", extra={"kind": "user"})
            raise unauthorized

    identity = await resolve_identity_by_email(session, email)
    assert identity is not None  # recien autenticado arriba
    await session.commit()  # persiste last_login_at

    return LoginResponse(token=create_panel_token(identity.email), user=_user_out(identity))


@router.post("/auth/sso/exchange", response_model=LoginResponse)
async def sso_exchange(
    payload: SsoExchangeRequest, session: AsyncSession = Depends(get_session)
) -> LoginResponse:
    """Canjea una asercion de nexolu-auth por el token propio del panel.

    Publico a proposito: la asercion ES la credencial (firmada, 120 s).

    El 403 es TERMINAL para el front: esa identidad no es usuaria de este
    panel y reintentar el SSO devolveria lo mismo - rebotar a nexolu-auth
    seria un bucle sin formulario (mismo aprendizaje de nexolu-admin).
    """
    settings = get_settings()
    if not settings.panel_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El panel no esta configurado (falta PANEL_JWT_SECRET).",
        )

    try:
        claims = verify_assertion(payload.assertion)
    except AssertionNotConfiguredError as error:
        # 503 y no 401: el SSO no esta habilitado en este ambiente; el
        # login local sigue funcionando, que es justo el punto.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except InvalidAssertionError as error:
        logger.warning("panel.sso_invalid_assertion", extra={"detail": str(error)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="La asercion no es valida."
        ) from error

    email = (claims.get("email") or "").strip().lower()
    identity = await resolve_identity_by_email(session, email) if email else None

    if identity is None:
        logger.warning("panel.sso_unknown_identity", extra={"email": email})
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esa identidad no tiene acceso a este panel.",
        )

    await session.commit()  # persiste last_login_at
    logger.info("panel.sso_exchange_ok", extra={"email": email, "role": identity.role})

    return LoginResponse(token=create_panel_token(identity.email), user=_user_out(identity))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> None:
    # Token stateless: no hay nada que revocar, el front lo descarta.
    return None


@router.get("/me", response_model=PanelUserOut)
async def me(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> PanelUserOut:
    identity = await resolve_identity_by_token(session, credentials.credentials)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesion invalida o expirada.")
    return _user_out(identity)
