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


def create_panel_token(email: str, ttl_hours: int | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    hours = ttl_hours if ttl_hours is not None else settings.panel_jwt_ttl_hours
    payload = {"sub": email, "iat": now, "exp": now + timedelta(hours=hours)}
    return jwt.encode(payload, settings.panel_jwt_secret, algorithm=_ALGORITHM)


def create_embed_token(app_id: str, business_id: str, ttl_minutes: int) -> str:
    """Token para la bandeja EMBEBIDA en el panel de otra app.

    No es una sesion de panel y por eso lleva `typ`: el sujeto no es una
    persona con email sino un negocio concreto dentro de una app, y lo
    unico que puede hacer es mirar y contestar SUS conversaciones. Si un
    token de estos se colara por la puerta del panel normal, su portador
    heredaria el alcance de un administrador; el `typ` es lo que hace que
    esa confusion no pueda ocurrir en silencio.

    Vida corta a proposito: viaja en la URL de un iframe, que es el peor
    sitio donde puede estar un token -- queda en el historial del
    navegador y en el `Referer`. El panel que lo embebe lo renueva.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "typ": "embed",
        "sub": f"{app_id}:{business_id}",
        "app": app_id,
        "biz": business_id,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
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
