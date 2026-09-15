"""Identidad de WhatsApp por negocio (Fase 1 del plan transversal): el
envio resuelve primero el numero PROPIO del negocio (BusinessChannel, via
Embedded Signup) y cae al numero compartido de la app si no hay; el
onboarding hace el lado servidor del signup contra Meta; el webhook de
plataforma enruta por phone_number_id y reenvia con el business_id
resuelto."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from nexolu_comms_api.core.db.entities import BusinessChannel
from nexolu_comms_api.core.db.session import get_sessionmaker

APP_SHARED_URL = "https://graph.facebook.com/v21.0/123456/messages"
OWN_NUMBER_URL = "https://graph.facebook.com/v21.0/999888/messages"
CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"


async def _insert_channel(**overrides) -> str:
    defaults = {
        "app_id": "pos",
        "business_id": "42",
        "waba_id": "waba-1",
        "phone_number_id": "999888",
        "display_phone_number": "+573009998888",
        "access_token": "own-business-token",
        "pin": "123456",
        "status": "active",
        "connected_at": datetime.utcnow(),
    }
    defaults.update(overrides)
    channel = BusinessChannel(**defaults)
    async with get_sessionmaker()() as session:
        session.add(channel)
        await session.commit()
        return channel.id


def _send(client, auth_headers, business_id="42"):
    return client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": business_id,
            "channels": ["whatsapp"],
            "to": {"whatsapp": "+573001234567"},
            "text": "hola",
            "category": "service",
        },
    )


# --- resolucion de canal en el envio -----------------------------------------


def test_a_business_with_its_own_channel_sends_through_it(client, auth_headers, httpx_mock):
    import asyncio

    asyncio.run(_insert_channel())
    httpx_mock.add_response(url=OWN_NUMBER_URL, json={"messages": [{"id": "wamid.own"}]})

    response = _send(client, auth_headers, business_id="42")

    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "sent"
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer own-business-token"


def test_a_business_without_a_channel_falls_back_to_the_apps_shared_number(client, auth_headers, httpx_mock):
    httpx_mock.add_response(url=APP_SHARED_URL, json={"messages": [{"id": "wamid.shared"}]})

    response = _send(client, auth_headers, business_id="otro-negocio")

    assert response.status_code == 200
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "Bearer wa-token"


def test_a_disconnected_channel_is_not_used(client, auth_headers, httpx_mock):
    import asyncio

    asyncio.run(_insert_channel(status="disconnected"))
    httpx_mock.add_response(url=APP_SHARED_URL, json={"messages": [{"id": "wamid.shared"}]})

    _send(client, auth_headers, business_id="42")

    assert httpx_mock.get_requests()[0].headers["Authorization"] == "Bearer wa-token"


def test_a_401_from_meta_disconnects_the_business_channel(client, auth_headers, httpx_mock, platform_headers):
    import asyncio

    channel_id = asyncio.run(_insert_channel())
    httpx_mock.add_response(url=OWN_NUMBER_URL, status_code=401, json={"error": {"message": "token expirado"}})

    response = _send(client, auth_headers, business_id="42")

    assert response.json()["results"][0]["status"] == "failed"
    detail = client.get(f"/v1/admin/business-channels/{channel_id}", headers=platform_headers).json()
    assert detail["status"] == "disconnected"
    assert "401" in detail["last_error"]


# --- onboarding (Embedded Signup, lado servidor) ------------------------------


def _complete_signup(client, auth_headers):
    return client.post(
        "/v1/onboarding/whatsapp/complete",
        headers=auth_headers,
        json={
            "business_id": "42",
            "code": "signup-code",
            "waba_id": "waba-nueva",
            "phone_number_id": "555777",
            "display_phone_number": "+573005557777",
        },
    )


def test_complete_signup_requires_the_platform_meta_app(client, auth_headers):
    response = _complete_signup(client, auth_headers)

    assert response.status_code == 503


def test_complete_signup_exchanges_subscribes_registers_and_stores_the_channel(
    app_env, monkeypatch, httpx_mock, auth_headers
):
    monkeypatch.setenv("META_PLATFORM_APP_ID", "platform-app-id")
    monkeypatch.setenv("META_PLATFORM_APP_SECRET", "platform-app-secret")
    from tests.conftest import _clear_caches

    _clear_caches()
    from fastapi.testclient import TestClient

    from nexolu_comms_api.main import create_app

    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/oauth/access_token?client_id=platform-app-id&client_secret=platform-app-secret&code=signup-code",
        json={"access_token": "business-token-nuevo"},
    )
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/waba-nueva/subscribed_apps", json={"success": True}
    )
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/555777/register", json={"success": True}
    )

    with TestClient(create_app()) as client:
        response = _complete_signup(client, auth_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "active"
        assert body["phone_number_id"] == "555777"

        # El registro en Meta salio con el token del cliente y un PIN de 6 digitos.
        register = httpx_mock.get_requests(url="https://graph.facebook.com/v21.0/555777/register")[0]
        assert register.headers["Authorization"] == "Bearer business-token-nuevo"
        pin = json.loads(register.content)["pin"]
        assert len(pin) == 6 and pin.isdigit()

        # Y la app puede consultar el estado del canal de su negocio.
        status = client.get("/v1/onboarding/whatsapp/channels/42", headers=auth_headers)
        assert status.json()["status"] == "active"


def test_a_meta_rejection_surfaces_as_502_and_stores_nothing(
    app_env, monkeypatch, httpx_mock, auth_headers
):
    monkeypatch.setenv("META_PLATFORM_APP_ID", "platform-app-id")
    monkeypatch.setenv("META_PLATFORM_APP_SECRET", "platform-app-secret")
    from tests.conftest import _clear_caches

    _clear_caches()
    from fastapi.testclient import TestClient

    from nexolu_comms_api.main import create_app

    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/oauth/access_token?client_id=platform-app-id&client_secret=platform-app-secret&code=signup-code",
        status_code=400,
        json={"error": {"message": "code invalido o expirado"}},
    )

    with TestClient(create_app()) as client:
        response = _complete_signup(client, auth_headers)

        assert response.status_code == 502
        assert "code invalido" in response.json()["detail"]
        assert client.get("/v1/onboarding/whatsapp/channels/42", headers=auth_headers).json()["status"] == "not_connected"


# --- webhook de plataforma -----------------------------------------------------


def _platform_signature(body: bytes, secret: str = "platform-app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _platform_body(phone_number_id: str = "999888") -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "messages": [{"id": "wamid.p1", "type": "order"}],
                            },
                        }
                    ]
                }
            ]
        }
    ).encode()


def _platform_client(monkeypatch):
    monkeypatch.setenv("META_PLATFORM_APP_SECRET", "platform-app-secret")
    monkeypatch.setenv("META_PLATFORM_WEBHOOK_VERIFY_TOKEN", "verify-platform")
    from tests.conftest import _clear_caches

    _clear_caches()
    from fastapi.testclient import TestClient

    from nexolu_comms_api.main import create_app

    return TestClient(create_app())


def test_platform_verify_handshake(app_env, monkeypatch):
    with _platform_client(monkeypatch) as client:
        response = client.get(
            "/webhooks/whatsapp/platform",
            params={"hub.mode": "subscribe", "hub.verify_token": "verify-platform", "hub.challenge": "77"},
        )

    assert response.status_code == 200
    assert response.text == "77"


def test_platform_event_routes_by_phone_number_and_forwards_with_business_id(
    app_env, monkeypatch, httpx_mock, platform_headers
):
    import asyncio

    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    with _platform_client(monkeypatch) as client:
        asyncio.run(_insert_channel())  # canal del negocio 42, numero 999888, app pos
        body = _platform_body()

        response = client.post(
            "/webhooks/whatsapp/platform",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _platform_signature(body)},
        )

        assert response.status_code == 200
        forwarded = httpx_mock.get_requests(url=CALLBACK_URL)[0]
        # Payload intacto, firma HMAC propia, y el negocio ya resuelto.
        assert forwarded.content == body
        assert forwarded.headers["X-Nexolu-Business-Id"] == "42"
        assert "X-Nexolu-Signature" in forwarded.headers

        event = client.get(
            "/v1/admin/webhook-events", headers=platform_headers, params={"forward_status": "delivered"}
        ).json()["items"][0]
        assert event["app_id"] == "pos"
        assert event["event_type"] == "order"


def test_a_platform_event_from_an_unknown_number_is_kept_as_skipped(
    app_env, monkeypatch, httpx_mock, platform_headers
):
    with _platform_client(monkeypatch) as client:
        body = _platform_body(phone_number_id="000000")

        response = client.post(
            "/webhooks/whatsapp/platform",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _platform_signature(body)},
        )

        assert response.status_code == 200
        assert httpx_mock.get_requests() == []
        event = client.get("/v1/admin/webhook-events", headers=platform_headers).json()["items"][0]
        assert event["forward_status"] == "skipped"
        assert event["app_id"] == "platform"


def test_a_platform_event_with_a_bad_signature_is_rejected(app_env, monkeypatch, httpx_mock):
    with _platform_client(monkeypatch) as client:
        response = client.post(
            "/webhooks/whatsapp/platform",
            content=_platform_body(),
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=mala"},
        )

    assert response.status_code == 401
    assert httpx_mock.get_requests() == []


def test_platform_webhook_without_secret_configured_fails_closed(client):
    response = client.post("/webhooks/whatsapp/platform", json={"entry": []})

    assert response.status_code == 503


# --- panel: listado y desconexion manual --------------------------------------


def test_admin_can_list_and_disconnect_a_channel(client, platform_headers):
    import asyncio

    channel_id = asyncio.run(_insert_channel())

    listed = client.get("/v1/admin/business-channels", headers=platform_headers).json()
    assert [c["id"] for c in listed["items"]] == [channel_id]
    assert "access_token" not in listed["items"][0]

    disconnected = client.post(
        f"/v1/admin/business-channels/{channel_id}/disconnect", headers=platform_headers
    )
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "disconnected"
