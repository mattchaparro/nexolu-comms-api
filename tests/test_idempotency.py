"""Idempotencia de POST /v1/notifications/send via header Idempotency-Key:
repetir la llamada con la misma clave devuelve la respuesta original sin
enviar nada de nuevo. Sin header, cada llamada envia (comportamiento de
siempre)."""
from __future__ import annotations

WHATSAPP_URL = "https://graph.facebook.com/v21.0/123456/messages"


def _send(client, auth_headers, idempotency_key: str | None = None):
    headers = dict(auth_headers)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        "/v1/notifications/send",
        headers=headers,
        json={
            "business_id": "42",
            "channels": ["whatsapp"],
            "to": {"whatsapp": "+573001234567"},
            "text": "hola",
            "category": "service",
        },
    )


def _mock_whatsapp_ok(httpx_mock):
    httpx_mock.add_response(url=WHATSAPP_URL, json={"messages": [{"id": "wamid.sent-1"}]})


def test_the_same_key_returns_the_original_response_without_resending(client, auth_headers, httpx_mock):
    _mock_whatsapp_ok(httpx_mock)

    first = _send(client, auth_headers, idempotency_key="job-99")
    second = _send(client, auth_headers, idempotency_key="job-99")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    # Un solo POST a Meta: el retry no duplico el mensaje al cliente final.
    assert len(httpx_mock.get_requests(url=WHATSAPP_URL)) == 1


def test_different_keys_send_independently(client, auth_headers, httpx_mock):
    _mock_whatsapp_ok(httpx_mock)
    _mock_whatsapp_ok(httpx_mock)

    _send(client, auth_headers, idempotency_key="job-1")
    _send(client, auth_headers, idempotency_key="job-2")

    assert len(httpx_mock.get_requests(url=WHATSAPP_URL)) == 2


def test_without_a_key_every_call_sends(client, auth_headers, httpx_mock):
    _mock_whatsapp_ok(httpx_mock)
    _mock_whatsapp_ok(httpx_mock)

    _send(client, auth_headers)
    _send(client, auth_headers)

    assert len(httpx_mock.get_requests(url=WHATSAPP_URL)) == 2
