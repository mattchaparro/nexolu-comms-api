"""La bandeja / live chat: el historial de la conversacion (entrantes del
webhook + salientes del motor y del panel) y la respuesta humana desde el
panel - la UNICA via cuando el numero no tiene app movil ni SIM."""
from __future__ import annotations

import hashlib
import hmac
import json

MESSAGES_URL = "https://graph.facebook.com/v21.0/123456/messages"
CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"


def _inbound_text(client, httpx_mock, text: str):
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


def test_the_inbox_shows_the_full_thread_and_replies(client, platform_headers, httpx_mock):
    # 1. Entra un mensaje: la conversacion nace en la bandeja aunque ningun
    #    flujo lo atienda (el motor calla, la bandeja NO).
    assert _inbound_text(client, httpx_mock, "Hola, ¿tienen agenda para mañana?").status_code == 200

    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()
    assert len(conversations) == 1
    convo = conversations[0]
    assert convo["phone"] == "573001112233"
    assert convo["name"] == "Laura"
    assert "agenda para mañana" in convo["last_body"]
    assert convo["last_direction"] == "in"
    assert convo["window_open"] is True

    # 2. El negocio responde desde el panel (sin celular): sale por la misma
    #    identidad de WhatsApp y queda en el hilo como out/panel.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.reply"}]})
    sent = client.post(
        f"/v1/admin/chats/{convo['contact_id']}/messages",
        headers=platform_headers,
        json={"text": "¡Hola Laura! Sí, tenemos a las 3pm."},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["status"] == "sent"

    outbound = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert outbound["text"]["body"] == "¡Hola Laura! Sí, tenemos a las 3pm."

    # 3. El hilo completo, en orden.
    thread = client.get(
        f"/v1/admin/chats/{convo['contact_id']}/messages", headers=platform_headers
    ).json()
    assert [(m["direction"], m["origin"]) for m in thread] == [("in", ""), ("out", "panel")]

    # 4. Y la conversacion refleja el ultimo saliente.
    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()
    assert conversations[0]["last_direction"] == "out"


def test_flow_sends_land_in_the_same_thread(client, platform_headers, auth_headers, httpx_mock):
    """Lo que el motor responde tambien se lee en la bandeja: un solo hilo
    humano+bot."""
    client.post(
        "/v1/admin/flows",
        headers=platform_headers,
        json={
            "app_id": "pos",
            "name": "eco",
            "trigger_type": "keyword",
            "trigger_keywords": ["hola"],
            "definition": {
                "start": "r",
                "nodes": {"r": {"type": "message", "text": "¡Hola! Soy el bot."}},
            },
        },
    )

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.bot"}]})
    assert _inbound_text(client, httpx_mock, "hola").status_code == 200

    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()
    thread = client.get(
        f"/v1/admin/chats/{conversations[0]['contact_id']}/messages", headers=platform_headers
    ).json()
    assert [(m["direction"], m["origin"]) for m in thread] == [("in", ""), ("out", "flow")]
    assert thread[1]["body"] == "¡Hola! Soy el bot."
