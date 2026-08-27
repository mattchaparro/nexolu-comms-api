from __future__ import annotations

import pytest

from nexolu_comms_api.core.auth.repository import CommsAppRepository, ProviderCredentialRepository
from nexolu_comms_api.core.db.session import get_sessionmaker, init_models
from nexolu_comms_api.core.security.api_keys import hash_api_key


@pytest.fixture
async def session(app_env):
    await init_models()
    async with get_sessionmaker()() as db_session:
        yield db_session


async def test_create_and_get_by_app_id(session):
    repo = CommsAppRepository(session)
    app = await repo.create(app_id="spa", name="Nexolu Spa")
    await session.commit()

    fetched = await repo.get_by_app_id("spa")
    assert fetched is not None
    assert fetched.api_key == app.api_key  # descifrado de forma transparente
    assert fetched.api_key.startswith("ncm_")


async def test_get_active_by_api_key_hash(session):
    repo = CommsAppRepository(session)
    await repo.create(app_id="spa", name="Nexolu Spa", api_key="ncm_fixed-key")
    await session.commit()

    found = await repo.get_active_by_api_key_hash(hash_api_key("ncm_fixed-key"))
    assert found is not None
    assert found.app_id == "spa"

    assert await repo.get_active_by_api_key_hash(hash_api_key("wrong")) is None


async def test_regenerate_key_overwrites_immediately(session):
    repo = CommsAppRepository(session)
    app = await repo.create(app_id="spa", api_key="ncm_old-key")
    await session.commit()
    old_hash = app.api_key_hash

    await repo.regenerate_key(app)
    await session.commit()

    assert app.api_key != "ncm_old-key"
    assert app.api_key_hash != old_hash
    assert await repo.get_active_by_api_key_hash(hash_api_key("ncm_old-key")) is None


async def test_provider_credential_upsert_creates_then_overwrites(session):
    app_repo = CommsAppRepository(session)
    cred_repo = ProviderCredentialRepository(session)
    app = await app_repo.create(app_id="spa")
    await session.commit()

    created = await cred_repo.upsert(
        app_id=app.id,
        provider_slug="meta_whatsapp",
        config={"phone_number_id": "1"},
        secrets={"access_token": "t1"},
    )
    await session.commit()
    assert created.secrets["access_token"] == "t1"

    updated = await cred_repo.upsert(
        app_id=app.id,
        provider_slug="meta_whatsapp",
        config={"phone_number_id": "2"},
        secrets={"access_token": "t2"},
    )
    await session.commit()

    assert updated.id == created.id  # mismo row, no uno nuevo
    assert updated.config["phone_number_id"] == "2"
    assert updated.secrets["access_token"] == "t2"

    active = await cred_repo.list_for_app(app.id)
    assert len(active) == 1
