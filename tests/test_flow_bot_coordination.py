"""Coordinacion flujos + bot de la app (el hibrido spa).

Tres garantias:
- El reenvio a la app lleva X-Nexolu-Flow-Handled: "1" si un flujo de
  Connect atendio el mensaje, "0" si nadie lo reclamo (el bot de la app
  puede contestar). Sin doble respuesta.
- Un flujo disparado por la app (con business_id y telefono con '+') y la
  respuesta del webhook (sin negocio, sin '+') caen en el MISMO contacto:
  los botones avanzan y el hilo no se parte.
- Lo que la app envia por API (/v1/notifications/send, el bot del spa)
  queda en el hilo de la bandeja como out/api.
"""
from __future__ import annotations

import hashlib
import hmac
import json

MESSAGES_URL = "https://graph.facebook.com/v21.0/123456/messages"
CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"


def _signed_inbound(client, message: dict, name: str = "Laura"):
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "123456"},
                            "contacts": [{"profile": {"name": name}, "wa_id": message["from"]}],
                            "messages": [message],
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


def _text(phone: str, text: str) -> dict:
    return {"from": phone, "id": f"wamid.{abs(hash(text))}", "type": "text", "text": {"body": text}}


def _button_reply(phone: str, button_id: str, title: str) -> dict:
    return {
        "from": phone,
        "id": f"wamid.{abs(hash(button_id))}",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}},
    }


def _create_keyword_flow(client, platform_headers) -> None:
    response = client.post(
        "/v1/admin/flows",
        headers=platform_headers,
        json={
            "app_id": "pos",
            "name": "saludo",
            "trigger_type": "keyword",
            "trigger_keywords": ["hola"],
            "definition": {
                "start": "m",
                "nodes": {"m": {"type": "message", "text": "¡Hola! Soy el flujo."}},
            },
        },
    )
    assert response.status_code == 201, response.text


def test_forward_says_when_a_flow_handled_the_message(client, platform_headers, httpx_mock):
    _create_keyword_flow(client, platform_headers)

    # 1. Keyword que matchea: el flujo responde y el reenvio avisa "1".
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.f"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "hola")).status_code == 200
    forward = httpx_mock.get_requests(url=CALLBACK_URL)[-1]
    assert forward.headers["X-Nexolu-Flow-Handled"] == "1"

    # 2. Texto libre que nadie reclama: "0" - el bot de la app puede ayudar.
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "quiero una cita")).status_code == 200
    forward = httpx_mock.get_requests(url=CALLBACK_URL)[-1]
    assert forward.headers["X-Nexolu-Flow-Handled"] == "0"


def test_app_triggered_flow_advances_when_the_reply_comes_without_business(
    client, platform_headers, auth_headers, httpx_mock
):
    """El bug del numero compartido: la app dispara con business_id (y hasta
    con '+'), el webhook llega sin negocio - el boton debe avanzar el MISMO
    contacto, no crear otro con el hilo partido."""
    client.post(
        "/v1/admin/flows",
        headers=platform_headers,
        json={
            "app_id": "pos",
            "name": "post_agenda",
            "trigger_type": "api",
            "definition": {
                "start": "q",
                "nodes": {
                    "q": {
                        "type": "buttons",
                        "text": "¿Confirmas tu cita?",
                        "buttons": [{"id": "si", "title": "Sí", "next": "ok"}],
                    },
                    "ok": {"type": "message", "text": "¡Confirmada!"},
                },
            },
        },
    )

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.q"}]})
    trigger = client.post(
        "/v1/flows/trigger",
        headers=auth_headers,
        json={"flow": "post_agenda", "to": "+573009998877", "business_id": "5"},
    )
    assert trigger.status_code == 200, trigger.text

    # La clienta toca el boton: llega por el webhook compartido (sin
    # negocio, sin '+').
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.ok"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _button_reply("573009998877", "si", "Sí")).status_code == 200

    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[-1].content)
    assert sent["text"]["body"] == "¡Confirmada!"
    assert httpx_mock.get_requests(url=CALLBACK_URL)[-1].headers["X-Nexolu-Flow-Handled"] == "1"

    # Un solo contacto/conversacion, con todo el hilo.
    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()["items"]
    assert len(conversations) == 1
    thread = client.get(
        f"/v1/admin/chats/{conversations[0]['contact_id']}/messages", headers=platform_headers
    ).json()
    assert [(m["direction"], m["origin"]) for m in thread] == [
        ("out", "flow"),
        ("in", ""),
        ("out", "flow"),
    ]


def test_writing_first_and_the_reply_later_are_ONE_thread(
    client, platform_headers, auth_headers, httpx_mock
):
    """El caso que partió el hilo en producción: la app escribe primero
    (con negocio declarado) y la persona responde después por el webhook
    (sin negocio). Si cada lado crea su contacto, la bandeja muestra lo que
    respondimos en un hilo y lo que preguntó la clienta en otro."""
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.first"}]})
    primero = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "channels": ["whatsapp"],
            "to": {"whatsapp": "573001112233"},
            "business_id": "1",
            "whatsapp_template": {"name": "hello_world", "language": "en_US"},
        },
    )
    assert primero.status_code == 200, primero.text

    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "Hola, ¿qué precios manejan?")).status_code == 200

    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()["items"]
    assert len(conversations) == 1, "el hilo se partió en dos contactos"
    thread = client.get(
        f"/v1/admin/chats/{conversations[0]['contact_id']}/messages", headers=platform_headers
    ).json()
    assert [m["direction"] for m in thread] == ["out", "in"]
    # Y el negocio que declaró la app queda pegado al contacto.
    assert conversations[0]["business_id"] == "1"


