"""Verificacion LOCAL de las aserciones que emite nexolu-auth
(auth.nexolu.co), para el login SSO del panel Connect.

Puerto directo de `nexolu-admin/app/auth/nexolu_auth.py`, incluida su
restriccion mas dura: NO llama a nexolu-auth ni a nadie. La llave publica
viaja fijada en NEXOLU_AUTH_PUBLIC_KEYS ({kid: PEM en base64}); aceptar
varios kids permite rotar sin downtime. Vacia por defecto = el canje
responde 503 y el login local (break-glass + usuarios de BD con
contraseña) sigue funcionando intacto.

La audiencia es `nexolu-connect` (Settings.nexolu_auth_audience): una
asercion emitida para nexolu-admin o para el POS NO sirve aca - cada
producto es una audiencia distinta en nexolu-auth.
"""
from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any

import jwt

from nexolu_comms_api.config import get_settings

_ALGORITHM = "RS256"

# Margen para deriva de reloj entre este droplet y el de nexolu-auth.
_LEEWAY_SECONDS = 60


class AssertionNotConfiguredError(Exception):
    """NEXOLU_AUTH_PUBLIC_KEYS vacio: SSO no habilitado en este ambiente.
    Es un 503, no un 401."""


class InvalidAssertionError(Exception):
    """Firma, emisor, audiencia, tipo o vigencia incorrectos."""


def _public_keys() -> dict[str, str]:
    raw = get_settings().nexolu_auth_public_keys or "{}"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AssertionNotConfiguredError(
            'NEXOLU_AUTH_PUBLIC_KEYS no es un JSON valido. Se espera {"kid": "<PEM en base64>"}.'
        ) from error

    keys: dict[str, str] = {}
    for kid, encoded in parsed.items():
        try:
            keys[kid] = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as error:
            raise AssertionNotConfiguredError(
                f"La llave publica '{kid}' de NEXOLU_AUTH_PUBLIC_KEYS no es un PEM en base64."
            ) from error

    return keys


class _ReplayGuard:
    """Impide que la MISMA asercion se canjee dos veces (vive 120 s en el
    fragmento de la URL y queda en el historial del navegador). En memoria
    porque el servicio corre en un solo proceso uvicorn."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def check_and_remember(self, jti: str, expires_at: float) -> bool:
        now = time.time()
        self._seen = {key: exp for key, exp in self._seen.items() if exp > now}

        if jti in self._seen:
            return False

        self._seen[jti] = expires_at
        return True


_replay_guard = _ReplayGuard()


def verify_assertion(assertion: str) -> dict[str, Any]:
    """Devuelve los claims si la asercion es valida para ESTE panel."""
    settings = get_settings()
    keys = _public_keys()

    if not keys:
        raise AssertionNotConfiguredError(
            "El acceso con Nexolu no esta configurado (falta NEXOLU_AUTH_PUBLIC_KEYS)."
        )

    try:
        kid = jwt.get_unverified_header(assertion).get("kid")
    except jwt.PyJWTError as error:
        raise InvalidAssertionError(str(error)) from error

    public_key = keys.get(kid)
    if public_key is None:
        raise InvalidAssertionError(f"kid desconocido: {kid!r}")

    try:
        claims = jwt.decode(
            assertion,
            public_key,
            # Lista explicita: impide alg:none o HS256 con la llave publica
            # como secreto.
            algorithms=[_ALGORITHM],
            audience=settings.nexolu_auth_audience,
            issuer=settings.nexolu_auth_issuer,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
        )
    except jwt.PyJWTError as error:
        raise InvalidAssertionError(str(error)) from error

    if claims.get("typ") != "sso":
        raise InvalidAssertionError("La asercion no es de tipo 'sso'.")

    if not _replay_guard.check_and_remember(claims["jti"], float(claims["exp"])):
        raise InvalidAssertionError("Esta asercion ya se uso.")

    return claims
