"""La bandeja / live chat: el historial de la conversacion (entrantes del
webhook + salientes del motor y del panel) y la respuesta humana desde el
panel - la UNICA via cuando el numero no tiene app movil ni SIM."""
from __future__ import annotations

import asyncio
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

    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()["items"]
    assert len(conversations) == 1
    convo = conversations[0]
    assert convo["phone"] == "573001112233"
    assert convo["name"] == "Laura"
    assert "agenda para mañana" in convo["last_body"]
    assert convo["last_direction"] == "in"
    assert convo["window_open"] is True

    # 2. El negocio responde desde el panel (sin celular): sale por la misma
    #    identidad de WhatsApp y queda en el hilo como out/panel. Ademas
    #    avisa a la app duena (human_reply) para que su bot se calle.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.reply"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
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
    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()["items"]
    assert conversations[0]["last_direction"] == "out"


def _seed_template(name: str, status: str = "APPROVED", reason: str | None = None) -> None:
    from nexolu_comms_api.core.db.entities import WhatsAppTemplate
    from nexolu_comms_api.core.db.session import get_sessionmaker

    async def seed():
        async with get_sessionmaker()() as session:
            session.add(
                WhatsAppTemplate(
                    app_id="pos",
                    waba_id="",
                    name=name,
                    language="es",
                    category="UTILITY",
                    status=status,
                    reason=reason,
                )
            )
            await session.commit()

    asyncio.run(seed())


def test_the_panel_can_reopen_a_cold_conversation_with_a_template(
    client, platform_headers, httpx_mock
):
    """Fuera de la ventana de 24h el texto libre no entrega: la plantilla es
    la UNICA salida, y por eso el panel debe poder mandarla."""
    assert _inbound_text(client, httpx_mock, "¿Siguen abiertos?").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]
    _seed_template("recordatorio_cita")

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.tpl"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    sent = client.post(
        f"/v1/admin/chats/{contact_id}/messages",
        headers=platform_headers,
        json={"template": {"name": "recordatorio_cita", "language": "es", "params": ["Laura", "3pm"]}},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["message_type"] == "template"
    # El hilo guarda lo que el operador vio al enviar (el cuerpo lo tiene Meta).
    assert sent.json()["body"] == "[plantilla recordatorio_cita] Laura · 3pm"

    outbound = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert outbound["type"] == "template"
    assert outbound["template"]["name"] == "recordatorio_cita"
    assert outbound["template"]["components"][0]["parameters"][0]["text"] == "Laura"


def test_a_template_the_mirror_knows_is_rejected_never_reaches_meta(
    client, platform_headers, httpx_mock
):
    assert _inbound_text(client, httpx_mock, "Hola").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]
    _seed_template("promo_vieja", status="REJECTED", reason="Contenido promocional")

    refused = client.post(
        f"/v1/admin/chats/{contact_id}/messages",
        headers=platform_headers,
        json={"template": {"name": "promo_vieja", "language": "es"}},
    )
    assert refused.status_code == 409
    assert "REJECTED" in refused.json()["detail"]
    assert httpx_mock.get_requests(url=MESSAGES_URL) == []


def test_sending_needs_exactly_one_of_text_or_template(client, platform_headers, httpx_mock):
    assert _inbound_text(client, httpx_mock, "Hola").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]

    for body in ({}, {"text": "hola", "template": {"name": "x"}}):
        assert (
            client.post(
                f"/v1/admin/chats/{contact_id}/messages", headers=platform_headers, json=body
            ).status_code
            == 422
        )


def test_a_human_reply_from_the_panel_tells_the_app_to_hush_its_bot(
    client, platform_headers, httpx_mock
):
    """Sin este aviso el bot de la app contesta encima de la persona que
    esta atendiendo: el saliente del panel no pasa por la app."""
    assert _inbound_text(client, httpx_mock, "¿Tienen hora hoy?").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.h"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    sent = client.post(
        f"/v1/admin/chats/{contact_id}/messages",
        headers=platform_headers,
        json={"text": "Sí, a las 4pm te esperamos."},
    )
    assert sent.status_code == 201

    aviso = [
        r
        for r in httpx_mock.get_requests(url=CALLBACK_URL)
        if r.headers.get("X-Nexolu-Event") == "human-reply"
    ]
    assert len(aviso) == 1
    body = json.loads(aviso[0].content)
    assert body["event"] == "human_reply"
    assert body["contact"]["phone"] == "573001112233"
    assert body["message"]["text"] == "Sí, a las 4pm te esperamos."
    # Firmado como todo lo que sale hacia la app duena.
    assert "X-Nexolu-Signature" in aviso[0].headers


