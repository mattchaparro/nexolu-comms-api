"""La ficha del contacto en la bandeja y las respuestas guardadas.

Dos cosas que hacen que atender sea posible y no artesanal: saber quien es
esta persona ANTES de contestarle, y no escribir los precios veinte veces
al dia.

Lo que la ficha de Connect NO tiene, a proposito: citas, pedidos, saldos.
Eso lo sabe la app duena y lo pinta su propio panel al lado (principio
45). Connect solo sabe de la conversacion.
"""
from __future__ import annotations

import hashlib
import hmac
import json

CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"


def _inbound(client, httpx_mock, text: str):
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "123456"},
                            "contacts": [{"profile": {"name": "Laura"}, "wa_id": "573001112233"}],
                            "messages": [
                                {
                                    "from": "573001112233",
                                    "id": f"wamid.{abs(hash(text))}",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(b"meta-app-secret", raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp/pos",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


def _contact_id(client, platform_headers) -> str:
    return client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]


def test_la_ficha_dice_quien_es_sin_salirse_de_lo_que_connect_sabe(
    client, platform_headers, httpx_mock
):
    assert _inbound(client, httpx_mock, "¿Tienen hora hoy?").status_code == 200
    ficha = client.get(
        f"/v1/admin/chats/{_contact_id(client, platform_headers)}", headers=platform_headers
    ).json()

    assert ficha["name"] == "Laura"
    assert ficha["phone"] == "573001112233"
    assert ficha["window_open"] is True
    assert ficha["messages_in"] == 1
    assert ficha["messages_out"] == 0
    assert ficha["first_seen_at"] is not None
    # Nada de negocio: eso lo sabe la app dueña.
    assert "citas" not in ficha and "pedidos" not in ficha


def test_las_notas_y_los_tags_se_editan_desde_la_bandeja(
    client, platform_headers, httpx_mock
):
    """«Alérgica al acrílico» hay que verlo ANTES de contestar, no después."""
    assert _inbound(client, httpx_mock, "Hola").status_code == 200
    contact_id = _contact_id(client, platform_headers)
    # Cambiar el nombre le avisa a la app duena (contact_updated).
    httpx_mock.add_response(url="https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp", json={"ok": True})

    editada = client.patch(
        f"/v1/admin/chats/{contact_id}",
        headers=platform_headers,
        json={
            "notes": "Alérgica al acrílico. Siempre pide con María.",
            "tags": ["vip", "vip", " ", "alergias"],
            "name": "Laura Pérez",
        },
    )
    assert editada.status_code == 200, editada.text
    assert editada.json()["notes"].startswith("Alérgica")
    assert editada.json()["name"] == "Laura Pérez"
    # Sin duplicados ni vacíos: un tag repetido rompe cualquier filtro.
    assert editada.json()["tags"] == ["vip", "alergias"]

    # Y persiste.
    assert client.get(
        f"/v1/admin/chats/{contact_id}", headers=platform_headers
    ).json()["tags"] == ["vip", "alergias"]


def test_la_ficha_ajena_no_se_ve(client, platform_headers, httpx_mock):
    assert _inbound(client, httpx_mock, "Hola").status_code == 200

    assert client.get("/v1/admin/chats/noexiste", headers=platform_headers).status_code == 404


def test_una_respuesta_guardada_se_crea_y_se_corrige_por_su_atajo(client, platform_headers):
    creada = client.put(
        "/v1/admin/quick-replies",
        headers=platform_headers,
        json={"app_id": "pos", "shortcut": "precios", "text": "Manicure 45.000, pedicure 30.000"},
    )
    assert creada.status_code == 200, creada.text
    # Sin título explícito, el atajo sirve de nombre.
    assert creada.json()["title"] == "precios"

    corregida = client.put(
        "/v1/admin/quick-replies",
        headers=platform_headers,
        json={"app_id": "pos", "shortcut": "precios", "title": "Lista de precios", "text": "Actualizado"},
    )
    assert corregida.json()["id"] == creada.json()["id"], "guardar dos veces creó otra"
    assert corregida.json()["text"] == "Actualizado"

    listadas = client.get("/v1/admin/quick-replies", headers=platform_headers).json()
    assert len(listadas) == 1


def test_un_atajo_con_espacios_o_mayusculas_se_rechaza(client, platform_headers):
    # El atajo se teclea de corrido tras una barra: "/lista de precios" no
    # se puede escribir sin ambigüedad.
    assert (
        client.put(
            "/v1/admin/quick-replies",
            headers=platform_headers,
            json={"app_id": "pos", "shortcut": "lista de precios", "text": "x"},
        ).status_code
        == 422
    )


def test_una_respuesta_guardada_se_borra(client, platform_headers):
    creada = client.put(
        "/v1/admin/quick-replies",
        headers=platform_headers,
        json={"app_id": "pos", "shortcut": "horarios", "text": "Lunes a sábado 9 a 7"},
    ).json()

    assert (
        client.delete(
            f"/v1/admin/quick-replies/{creada['id']}", headers=platform_headers
        ).status_code
        == 204
    )
    assert client.get("/v1/admin/quick-replies", headers=platform_headers).json() == []
