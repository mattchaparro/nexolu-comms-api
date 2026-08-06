"""Fixtures compartidas.

`Settings`, el engine de BD y el registro de canales/apps estan cacheados
con `lru_cache` (a proposito: son singletons de proceso en produccion). Para
que cada test corra aislado con su propio `DATABASE_URL` y su propio
registro de apps, este fixture limpia esos caches antes y despues de cada
test - mismo patron que Nexolu IA Core.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

POS_API_KEY = "dev-pos-key"


@pytest.fixture(autouse=True)
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv(
        "NEXOLU_APPS_JSON",
        json.dumps(
            {
                "pos": {
                    "api_key": POS_API_KEY,
                    "name": "Nexolu POS",
                    "whatsapp": {"phone_number_id": "123456", "access_token": "wa-token"},
                    "email": {"from_email": "no-reply@pos.nexolu.co", "from_name": "Nexolu POS"},
                }
            }
        ),
    )
    monkeypatch.setenv("NEXOLU_PLATFORM_API_KEY", "platform-key")
    monkeypatch.setenv("BREVO_API_KEY", "platform-brevo-key")

    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    import nexolu_comms_api.core.auth.apps as apps_module
    from nexolu_comms_api.config import get_settings
    from nexolu_comms_api.core.channels.registry import get_channel_registry
    from nexolu_comms_api.core.db.session import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_channel_registry.cache_clear()
    apps_module._registry = None


@pytest.fixture
def client(app_env):
    from nexolu_comms_api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {POS_API_KEY}"}
