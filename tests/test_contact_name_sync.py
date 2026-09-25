"""El nombre del contacto, igual en Connect y en la app duena.

La ficha de la clienta es del Spa; el chat es de Connect. Si alguien
corrige el nombre en el chat, el Spa se entera (`contact_updated`); si el
Spa lo aprende (la clienta lo confirma por WhatsApp), Connect lo muestra
(PATCH /v1/contacts). Y ninguno de los dos le devuelve el eco al otro.
"""
from __future__ import annotations

import json

from tests.test_embed_chat import CALLBACK_URL, _inbound

PLATFORM = {"Authorization": "Bearer platform-key"}


def _contact_id(client) -> str:
    return client.get("/v1/admin/chats", headers=PLATFORM).json()["items"][0]["contact_id"]


def _events(httpx_mock, event: str) -> list[dict]:
    found = []
    for request in httpx_mock.get_requests(url=CALLBACK_URL):
        body = json.loads(request.content)
        if body.get("event") == event:
            found.append(body)
    return found


def test_corregir_el_nombre_en_el_chat_le_avisa_a_la_app(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})

    response = client.patch(f"/v1/admin/chats/{_contact_id(client)}", json={"name": "Ana María"}, headers=PLATFORM)
    assert response.status_code == 200

    [event] = _events(httpx_mock, "contact_updated")
    assert event["business_id"] == "7"
    assert event["contact"] == {"phone": "573001112233", "name": "Ana María"}


def test_guardar_el_mismo_nombre_no_avisa(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    contact_id = _contact_id(client)
    same = client.get(f"/v1/admin/chats/{contact_id}", headers=PLATFORM).json()["name"]

    client.patch(f"/v1/admin/chats/{contact_id}", json={"name": same, "notes": "vip"}, headers=PLATFORM)

    assert _events(httpx_mock, "contact_updated") == []


def test_la_app_pone_el_nombre_y_no_recibe_el_eco(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")

    response = client.patch(
        "/v1/contacts",
        json={"phone": "+57 300 111 2233", "business_id": "7", "name": "Carolina Pérez"},
        headers=auth_headers,
    )

    assert response.json() == {"updated": 1}
    assert client.get("/v1/admin/chats", headers=PLATFORM).json()["items"][0]["name"] == "Carolina Pérez"
    assert _events(httpx_mock, "contact_updated") == []


def test_la_app_no_crea_contactos_ni_toca_los_de_otro_negocio(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")

    otro_negocio = client.patch(
        "/v1/contacts", json={"phone": "573001112233", "business_id": "9", "name": "X"}, headers=auth_headers
    )
    desconocido = client.patch(
        "/v1/contacts", json={"phone": "573009990000", "business_id": "7", "name": "X"}, headers=auth_headers
    )

    assert otro_negocio.json() == {"updated": 0}
    assert desconocido.json() == {"updated": 0}
    assert client.patch("/v1/contacts", json={"phone": "573001112233", "name": "X"}).status_code == 401