def test_api_sends_land_in_the_same_thread_as_out_api(
    client, platform_headers, auth_headers, httpx_mock
):
    """La respuesta del bot del spa (via /v1/notifications/send) se lee en
    la bandeja de Connect, en el mismo hilo del contacto."""
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "¿Tienen agenda?")).status_code == 200

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.bot"}]})
    sent = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "channels": ["whatsapp"],
            "to": {"whatsapp": "+573001112233"},
            "text": "¡Claro! Mañana tengo 3pm y 5pm.",
            "category": "service",
        },
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["results"][0]["status"] == "sent"

    conversations = client.get("/v1/admin/chats", headers=platform_headers).json()["items"]
    assert len(conversations) == 1
    thread = client.get(
        f"/v1/admin/chats/{conversations[0]['contact_id']}/messages", headers=platform_headers
    ).json()
    assert [(m["direction"], m["origin"]) for m in thread] == [("in", ""), ("out", "api")]
    assert thread[1]["body"] == "¡Claro! Mañana tengo 3pm y 5pm."


def test_the_app_bot_can_offer_tappable_options(client, auth_headers, httpx_mock):
    """Hasta ahora solo los flujos podian ofrecer botones: el bot de la app
    solo mandaba texto, y escribir la hora a mano es donde la gente
    abandona. Tres o menos = botones, cuatro o mas = lista."""
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.b"}]})
    botones = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "channels": ["whatsapp"],
            "to": {"whatsapp": "573001112233"},
            "text": "¿Cuál te sirve?",
            "whatsapp_options": {
                "options": [{"id": "10", "title": "10 am"}, {"id": "15", "title": "3 pm"}]
            },
        },
    )
    assert botones.status_code == 200, botones.text
    enviado = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[-1].content)
    assert enviado["interactive"]["type"] == "button"
    titulos = [b["reply"]["title"] for b in enviado["interactive"]["action"]["buttons"]]
    assert titulos == ["10 am", "3 pm"]

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.l"}]})
    lista = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "channels": ["whatsapp"],
            "to": {"whatsapp": "573001112233"},
            "text": "Estas son las horas libres:",
            "whatsapp_options": {
                "button": "Ver horas",
                "options": [
                    {"id": "9", "title": "9 am"},
                    {"id": "10", "title": "10 am"},
                    {"id": "13", "title": "1 pm"},
                    {"id": "17", "title": "5 pm", "description": "con María"},
                ],
            },
        },
    )
    assert lista.status_code == 200, lista.text
    enviado = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[-1].content)
    assert enviado["interactive"]["type"] == "list"
    assert enviado["interactive"]["action"]["button"] == "Ver horas"
    filas = enviado["interactive"]["action"]["sections"][0]["rows"]
    assert len(filas) == 4
    assert filas[-1]["description"] == "con María"


def test_un_keyword_interrumpe_un_flujo_a_medias(client, platform_headers, httpx_mock):
    """Quien escribe "menu" a mitad de camino esta pidiendo empezar de
    nuevo. Antes eso era silencio: la sesion seguia esperando una fila que
    ya nadie iba a tocar, y ni el flujo ni el bot contestaban."""
    _create_keyword_flow(client, platform_headers)
    client.post(
        "/v1/admin/flows",
        headers=platform_headers,
        json={
            "app_id": "pos",
            "name": "menu",
            "trigger_type": "keyword",
            "trigger_keywords": ["menu"],
            "definition": {
                "start": "m",
                "nodes": {
                    "m": {
                        "type": "buttons",
                        "text": "¿Qué necesitas?",
                        "buttons": [{"id": "a", "title": "Agendar"}],
                    }
                },
            },
        },
    )

    # Arranca el menú y queda esperando que toque un botón.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.m"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "menu")).status_code == 200

    # En vez de tocar, escribe otra vez el keyword: vuelve a empezar.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.m2"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "menu")).status_code == 200

    enviados = [json.loads(r.content) for r in httpx_mock.get_requests(url=MESSAGES_URL)]
    assert len(enviados) == 2, "el keyword no interrumpió el flujo a medias"
    assert httpx_mock.get_requests(url=CALLBACK_URL)[-1].headers["X-Nexolu-Flow-Handled"] == "1"


def test_lo_que_no_es_keyword_sigue_siendo_del_bot_de_la_app(
    client, platform_headers, httpx_mock
):
    """Interrumpir con un keyword no puede volverse "el motor se queda con
    todo": si escribe algo libre, el bot de la app tiene que poder ayudar."""
    _create_keyword_flow(client, platform_headers)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.h"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "hola")).status_code == 200

    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    assert _signed_inbound(client, _text("573001112233", "¿cuánto vale?")).status_code == 200

    assert httpx_mock.get_requests(url=CALLBACK_URL)[-1].headers["X-Nexolu-Flow-Handled"] == "0"
