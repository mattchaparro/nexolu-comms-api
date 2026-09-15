"""Sesion del panel dedicado: login con bcrypt + JWT local, y el JWT como
credencial valida en los endpoints de plataforma (asi el front consume
/v1/admin/* sin que la platform key viaje al navegador)."""
from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

from tests.conftest import _clear_caches

PASSWORD = "clave-segura"


@pytest.fixture
def panel_client(app_env, monkeypatch):
    monkeypatch.setenv("PANEL_EMAIL", "alejandro@nexolu.co")
    monkeypatch.setenv(
        "PANEL_PASSWORD_HASH", bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    )
    monkeypatch.setenv("PANEL_JWT_SECRET", "panel-jwt-secret")
    _clear_caches()
    from nexolu_comms_api.main import create_app

    with TestClient(create_app()) as client:
        yield client


def _login(client, password=PASSWORD):
    return client.post("/panel/auth/login", json={"email": "alejandro@nexolu.co", "password": password})


def test_login_returns_a_token_and_the_operator(panel_client):
    response = _login(panel_client)

    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["user"]["email"] == "alejandro@nexolu.co"
    assert body["user"]["roles"] == ["platform"]


def test_login_rejects_a_wrong_password(panel_client):
    assert _login(panel_client, password="otra").status_code == 401


def test_login_fails_closed_when_the_panel_is_not_configured(client):
    # El fixture `client` normal no configura PANEL_*: 503 (panel sin
    # configurar, condicion operativa) y no 401 (credenciales malas).
    response = client.post("/panel/auth/login", json={"email": "x@x.co", "password": "x"})

    assert response.status_code == 503


def test_me_accepts_the_token_and_rejects_garbage(panel_client):
    token = _login(panel_client).json()["token"]

    ok = panel_client.get("/panel/me", headers={"Authorization": f"Bearer {token}"})
    bad = panel_client.get("/panel/me", headers={"Authorization": "Bearer basura"})

    assert ok.status_code == 200
    assert bad.status_code == 401


def test_the_panel_token_grants_platform_access(panel_client):
    token = _login(panel_client).json()["token"]

    response = panel_client.get(
        "/v1/admin/webhook-events", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_a_random_bearer_still_gets_401_on_platform_endpoints(panel_client):
    response = panel_client.get(
        "/v1/admin/webhook-events", headers={"Authorization": "Bearer ni-key-ni-jwt"}
    )

    assert response.status_code == 401