def test_an_inbound_message_leaves_the_thread_unread_until_someone_opens_it(
    client, platform_headers, httpx_mock
):
    """Sin "no leido" una clienta se queda sin respuesta y nadie se entera."""
    assert _inbound_text(client, httpx_mock, "¿Me pueden atender?").status_code == 200

    listed = client.get("/v1/admin/chats", headers=platform_headers).json()
    assert listed["unread_total"] == 1
    assert listed["items"][0]["unread"] is True
    contact_id = listed["items"][0]["contact_id"]

    assert (
        client.post(f"/v1/admin/chats/{contact_id}/read", headers=platform_headers).status_code
        == 204
    )
    listed = client.get("/v1/admin/chats", headers=platform_headers).json()
    assert listed["unread_total"] == 0
    assert listed["items"][0]["unread"] is False

    # Y si vuelve a escribir, vuelve a quedar pendiente.
    assert _inbound_text(client, httpx_mock, "¿Hola?").status_code == 200
    assert client.get("/v1/admin/chats", headers=platform_headers).json()["unread_total"] == 1


def test_replying_counts_as_reading(client, platform_headers, httpx_mock):
    assert _inbound_text(client, httpx_mock, "¿Tienen hora?").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.r"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    client.post(
        f"/v1/admin/chats/{contact_id}/messages",
        headers=platform_headers,
        json={"text": "Sí, a las 5pm."},
    )

    assert client.get("/v1/admin/chats", headers=platform_headers).json()["unread_total"] == 0


def test_the_inbox_searches_by_name_phone_or_what_was_said(
    client, platform_headers, httpx_mock
):
    """La busqueda de una bandeja real es "la señora que preguntó por
    acrílicas", no un id."""
    assert _inbound_text(client, httpx_mock, "¿Cuánto cuestan las acrílicas?").status_code == 200

    def buscar(q: str) -> list[dict]:
        return client.get(f"/v1/admin/chats?q={q}", headers=platform_headers).json()["items"]

    assert len(buscar("acrílicas")) == 1
    assert len(buscar("Laura")) == 1
    assert len(buscar("300111")) == 1
    assert buscar("pestañas") == []


def test_assigning_says_who_is_attending_and_can_be_released(
    client, platform_headers, httpx_mock
):
    assert _inbound_text(client, httpx_mock, "Hola").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]

    user = client.post(
        "/v1/admin/users",
        headers=platform_headers,
        json={"email": "recepcion@luxury.co", "full_name": "Recepción", "role": "platform"},
    ).json()

    assigned = client.post(
        f"/v1/admin/chats/{contact_id}/assign",
        headers=platform_headers,
        json={"user_id": user["id"]},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned_name"] == "Recepción"

    listed = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]
    assert listed["assigned_to"] == user["id"]
    assert listed["assigned_name"] == "Recepción"

    # Soltarla la devuelve a la bolsa comun.
    client.post(
        f"/v1/admin/chats/{contact_id}/assign", headers=platform_headers, json={"user_id": None}
    )
    assert client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0][
        "assigned_to"
    ] is None


def test_the_panel_can_send_an_image(client, platform_headers, httpx_mock):
    assert _inbound_text(client, httpx_mock, "¿Me muestras diseños?").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.img"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    sent = client.post(
        f"/v1/admin/chats/{contact_id}/messages",
        headers=platform_headers,
        json={
            "media": {
                "kind": "image",
                "url": "https://comms.nexolu.co/media/abc.jpg",
                "caption": "Estos son los diseños de esta semana",
            }
        },
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["message_type"] == "image"
    assert sent.json()["payload"]["media_url"] == "https://comms.nexolu.co/media/abc.jpg"

    outbound = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert outbound["type"] == "image"
    assert outbound["image"]["link"] == "https://comms.nexolu.co/media/abc.jpg"


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

    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()["items"]
    thread = client.get(
        f"/v1/admin/chats/{conversations[0]['contact_id']}/messages", headers=platform_headers
    ).json()
    assert [(m["direction"], m["origin"]) for m in thread] == [("in", ""), ("out", "flow")]
    assert thread[1]["body"] == "¡Hola! Soy el bot."
