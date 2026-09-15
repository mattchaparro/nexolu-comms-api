"""Flag `enforce_meta_signature` por app: con el encendido, un webhook sin
firma verificable se rechaza con 401 (fallar cerrado) en vez de pasar con
warning. El default (apagado) conserva el comportamiento historico para las
apps que aun no configuran su meta_app_secret."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import _clear_caches


@pytest.fixture
def client_without_secret(app_env, monkeypatch):
    """App 'pos' SIN meta_app_secret, variando el flag por parametro."""

    def build(enforce: bool) -> TestClient:
        monkeypatch.setenv(
            "NEXOLU_APPS_JSON",
            json.dumps(
                {
                    "pos": {
                        "api_key": "dev-pos-key",
                        "whatsapp": {
                            "phone_number_id": "123456",
                            "access_token": "wa-token",
                            "enforce_meta_signature": enforce,
                            "callback_url": "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp",
                            "callback_secret": "pos-callback-secret",
                        },
                    }
                }
            ),
        )
        _clear_caches()
        from nexolu_comms_api.main import create_app

        return TestClient(create_app())

    return build


def test_enforcing_without_a_secret_fails_closed(client_without_secret, httpx_mock):
    with client_without_secret(enforce=True) as client:
        response = client.post("/webhooks/whatsapp/pos", json={"entry": []})

    assert response.status_code == 401
    assert httpx_mock.get_requests() == []


def test_without_the_flag_a_missing_secret_still_forwards(client_without_secret, httpx_mock):
    httpx_mock.add_response(
        url="https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp", json={"ok": True}
    )

    with client_without_secret(enforce=False) as client:
        response = client.post("/webhooks/whatsapp/pos", json={"entry": []})

    assert response.status_code == 200
    assert len(httpx_mock.get_requests()) == 1
