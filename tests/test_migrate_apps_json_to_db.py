from __future__ import annotations

from nexolu_comms_api.core.auth.repository import CommsAppRepository, ProviderCredentialRepository
from nexolu_comms_api.core.db.session import get_sessionmaker, init_models


async def _run_migration():
    from scripts.migrate_apps_json_to_db import migrate

    await migrate()


async def test_backfill_creates_pos_with_its_existing_api_key(app_env):
    await _run_migration()

    async with get_sessionmaker()() as session:
        app = await CommsAppRepository(session).get_by_app_id("pos")
        assert app is not None
        assert app.api_key == "dev-pos-key"  # preserva la key existente, no genera una nueva

        credentials = await ProviderCredentialRepository(session).list_for_app(app.id)
        slugs = {c.provider_slug for c in credentials}
        assert slugs == {"meta_whatsapp", "brevo"}

        whatsapp = next(c for c in credentials if c.provider_slug == "meta_whatsapp")
        assert whatsapp.config["phone_number_id"] == "123456"
        assert whatsapp.secrets["access_token"] == "wa-token"


async def test_backfill_is_idempotent(app_env):
    await _run_migration()
    await _run_migration()  # correrlo de nuevo no debe duplicar ni fallar

    async with get_sessionmaker()() as session:
        apps = await CommsAppRepository(session).list_all()
        assert [a.app_id for a in apps] == ["pos"]


async def test_backfill_does_not_touch_an_app_already_created_via_admin(app_env, monkeypatch):
    await init_models()

    async with get_sessionmaker()() as session:
        repo = CommsAppRepository(session)
        await repo.create(app_id="pos", api_key="already-rotated-via-admin")
        await session.commit()

    await _run_migration()

    async with get_sessionmaker()() as session:
        app = await CommsAppRepository(session).get_by_app_id("pos")
        assert app.api_key == "already-rotated-via-admin"  # el backfill no lo sobreescribe
