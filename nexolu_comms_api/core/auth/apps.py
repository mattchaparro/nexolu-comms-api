"""Identidad de las aplicaciones cliente (POS, Spa, EasyTickets...).

Este servicio no tiene usuarios ni tenants propios: sus unicos "clientes
autenticados" son las aplicaciones que lo llaman. Cada una tiene una API key
y, opcionalmente, sus propias credenciales de WhatsApp/email.

Fuente de verdad: la BD (`CommsApp`/`ProviderCredential`, ver
core/db/entities.py y core/auth/repository.py). Durante la ventana de
transicion desde `NEXOLU_APPS_JSON` (antes de que el backfill
`scripts/migrate_apps_json_to_db.py` haya corrido en todos los ambientes),
si una app no se encuentra en BD se cae de vuelta a resolverla desde el
registro legado en memoria - logueando un warning cada vez, para que sea
visible en logs si a algun ambiente le falta el backfill. Ese fallback se
elimina en una fase posterior, una vez confirmado en todos los ambientes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import (
    EmailAppConfig,
    InstagramAppConfig,
    Settings,
    WhatsAppAppConfig,
    get_settings,
)
from nexolu_comms_api.core.auth.repository import CommsAppRepository, ProviderCredentialRepository
from nexolu_comms_api.core.db.entities import CommsApp, ProviderCredential
from nexolu_comms_api.core.security.api_keys import hash_api_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppIdentity:
    app_id: str
    api_key: str
    name: str
    whatsapp: WhatsAppAppConfig | None
    email: EmailAppConfig | None
    instagram: InstagramAppConfig | None = None


def _whatsapp_config(credential: ProviderCredential) -> WhatsAppAppConfig:
    return WhatsAppAppConfig(
        phone_number_id=credential.config["phone_number_id"],
        access_token=credential.secrets["access_token"],
        waba_id=credential.config.get("waba_id"),
        webhook_verify_token=credential.secrets.get("webhook_verify_token"),
        meta_app_secret=credential.secrets.get("meta_app_secret"),
        enforce_meta_signature=bool(credential.config.get("enforce_meta_signature", False)),
        callback_secret=credential.secrets.get("callback_secret"),
        callback_url=credential.config.get("callback_url"),
    )


def _instagram_config(credential: ProviderCredential) -> InstagramAppConfig:
    return InstagramAppConfig(
        ig_user_id=credential.config["ig_user_id"],
        access_token=credential.secrets["access_token"],
        username=credential.config.get("username"),
    )


def _email_config(credential: ProviderCredential) -> EmailAppConfig:
    return EmailAppConfig(
        from_email=credential.config["from_email"],
        from_name=credential.config.get("from_name", ""),
        brevo_api_key=credential.secrets.get("brevo_api_key"),
    )


async def _build_identity(session: AsyncSession, app: CommsApp) -> AppIdentity:
    credentials = await ProviderCredentialRepository(session).list_for_app(app.id)
    whatsapp: WhatsAppAppConfig | None = None
    email: EmailAppConfig | None = None
    instagram: InstagramAppConfig | None = None
    for credential in credentials:
        if credential.provider_slug == "meta_whatsapp":
            whatsapp = _whatsapp_config(credential)
        elif credential.provider_slug == "brevo":
            email = _email_config(credential)
        elif credential.provider_slug == "meta_instagram":
            instagram = _instagram_config(credential)
    return AppIdentity(
        app_id=app.app_id,
        api_key=app.api_key,
        name=app.name or app.app_id,
        whatsapp=whatsapp,
        email=email,
        instagram=instagram,
    )


async def resolve_by_api_key(session: AsyncSession, api_key: str) -> AppIdentity | None:
    app = await CommsAppRepository(session).get_active_by_api_key_hash(hash_api_key(api_key))
    if app is not None:
        return await _build_identity(session, app)

    legacy = _legacy_registry().resolve_by_api_key(api_key)
    if legacy is not None:
        logger.warning("apps.resolved_via_env_json_fallback", extra={"app_id": legacy.app_id})
    return legacy


async def resolve_by_app_id(session: AsyncSession, app_id: str) -> AppIdentity | None:
    app = await CommsAppRepository(session).get_by_app_id(app_id)
    if app is not None and app.is_active:
        return await _build_identity(session, app)

    legacy = _legacy_registry().resolve_by_app_id(app_id)
    if legacy is not None:
        logger.warning("apps.resolved_via_env_json_fallback", extra={"app_id": app_id})
    return legacy


class _LegacyAppRegistry:
    """Registro en memoria construido desde `NEXOLU_APPS_JSON` - solo se usa
    como fallback (ver docstring del modulo) mientras el backfill a BD no
    haya corrido en un ambiente dado."""

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self._by_api_key: dict[str, AppIdentity] = {}
        self._by_app_id: dict[str, AppIdentity] = {}

        for app_id, registration in settings.apps.items():
            identity = AppIdentity(
                app_id=app_id,
                api_key=registration.api_key,
                name=registration.name or app_id,
                whatsapp=registration.whatsapp,
                email=registration.email,
            )
            self._by_api_key[registration.api_key] = identity
            self._by_app_id[app_id] = identity

    def resolve_by_api_key(self, api_key: str) -> AppIdentity | None:
        return self._by_api_key.get(api_key)

    def resolve_by_app_id(self, app_id: str) -> AppIdentity | None:
        return self._by_app_id.get(app_id)


_legacy: _LegacyAppRegistry | None = None


def _legacy_registry() -> _LegacyAppRegistry:
    global _legacy
    if _legacy is None:
        _legacy = _LegacyAppRegistry()
    return _legacy


def reset_legacy_registry() -> None:
    """Solo para tests: fuerza a reconstruir el registro legado en el
    proximo uso (equivalente a lo que antes hacia `apps_module._registry =
    None` en conftest)."""
    global _legacy
    _legacy = None
