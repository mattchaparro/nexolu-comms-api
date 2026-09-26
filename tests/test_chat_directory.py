"""Escribirle a alguien que no ha escrito: buscarla en el directorio (tenga o
no conversacion) o crearla por numero, y abrirle el chat con una plantilla.
"""
from __future__ import annotations

from tests.test_embed_chat import _inbound

PLATFORM = {"Authorization": "Bearer platform-key"}


def _publicar(client, auth_headers):
    response = client.put(
        "/v1/contacts/bulk",
        json={"business_id": "7", "contacts": [{"phone": "573102647944", "name": "Paola Chávez"}]},
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_encuentra_a_quien_nunca_ha_escrito(client, auth_headers, httpx_mock):
    _publicar(client, auth_headers)
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")

    por_nombre = client.get("/v1/admin/chats/directory", params={"q": "paola chavez"}, headers=PLATFORM).json()
    por_numero = client.get("/v1/admin/chats/directory", params={"q": "310 264"}, headers=PLATFORM).json()

    assert [c["name"] for c in por_nombre] == ["Paola Chávez"]
    assert por_nombre[0]["has_conversation"] is False
    assert por_numero[0]["phone"] == "573102647944"
    # Y la bandeja sigue mostrando solo conversaciones.
    assert [c["phone"] for c in client.get("/v1/admin/chats", headers=PLATFORM).json()["items"]] == ["573001112233"]


def test_un_numero_nuevo_se_crea_y_uno_conocido_no_se_duplica(client, auth_headers):
    _publicar(client, auth_headers)

    nuevo = client.post(
        "/v1/admin/chats/directory",
        json={"app_id": "pos", "business_id": "7", "phone": "311 555 0000", "name": "Nueva"},
        headers=PLATFORM,
    )
    conocido = client.post(
        "/v1/admin/chats/directory",
        json={"app_id": "pos", "business_id": "7", "phone": "3102647944"},
        headers=PLATFORM,
    )

    assert nuevo.status_code == 200
    assert nuevo.json()["phone"] == "573115550000"
    assert conocido.json()["name"] == "Paola Chávez"
    busqueda = client.get("/v1/admin/chats/directory", params={"q": "3102647944"}, headers=PLATFORM).json()
    assert len(busqueda) == 1


def test_numero_invalido_y_app_ajena(client, auth_headers):
    malo = client.post(
        "/v1/admin/chats/directory", json={"app_id": "pos", "phone": "no tengo cel"}, headers=PLATFORM
    )
    sin_sesion = client.get("/v1/admin/chats/directory", params={"q": "paola"})

    assert malo.status_code == 422
    assert sin_sesion.status_code == 401
