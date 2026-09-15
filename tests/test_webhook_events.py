"""Persistencia y reintento de eventos de webhook (Fase 0 del plan de
WhatsApp transversal): un evento entrante ya no vive solo en memoria - queda
en `webhook_events` y, si el callback de la app falla, se reintenta con
backoff hasta entregarlo o declararlo `dead`. Tambien cubre los endpoints
de plataforma que el panel usa para triage y re-lanzamiento manual."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest

from nexolu_comms_api.core.db.entities import WebhookEvent
from nexolu_comms_api.core.db.session import get_sessionmaker, init_models
from nexolu_comms_api.core.webhooks import forwarder

CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"


def _meta_signature(body: bytes, secret: str = "meta-app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _order_body() -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "123456"},
                                "messages": [{"id": "wamid.order", "type": "order"}],
                            },
                        }
                    ]
                }
            ]
        }
    ).encode()


def _post_event(client, body: bytes):
    return client.post(
        "/webhooks/whatsapp/pos",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _meta_signature(body)},
    )


def _list_events(client, platform_headers, **params):
    response = client.get("/v1/admin/webhook-events", headers=platform_headers, params=params)
    assert response.status_code == 200
    return response.json()


def test_a_delivered_event_is_persisted_with_metadata(client, httpx_mock, platform_headers):
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})

    assert _post_event(client, _order_body()).status_code == 200

    listed = _list_events(client, platform_headers)
    assert listed["total"] == 1
    event = listed["items"][0]
    assert event["forward_status"] == "delivered"
    assert event["event_type"] == "order"
    assert event["phone_number_id"] == "123456"
    assert event["signature_valid"] is True
    assert event["attempts"] == 1
    assert event["delivered_at"] is not None


def test_a_failed_forward_schedules_a_retry_instead_of_losing_the_event(client, httpx_mock, platform_headers):
    httpx_mock.add_response(url=CALLBACK_URL, status_code=500)

    # Meta SIEMPRE recibe 200: el fallo es entre comms y la app, no de Meta.
    assert _post_event(client, _order_body()).status_code == 200

    event = _list_events(client, platform_headers, forward_status="failed")["items"][0]
    assert event["attempts"] == 1
    assert event["next_retry_at"] is not None
    assert "500" in event["last_error"]

    # El payload crudo sigue integro para el reintento.
    detail = client.get(f"/v1/admin/webhook-events/{event['id']}", headers=platform_headers)
    assert detail.status_code == 200
    assert json.loads(detail.json()["payload"]) == json.loads(_order_body())


def test_an_invalid_signature_is_recorded_as_rejected_and_never_forwarded(client, httpx_mock, platform_headers):
    response = client.post(
        "/webhooks/whatsapp/pos",
        content=_order_body(),
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=wrong"},
    )

    assert response.status_code == 401
    assert httpx_mock.get_requests() == []

    event = _list_events(client, platform_headers)["items"][0]
    assert event["forward_status"] == "rejected"
    assert event["signature_valid"] is False


def test_manual_retry_relaunches_a_failed_event(client, httpx_mock, platform_headers):
    httpx_mock.add_response(url=CALLBACK_URL, status_code=500)
    _post_event(client, _order_body())
    event_id = _list_events(client, platform_headers)["items"][0]["id"]

    # La app "vuelve a la vida": el re-lanzamiento manual entrega ya mismo.
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    response = client.post(f"/v1/admin/webhook-events/{event_id}/retry", headers=platform_headers)

    assert response.status_code == 200
    assert response.json()["forward_status"] == "delivered"


def test_a_delivered_event_cannot_be_retried_into_a_duplicate(client, httpx_mock, platform_headers):
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    _post_event(client, _order_body())
    event_id = _list_events(client, platform_headers)["items"][0]["id"]

    response = client.post(f"/v1/admin/webhook-events/{event_id}/retry", headers=platform_headers)

    assert response.status_code == 409


@pytest.fixture
async def db(app_env):
    await init_models()
    yield


async def _insert_event(**overrides) -> str:
    event = WebhookEvent(
        app_id="pos",
        event_type="order",
        payload=_order_body().decode(),
        **overrides,
    )
    async with get_sessionmaker()() as session:
        session.add(event)
        await session.commit()
        return event.id


async def _fetch(event_id: str) -> WebhookEvent:
    async with get_sessionmaker()() as session:
        event = await session.get(WebhookEvent, event_id)
        assert event is not None
        return event


async def test_retry_due_events_delivers_what_is_overdue(db, httpx_mock):
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    event_id = await _insert_event(
        forward_status="failed", attempts=1, next_retry_at=datetime.utcnow() - timedelta(seconds=1)
    )

    attempted = await forwarder.retry_due_events()

    assert attempted == 1
    event = await _fetch(event_id)
    assert event.forward_status == "delivered"
    assert event.attempts == 2


async def test_retry_due_events_ignores_what_is_not_due_yet(db, httpx_mock):
    await _insert_event(
        forward_status="failed", attempts=1, next_retry_at=datetime.utcnow() + timedelta(hours=1)
    )

    assert await forwarder.retry_due_events() == 0
    assert httpx_mock.get_requests() == []


async def test_an_orphaned_pending_event_is_adopted_by_the_worker(db, httpx_mock):
    """Proceso reiniciado entre persistir y el primer intento: el evento
    quedo `pending` sin que nadie lo reenvie. Pasada la gracia, el worker
    lo trata como suyo."""
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    event_id = await _insert_event(
        forward_status="pending",
        received_at=datetime.utcnow() - timedelta(seconds=forwarder.PENDING_GRACE_SECONDS + 1),
    )

    assert await forwarder.retry_due_events() == 1
    assert (await _fetch(event_id)).forward_status == "delivered"


async def test_an_event_dies_after_exhausting_all_retries(db, httpx_mock):
    httpx_mock.add_response(url=CALLBACK_URL, status_code=500)
    # Ya agoto la tabla de esperas: el siguiente fallo es el ultimo.
    event_id = await _insert_event(
        forward_status="failed",
        attempts=len(forwarder.RETRY_DELAYS_SECONDS),
        next_retry_at=datetime.utcnow() - timedelta(seconds=1),
    )

    await forwarder.retry_due_events()

    event = await _fetch(event_id)
    assert event.forward_status == "dead"
    assert event.next_retry_at is None
    assert event.attempts == len(forwarder.RETRY_DELAYS_SECONDS) + 1
