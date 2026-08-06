"""Identidad de las aplicaciones cliente (POS, Spa, EasyTickets...).

Este servicio no tiene usuarios ni tenants propios: sus unicos "clientes
autenticados" son las aplicaciones que lo llaman. Cada una tiene una API key
y, opcionalmente, sus propias credenciales de WhatsApp/email - ver
App\\Services\\Messaging\\Contracts\\MessagingChannel en el POS, el contrato
local que este servicio termina reemplazando por app.
"""
from __future__ import annotations

from dataclasses import dataclass

from nexolu_comms_api.config import EmailAppConfig, Settings, WhatsAppAppConfig, get_settings


@dataclass(frozen=True)
class AppIdentity:
    app_id: str
    api_key: str
    name: str
    whatsapp: WhatsAppAppConfig | None
    email: EmailAppConfig | None


class AppRegistry:
    """Resuelve una API key (llamadas salientes de una app) o un app_id
    (webhooks entrantes de Meta, que no traen ninguna API key nuestra - ver
    core/webhooks/whatsapp.py) a la identidad de la app."""

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


_registry: AppRegistry | None = None


def get_app_registry() -> AppRegistry:
    global _registry
    if _registry is None:
        _registry = AppRegistry()
    return _registry
