"""Multi-usuario del panel Connect (Fase 2b del plan): SSO con nexolu-auth
(aserciones RS256 verificadas localmente), la distincion dura
plataforma/cliente externo, y el scoping por membresias - un cliente ve y
opera SOLO sus apps, y lo ajeno responde 404 como si no existiera."""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.conftest import TEST_PLATFORM_API_KEY, _clear_caches

# --- llaves RSA de "nexolu-auth" para firmar aserciones de prueba ------------

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = (
    _PRIVATE_KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)
_PUBLIC_KEYS_JSON = json.dumps({"kid-test": base64.b64encode(_PUBLIC_PEM.encode()).decode()})


def _assertion(email: str, *, kid: str = "kid-test", typ: str = "sso", audience: str = "nexolu-connect") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "https://auth.nexolu.co",
            "aud": audience,
            "sub": email,
            "email": email,
            "typ": typ,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + 120,
        },
        _PRIVATE_PEM,
        algorithm="RS256",
        headers={"kid": kid},
    )


@pytest.fixture
def panel(app_env, monkeypatch):
    """Panel configurado con break-glass + SSO habilitado."""
    import bcrypt

    monkeypatch.setenv("PANEL_EMAIL", "alejandro@nexolu.co")
    monkeypatch.setenv("PANEL_PASSWORD_HASH", bcrypt.hashpw(b"clave-admin", bcrypt.gensalt()).decode())
    monkeypatch.setenv("PANEL_JWT_SECRET", "panel-jwt-secret")
    monkeypatch.setenv("NEXOLU_AUTH_PUBLIC_KEYS", _PUBLIC_KEYS_JSON)
    _clear_caches()
    from nexolu_comms_api.main import create_app

    with TestClient(create_app()) as client:
        yield client


def _platform_headers():
    return {"Authorization": f"Bearer {TEST_PLATFORM_API_KEY}"}


def _seed_apps(client, *app_ids: str) -> None:
    for app_id in app_ids:
        response = client.post("/v1/admin/apps", headers=_platform_headers(), json={"app_id": app_id})
        assert response.status_code == 201


def _create_client_user(client, email: str, app_ids: list[str], password: str | None = "clave-cliente-8"):
    payload = {"email": email, "full_name": "Cliente Demo", "role": "client", "app_ids": app_ids}
    if password:
        payload["password"] = password
    response = client.post("/v1/admin/users", headers=_platform_headers(), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token: str):
    return {"Authorization": f"Bearer {token}"}


# --- SSO ----------------------------------------------------------------------


def test_sso_exchange_for_the_platform_admin(panel):
    response = panel.post("/panel/auth/sso/exchange", json={"assertion": _assertion("alejandro@nexolu.co")})

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["roles"] == ["platform"]
    assert panel.get("/panel/me", headers=_bearer(body["token"])).status_code == 200


def test_sso_exchange_for_a_client_user_carries_its_apps(panel):
    _seed_apps(panel, "luxury")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"], password=None)  # solo-SSO

    response = panel.post("/panel/auth/sso/exchange", json={"assertion": _assertion("dueña@luxurynails.co")})

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["roles"] == ["client"]
    assert body["user"]["app_ids"] == ["luxury"]


def test_sso_exchange_rejects_an_unknown_identity_as_terminal_403(panel):
    response = panel.post("/panel/auth/sso/exchange", json={"assertion": _assertion("intruso@x.co")})

    assert response.status_code == 403


def test_the_same_assertion_cannot_be_exchanged_twice(panel):
    assertion = _assertion("alejandro@nexolu.co")

    assert panel.post("/panel/auth/sso/exchange", json={"assertion": assertion}).status_code == 200
    assert panel.post("/panel/auth/sso/exchange", json={"assertion": assertion}).status_code == 401


def test_an_assertion_for_another_product_is_rejected(panel):
    assertion = _assertion("alejandro@nexolu.co", audience="nexolu-admin")

    assert panel.post("/panel/auth/sso/exchange", json={"assertion": assertion}).status_code == 401


