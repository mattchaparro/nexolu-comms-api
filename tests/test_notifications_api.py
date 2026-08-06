"""Cubre el endpoint estrella: POST /v1/notifications/send con varios
canales en una sola llamada. No prueba WhatsApp/Brevo en si (eso lo cubren
test_whatsapp_channel.py / test_email_channel.py) - aca importa que cada
canal se procese de forma independiente y quede registrado."""
from __future__ import annotations


def test_rejects_a_request_without_authorization(client):
    response = client.post("/v1/notifications/send", json={"business_id": "1", "channels": ["email"], "to": {}})

    assert response.status_code == 401


def test_rejects_an_empty_channels_list(client, auth_headers):
    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={"business_id": "1", "channels": [], "to": {}},
    )

    assert response.status_code == 422


def test_business_id_is_optional_and_falls_back_to_the_apps_own_id(client, auth_headers, httpx_mock):
    """Una app sin concepto propio de tenant (un solo cliente, sin
    sub-negocios) puede omitir business_id por completo - no le tiene que
    inventar un valor a un dato que no le aplica."""
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m1@brevo>"})

    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={"channels": ["email"], "to": {"email": "a@b.com"}, "subject": "Hola", "text": "hola"},
    )

    assert response.status_code == 200
    assert response.json()["business_id"] == "pos"  # app_id de auth_headers

    summary = client.get("/v1/usage/summary?business_id=pos", headers=auth_headers).json()
    assert summary["summary"]["message_count"] == 1


def test_sends_the_same_notification_over_two_channels_with_one_call(client, auth_headers, httpx_mock):
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/123456/messages", json={"messages": [{"id": "wamid.1"}]}
    )
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m1@brevo>"})

    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": "42",
            "reference": "low_stock_alert:456",
            "channels": ["whatsapp", "email"],
            "to": {"whatsapp": "+573001234567", "email": "dueno@negocio.com"},
            "subject": "Alerta de inventario bajo",
            "text": "3 productos estan por debajo del umbral.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reference"] == "low_stock_alert:456"
    assert body["business_id"] == "42"

    by_channel = {r["channel"]: r for r in body["results"]}
    assert by_channel["whatsapp"]["status"] == "sent"
    assert by_channel["whatsapp"]["provider_message_id"] == "wamid.1"
    assert by_channel["email"]["status"] == "sent"
    assert by_channel["email"]["provider_message_id"] == "<m1@brevo>"


def test_one_channel_missing_a_recipient_does_not_block_the_others(client, auth_headers, httpx_mock):
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m1@brevo>"})

    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": "42",
            "channels": ["whatsapp", "email"],
            "to": {"email": "dueno@negocio.com"},  # falta whatsapp
            "subject": "Hola",
            "text": "hola",
        },
    )

    assert response.status_code == 200
    by_channel = {r["channel"]: r for r in response.json()["results"]}
    assert by_channel["whatsapp"]["status"] == "failed"
    assert "destinatario" in by_channel["whatsapp"]["error"]
    assert by_channel["email"]["status"] == "sent"


def test_an_unknown_channel_fails_on_its_own_without_blocking_the_rest(client, auth_headers, httpx_mock):
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m1@brevo>"})

    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": "42",
            "channels": ["sms", "email"],
            "to": {"sms": "+573001234567", "email": "dueno@negocio.com"},
            "subject": "Hola",
            "text": "hola",
        },
    )

    assert response.status_code == 200
    by_channel = {r["channel"]: r for r in response.json()["results"]}
    assert by_channel["sms"]["status"] == "failed"
    assert "desconocido" in by_channel["sms"]["error"]
    assert by_channel["email"]["status"] == "sent"


def test_a_channel_not_configured_for_the_app_is_reported_as_skipped(client, auth_headers, monkeypatch):
    import json

    monkeypatch.setenv(
        "NEXOLU_APPS_JSON",
        json.dumps({"pos": {"api_key": "dev-pos-key", "name": "Nexolu POS"}}),  # sin whatsapp/email
    )
    import nexolu_comms_api.core.auth.apps as apps_module
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()
    apps_module._registry = None

    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={"business_id": "42", "channels": ["whatsapp"], "to": {"whatsapp": "+573001234567"}, "text": "hola"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "skipped"


def test_every_channel_attempt_is_persisted_regardless_of_outcome(client, auth_headers, httpx_mock):
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m1@brevo>"})

    client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": "42",
            "reference": "ref-1",
            "channels": ["sms", "email"],
            "to": {"sms": "+57", "email": "a@b.com"},
            "subject": "Hola",
            "text": "hola",
        },
    )

    summary = client.get("/v1/usage/summary", headers=auth_headers).json()
    # "sms" desconocido nunca llega a contar como enviado - solo el email exitoso.
    assert summary["summary"]["message_count"] == 1
