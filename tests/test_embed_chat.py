"""La bandeja de Connect embebida en el panel de la app dueña.

Por qué existe. La bandeja se escribió dos veces: una en Connect y otra,
más pobre, dentro del Spa. Cada mejora -- buscar, plantillas, adjuntos,
la ficha del contacto -- había que hacerla dos veces o dejar una atrás.
Se hace una vez, en Connect, y el panel del Spa la muestra adentro.

Lo que se prueba acá no es que se vea bonito: es que un negocio NO pueda
leer las conversaciones de otro. Ese es el riesgo entero de esta
funcionalidad, porque hasta ahora la bandeja se filtraba solo por app --
suficiente cuando quien miraba era Nexolu, y un agujero el día que
quien mira es uno de los cuarenta salones.
"""
from __future__ import annotations

import hashlib
import hmac
import json

CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"


def _inbound(client, httpx_mock, auth_headers, phone: str, text: str, business_id: str):
    """Una conversación de un negocio concreto.

    El negocio no viene en el webhook de Meta -- ahí solo hay un número --
    sino de la app dueña: es ella quien sabe que este teléfono le escribe
    a Luxury Nails y no al salón de al lado, y lo declara al mandar. Se
    hace igual acá: entra el mensaje y la app contesta diciendo de quién
    es la conversación.
    """
    # Primero la app declara de quien es la conversacion -- al mandar, que
    # es cuando lo sabe -- y despues entra el mensaje de la clienta. En ese
    # orden queda sin leer, que es el estado que de verdad se mira en una
    # bandeja.
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/123456/messages",
        json={"messages": [{"id": f"wamid.out{abs(hash(phone))}"}]},
    )
    salida = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": business_id,
            "channels": ["whatsapp"],
            "to": {"whatsapp": phone},
            "text": "Hola, en que te ayudo?",
        },
    )
    assert salida.status_code == 200, salida.text

    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "123456"},
                            "contacts": [{"profile": {"name": f"Clienta {phone[-4:]}"}, "wa_id": phone}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": f"wamid.{abs(hash(text + phone))}",
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
    entrante = client.post(
        "/webhooks/whatsapp/pos",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )
    assert entrante.status_code == 200


def _embed_headers(client, business_id: str, auth_headers) -> dict[str, str]:
    minted = client.post(
        "/v1/embed/chat-token",
        json={"business_id": business_id},
        headers=auth_headers,
    )
    assert minted.status_code == 200, minted.text
    return {"Authorization": f"Bearer {minted.json()['token']}"}


def test_el_token_solo_lo_emite_una_app_autenticada(client, monkeypatch):
    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_BASE_URL", "https://connect.nexolu.test")

    # Sin credencial no se emite: este token es la llave de una bandeja.
    assert client.post("/v1/embed/chat-token", json={"business_id": "7"}).status_code == 401
    assert (
        client.post(
            "/v1/embed/chat-token",
            json={"business_id": "7"},
            headers={"Authorization": "Bearer llave-inventada"},
        ).status_code
        == 401
    )


def test_la_respuesta_trae_la_url_que_hay_que_embeber(client, auth_headers, monkeypatch):
    """Para que cambiar la ruta del panel no obligue a desplegar el Spa."""
    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_BASE_URL", "https://connect.nexolu.test/")

    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    cuerpo = client.post("/v1/embed/chat-token", json={"business_id": "7"}, headers=auth_headers).json()

    assert cuerpo["url"] == "https://connect.nexolu.test/embebido/chat"
    assert cuerpo["token"]


def test_un_negocio_no_ve_las_conversaciones_de_otro(client, auth_headers, httpx_mock, monkeypatch):
    """El agujero que abre esta funcionalidad, y la razón de las otras
    pruebas: hasta ahora la bandeja se filtraba SOLO por app. Con cuarenta
    salones en la app `spa`, eso es la bandeja de todos."""
    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_BASE_URL", "https://connect.nexolu.test")

    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola, soy de Luxury", business_id="7")
    _inbound(client, httpx_mock, auth_headers, "573009998877", "Hola, soy del otro salon", business_id="9")

    de_luxury = client.get("/v1/admin/chats", headers=_embed_headers(client, "7", auth_headers)).json()

    telefonos = [c["phone"] for c in de_luxury["items"]]
    assert telefonos == ["573001112233"]
    # Y el contador de pendientes tampoco puede contar los ajenos: es un
    # numero que se mira todo el dia.
    assert de_luxury["unread_total"] == 1


def test_adivinar_el_id_de_un_contacto_ajeno_da_404(client, auth_headers, httpx_mock, monkeypatch):
    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_BASE_URL", "https://connect.nexolu.test")

    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    _inbound(client, httpx_mock, auth_headers, "573009998877", "Hola, soy del otro salon", business_id="9")

    ajeno = client.get(
        "/v1/admin/chats", headers={"Authorization": "Bearer platform-key"}
    ).json()["items"][0]["contact_id"]

    headers = _embed_headers(client, "7", auth_headers)

    # 404 y no 403: no se le confirma a nadie que exista una conversacion
    # que no es suya.
    assert client.get(f"/v1/admin/chats/{ajeno}", headers=headers).status_code == 404
    assert client.get(f"/v1/admin/chats/{ajeno}/messages", headers=headers).status_code == 404
    assert client.post(f"/v1/admin/chats/{ajeno}/read", headers=headers).status_code == 404


def test_el_token_del_embebido_no_sirve_como_sesion_de_panel(client, auth_headers, monkeypatch):
    """Entra por el mismo header que la sesión del panel. Si en algún punto
    se confundiera con una, su portador pasaría de ver un salón a ver la
    plataforma entera."""
    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_BASE_URL", "https://connect.nexolu.test")

    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    headers = _embed_headers(client, "7", auth_headers)

    assert client.get("/panel/me", headers=headers).status_code == 401