def test_sso_without_public_keys_is_503_and_local_login_still_works(app_env, monkeypatch):
    import bcrypt

    monkeypatch.setenv("PANEL_EMAIL", "alejandro@nexolu.co")
    monkeypatch.setenv("PANEL_PASSWORD_HASH", bcrypt.hashpw(b"clave-admin", bcrypt.gensalt()).decode())
    monkeypatch.setenv("PANEL_JWT_SECRET", "panel-jwt-secret")
    _clear_caches()
    from nexolu_comms_api.main import create_app

    with TestClient(create_app()) as client:
        sso = client.post("/panel/auth/sso/exchange", json={"assertion": _assertion("alejandro@nexolu.co")})
        login = client.post(
            "/panel/auth/login", json={"email": "alejandro@nexolu.co", "password": "clave-admin"}
        )

    assert sso.status_code == 503
    assert login.status_code == 200


# --- roles y scoping -----------------------------------------------------------


def _client_token(panel, email: str = "dueña@luxurynails.co") -> str:
    response = panel.post(
        "/panel/auth/login", json={"email": email, "password": "clave-cliente-8"}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_a_client_user_logs_in_with_password_and_sees_only_its_apps(panel):
    _seed_apps(panel, "luxury", "otra")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)

    apps = panel.get("/v1/admin/apps", headers=_bearer(token)).json()

    assert [a["app_id"] for a in apps] == ["luxury"]


def test_a_client_jwt_cannot_use_platform_only_endpoints(panel):
    _seed_apps(panel, "luxury")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)

    create = panel.post("/v1/admin/apps", headers=_bearer(token), json={"app_id": "hackeada"})
    users = panel.get("/v1/admin/users", headers=_bearer(token))
    rotate = panel.post("/v1/admin/apps/luxury/regenerate-key", headers=_bearer(token))

    assert create.status_code == 401
    assert users.status_code == 401
    assert rotate.status_code == 401


def test_a_client_manages_its_own_credentials_but_foreign_apps_look_nonexistent(panel):
    _seed_apps(panel, "luxury", "otra")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)

    own = panel.post(
        "/v1/admin/apps/luxury/providers/meta-whatsapp",
        headers=_bearer(token),
        json={"phone_number_id": "111", "access_token": "tok"},
    )
    foreign = panel.get("/v1/admin/apps/otra/providers/meta-whatsapp", headers=_bearer(token))

    assert own.status_code == 201
    assert foreign.status_code == 404


def test_webhook_events_are_filtered_to_the_clients_apps(panel):
    from nexolu_comms_api.core.db.entities import WebhookEvent
    from nexolu_comms_api.core.db.session import get_sessionmaker

    _seed_apps(panel, "luxury", "otra")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)

    async def seed_events():
        async with get_sessionmaker()() as session:
            session.add(WebhookEvent(app_id="luxury", event_type="message", payload="{}"))
            session.add(WebhookEvent(app_id="otra", event_type="order", payload="{}"))
            await session.commit()

    asyncio.run(seed_events())

    listed = panel.get("/v1/admin/webhook-events", headers=_bearer(token)).json()
    assert listed["total"] == 1
    assert listed["items"][0]["app_id"] == "luxury"

    # El detalle de un evento ajeno responde como si no existiera.
    all_events = panel.get("/v1/admin/webhook-events", headers=_platform_headers()).json()["items"]
    foreign_id = next(e["id"] for e in all_events if e["app_id"] == "otra")
    assert panel.get(f"/v1/admin/webhook-events/{foreign_id}", headers=_bearer(token)).status_code == 404


def test_platform_notifications_and_usage_are_scoped(panel, httpx_mock):
    _seed_apps(panel, "luxury", "otra")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)

    from nexolu_comms_api.core.db.entities import Notification
    from nexolu_comms_api.core.db.session import get_sessionmaker

    async def seed():
        async with get_sessionmaker()() as session:
            session.add(
                Notification(
                    app_id="luxury", business_id="b1", channel="whatsapp", recipient="+57...",
                    status="sent", cost_micros=8000,
                )
            )
            session.add(
                Notification(
                    app_id="otra", business_id="b2", channel="whatsapp", recipient="+57...",
                    status="sent", cost_micros=8000,
                )
            )
            await session.commit()

    asyncio.run(seed())

    notifications = panel.get("/v1/platform/notifications", headers=_bearer(token)).json()
    assert {n["app_id"] for n in notifications["notifications"]} == {"luxury"}

    usage = panel.get("/v1/platform/usage", headers=_bearer(token)).json()
    assert [row["key"] for row in usage["breakdown"]] == ["luxury"]

    assert panel.get("/v1/platform/usage", headers=_bearer(token), params={"app_id": "otra"}).status_code == 404


