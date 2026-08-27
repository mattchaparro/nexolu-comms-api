"""Cifrado en reposo de credenciales sensibles (api_key de apps, credenciales
de proveedor como el access token de WhatsApp Cloud API o la API key de
Brevo).

Puerto directo de `nexolu_ia_core/core/security/crypto.py` (que a su vez es
puerto de `nexolu_payments_core/core/security/crypto.py`): Fernet (AES128 +
HMAC, con rotacion de nonce por valor) usando una unica clave de proceso
(`COMMS_MASTER_KEY`), nunca guardada en la base. Un dump de la BD o un acceso
de solo lectura mal configurado no debe entregar esos secretos en texto
plano.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from nexolu_comms_api.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().comms_master_key
    if not key:
        raise RuntimeError(
            "COMMS_MASTER_KEY no esta configurada: no se pueden leer ni "
            "escribir credenciales de apps/proveedores. Generarla con "
            "`python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"`.'
        )
    return Fernet(key.encode())


class EncryptedString(TypeDecorator):
    """Columna String que cifra al escribir y descifra al leer, de forma
    transparente para el resto del codigo (los modelos la usan como un
    `Mapped[str]` normal)."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return _fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return _fernet().decrypt(value.encode()).decode()


class EncryptedJSON(TypeDecorator):
    """Igual que `EncryptedString`, pero serializa/deserializa un dict antes
    de cifrar - usado para el blob `secrets` de `ProviderCredential`, donde
    cada proveedor (meta_whatsapp/brevo) guarda un conjunto distinto de
    campos secretos bajo una sola columna."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect) -> str | None:
        if value is None:
            return None
        return _fernet().encrypt(json.dumps(value).encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> dict[str, Any] | None:
        if value is None:
            return None
        return json.loads(_fernet().decrypt(value.encode()).decode())
