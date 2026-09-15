"""Sesion del panel Connect: bcrypt + JWT HS256, verificado 100% local
(porta `app/auth/security.py` de nexolu-admin).

El `sub` del token es el email del usuario - puede ser el operador de
emergencia (PANEL_EMAIL de env, el break-glass que funciona con la BD
vacia) o un `PanelUser` de BD (admin de plataforma o cliente externo).
QUIEN es y QUE puede hacer no se decide aca: eso lo resuelve
core/auth/panel.py contra la BD en cada request, para que desactivar un
usuario surta efecto de inmediato y no cuando expire su token.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from nexolu_comms_api.config import get_settings

_ALGORITHM = "HS256"


class InvalidPanelTokenError(Exception):
    """Token ausente, mal firmado, expirado o de otro sujeto."""


def verify_password(password: str, password_hash: str) -> bool:
    # Hash vacio (PANEL_PASSWORD_HASH sin configurar) siempre rechaza en vez
    # de lanzar - falla cerrado, no expone un 500.
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_panel_token(email: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {"sub": email, "iat": now, "exp": now + timedelta(hours=settings.panel_jwt_ttl_hours)}
    return jwt.encode(payload, settings.panel_jwt_secret, algorithm=_ALGORITHM)


def decode_panel_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.panel_jwt_secret:
        raise InvalidPanelTokenError("El panel no esta configurado (falta PANEL_JWT_SECRET).")
    try:
        payload = jwt.decode(token, settings.panel_jwt_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as error:
        raise InvalidPanelTokenError(str(error)) from error

    return payload
