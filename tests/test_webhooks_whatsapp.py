"""Cubre el webhook de WhatsApp por app: handshake de verificacion, y
reenvio firmado del evento crudo al callback de la app. No prueba que Meta
en si funcione - solo que este servicio verifica lo que hay que verificar y
reenvia sin tocar el contenido."""
from __future__ import annotations

import hashlib
import hmac
import json


def _meta_signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_returns_the_challenge_for_a_valid_token(client):
    response = client.get(
        "/webhooks/whatsapp/pos",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"},
    )

    assert response.status_code == 200
    assert response.text == "12345"


def test_verify_rejects_a_wrong_token(client):
    response = client.get(
        "/webhooks/whatsapp/pos",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )

    assert response.status_code == 403


def test_verify_rejects_an_unknown_app(client):
    response = client.get(
        "/webhooks/whatsapp/no-existe",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"},
    )

    assert response.status_code == 403


def test_receive_rejects_an_unknown_app(client):
    assert client.post("/webhooks/whatsapp/no-existe", json={"entry": []}).status_code == 404


def test_receive_rejects_an_invalid_meta_signature(client, httpx_mock):
    response = client.post(
        "/webhooks/whatsapp/pos",
        json={"entry": [{"changes": []}]},
        headers={"X-Hub-Signature-256": "sha256=wrong"},
    )

    assert response.status_code == 401
    assert httpx_mock.get_requests() == []  # nunca llego a reenviar


def test_receive_forwards_the_raw_event_signed_to_the_apps_callback(client, httpx_mock):
    httpx_mock.add_response(url="https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp", json={"ok": True})

    body = {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.1", "from": "573001234567"}]}}]}]}
    raw = json.dumps(body).encode()

    response = client.post(
        "/webhooks/whatsapp/pos",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _meta_signature(raw, "meta-app-secret"),
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    forwarded = httpx_mock.get_requests()[0]
    assert forwarded.headers["X-Nexolu-Signature"]
    assert forwarded.headers["X-Nexolu-Timestamp"]
    assert json.loads(forwarded.content) == body  # reenviado intacto, sin interpretar


def test_receive_skips_verification_when_no_meta_app_secret_is_configured(client, httpx_mock, monkeypatch):
    httpx_mock.add_response(url="https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp", json={"ok": True})

    monkeypatch.setenv(
        "NEXOLU_APPS_JSON",
        json.dumps(
            {
                "pos": {
                    "api_key": "dev-pos-key",
                    "name": "Nexolu POS",
                    "whatsapp": {
                        "phone_number_id": "123456",
                        "access_token": "wa-token",
                        "webhook_verify_token": "verify-me",
                        "callback_url": "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp",
                        "callback_secret": "pos-callback-secret",
                    },
                }
            }
        ),
    )
    import nexolu_comms_api.core.auth.apps as apps_module
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()
    apps_module._registry = None

    response = client.post("/webhooks/whatsapp/pos", json={"entry": []})

    assert response.status_code == 200
    assert len(httpx_mock.get_requests()) == 1
