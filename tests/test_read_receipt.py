from __future__ import annotations


def test_requires_authorization(client):
    response = client.post("/v1/whatsapp/read-receipt", json={"to": "+573001234567", "message_id": "wamid.1"})

    assert response.status_code == 401


def test_marks_as_read_and_activates_typing(client, auth_headers, httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v21.0/123456/messages", json={"success": True})

    response = client.post(
        "/v1/whatsapp/read-receipt",
        headers=auth_headers,
        json={"to": "+573001234567", "message_id": "wamid.1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"

    request = httpx_mock.get_requests()[0]
    import json

    body = json.loads(request.content)
    assert body["status"] == "read"
    assert body["message_id"] == "wamid.1"
    assert body["typing_indicator"] == {"type": "text"}


def test_skipped_when_the_app_has_no_whatsapp_configured(client, monkeypatch):
    import json

    monkeypatch.setenv("NEXOLU_APPS_JSON", json.dumps({"pos": {"api_key": "dev-pos-key", "name": "Nexolu POS"}}))
    import nexolu_comms_api.core.auth.apps as apps_module
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()
    apps_module._registry = None

    response = client.post(
        "/v1/whatsapp/read-receipt",
        headers={"Authorization": "Bearer dev-pos-key"},
        json={"to": "+573001234567", "message_id": "wamid.1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
