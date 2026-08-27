"""Acceso a datos de `CommsApp`/`ProviderCredential` (identidad de apps y
credenciales de proveedor).

Separado de `NotificationRepository` (core/db/repository.py) a proposito:
esa clase es sobre el log de envios, esta es sobre administracion/identidad
de apps - ciclos de vida y consumidores distintos (el admin de plataforma
contra esta, el envio de mensajes contra la otra).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.db.entities import CommsApp, ProviderCredential
from nexolu_comms_api.core.security.api_keys import generate_api_key, hash_api_key


class CommsAppRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[CommsApp]:
        stmt = select(CommsApp).order_by(CommsApp.app_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_by_app_id(self, app_id: str) -> CommsApp | None:
        stmt = select(CommsApp).where(CommsApp.app_id == app_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_active_by_api_key_hash(self, api_key_hash: str) -> CommsApp | None:
        stmt = select(CommsApp).where(CommsApp.api_key_hash == api_key_hash, CommsApp.is_active.is_(True))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create(self, **fields: Any) -> CommsApp:
        app = CommsApp(**fields)
        self._session.add(app)
        await self._session.flush()
        return app

    async def update(self, app: CommsApp, **fields: Any) -> CommsApp:
        """Aplica `fields` tal cual (el caller ya filtro lo que no vino en el
        patch, ver `CommsAppUpdate.model_dump(exclude_unset=True)`)."""
        for key, value in fields.items():
            setattr(app, key, value)
        await self._session.flush()
        return app

    async def regenerate_key(self, app: CommsApp) -> CommsApp:
        new_key = generate_api_key()
        app.api_key = new_key
        app.api_key_hash = hash_api_key(new_key)
        await self._session.flush()
        return app


class ProviderCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, app_id: str, provider_slug: str) -> ProviderCredential | None:
        stmt = select(ProviderCredential).where(
            ProviderCredential.app_id == app_id,
            ProviderCredential.provider_slug == provider_slug,
            ProviderCredential.is_active.is_(True),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_app(self, app_id: str) -> list[ProviderCredential]:
        stmt = select(ProviderCredential).where(
            ProviderCredential.app_id == app_id, ProviderCredential.is_active.is_(True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert(
        self, *, app_id: str, provider_slug: str, config: dict[str, Any], secrets: dict[str, Any]
    ) -> ProviderCredential:
        """Crea la credencial de este proveedor para esta app si no existe,
        o la sobreescribe por completo si ya existe - "rotar" un token de
        Meta o una key de Brevo es siempre pegar el valor nuevo que el
        operador ya genero en el dashboard del proveedor (ninguno de los dos
        expone una API para generarlo), asi que no hace falta un endpoint de
        "rotate" separado de "configure"."""
        credential = await self.get_active(app_id, provider_slug)
        if credential is None:
            credential = ProviderCredential(app_id=app_id, provider_slug=provider_slug, config=config, secrets=secrets)
            self._session.add(credential)
        else:
            credential.config = config
            credential.secrets = secrets
        await self._session.flush()
        return credential
