"""La pausa del bot de la app, visible y reactivable desde el chat.

La pausa es de la app (el Spa calla a su bot cuando alguien del equipo
contesta). Connect la muestra -- la app le dice hasta cuando con
PUT /v1/contacts/bot-pause -- y el boton «Reactivar bot» le avisa a la app
con `agent_resume`. Antes no habia como quitarla desde Connect: habia que
esperar las dos horas.
"""
from __future__ import annotations

from tests.test_contact_name_sync import PLATFORM, _contact_id, _events
from tests.test_embed_chat import CALLBACK_URL, _inbound


def _card(client) -> dict:
    return client.get(f"/v1/admin/chats/{_contact_id(client)}", headers=PLATFORM).json()


def test_la_app_marca_la_pausa_y_el_chat_la_ve(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")

    response = client.put(
        "/v1/contacts/bot-pause",
        json={"phone": "573001112233", "business_id": "7", "paused_until": "2026-09-28T22:30:00+00:00"},
        headers=auth_headers,
    )

    assert response.json() == {"updated": 1}
    assert _card(client)["fields"]["bot_paused_until"] == "2026-09-28T22:30:00+00:00"

    # Y la quita cuando el bot vuelve a atender.
    client.put(
        "/v1/contacts/bot-pause",
        json={"phone": "573001112233", "business_id": "7", "paused_until": None},
        headers=auth_headers,
    )
    assert "bot_paused_until" not in _card(client)["fields"]


def test_reactivar_desde_el_chat_le_avisa_a_la_app(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    client.put(
        "/v1/contacts/bot-pause",
        json={"phone": "573001112233", "business_id": "7", "paused_until": "2026-09-28T22:30:00+00:00"},
        headers=auth_headers,
    )
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})

    response = client.post(f"/v1/admin/chats/{_contact_id(client)}/bot/resume", headers=PLATFORM)

    assert response.status_code == 204
    assert "bot_paused_until" not in _card(client)["fields"]
    [event] = _events(httpx_mock, "agent_resume")
    assert event["business_id"] == "7"
    assert event["contact"]["phone"] == "573001112233"