def test_business_channels_are_scoped_too(panel):
    from datetime import datetime

    from nexolu_comms_api.core.db.entities import BusinessChannel
    from nexolu_comms_api.core.db.session import get_sessionmaker

    _seed_apps(panel, "luxury", "otra")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)

    async def seed():
        async with get_sessionmaker()() as session:
            own = BusinessChannel(
                app_id="luxury", business_id="sede-1", waba_id="w1", phone_number_id="111",
                access_token="t1", status="active", connected_at=datetime.utcnow(),
            )
            foreign = BusinessChannel(
                app_id="otra", business_id="b", waba_id="w2", phone_number_id="222",
                access_token="t2", status="active", connected_at=datetime.utcnow(),
            )
            session.add_all([own, foreign])
            await session.commit()
            return own.id, foreign.id

    own_id, foreign_id = asyncio.run(seed())

    listed = panel.get("/v1/admin/business-channels", headers=_bearer(token)).json()["items"]
    assert [c["id"] for c in listed] == [own_id]
    assert panel.post(f"/v1/admin/business-channels/{foreign_id}/disconnect", headers=_bearer(token)).status_code == 404
    assert panel.post(f"/v1/admin/business-channels/{own_id}/disconnect", headers=_bearer(token)).status_code == 200


# --- gestion de usuarios --------------------------------------------------------


def test_creating_a_user_with_a_membership_to_a_ghost_app_fails(panel):
    response = panel.post(
        "/v1/admin/users",
        headers=_platform_headers(),
        json={"email": "x@y.co", "role": "client", "app_ids": ["no-existe"]},
    )

    assert response.status_code == 422


def test_duplicated_emails_are_rejected(panel):
    _seed_apps(panel, "luxury")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])

    response = panel.post(
        "/v1/admin/users",
        headers=_platform_headers(),
        json={"email": "DUEÑA@luxurynails.co", "role": "client", "app_ids": ["luxury"]},
    )

    assert response.status_code == 409


def test_deactivating_a_user_kills_its_session_immediately(panel):
    _seed_apps(panel, "luxury")
    user = _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)
    assert panel.get("/panel/me", headers=_bearer(token)).status_code == 200

    patched = panel.patch(
        f"/v1/admin/users/{user['id']}", headers=_platform_headers(), json={"is_active": False}
    )

    assert patched.status_code == 200
    # El JWT sigue vigente criptograficamente, pero la identidad se resuelve
    # contra la BD en cada request: el acceso muere YA, no cuando expire.
    assert panel.get("/panel/me", headers=_bearer(token)).status_code == 401


def test_templates_are_scoped_for_client_users(panel):
    from nexolu_comms_api.core.db.entities import WhatsAppTemplate
    from nexolu_comms_api.core.db.session import get_sessionmaker

    _seed_apps(panel, "luxury", "otra")
    _create_client_user(panel, "dueña@luxurynails.co", ["luxury"])
    token = _client_token(panel)

    async def seed():
        async with get_sessionmaker()() as session:
            session.add(
                WhatsAppTemplate(
                    app_id="luxury", waba_id="w1", name="propia", language="es", category="UTILITY"
                )
            )
            session.add(
                WhatsAppTemplate(
                    app_id="otra", waba_id="w2", name="ajena", language="es", category="UTILITY"
                )
            )
            await session.commit()

    asyncio.run(seed())

    listed = panel.get("/v1/admin/templates", headers=_bearer(token)).json()["items"]
    assert [t["name"] for t in listed] == ["propia"]
