"""Motor de flujos (Fase 3c): el caso real que lo motivo - el Spa agenda
una cita, dispara el flujo por API con variables, el cliente recibe
botones (condiciones / garantias / gestionar en la web) y el motor
atiende cada respuesta, dejando tags en el contacto. Tambien: disparo por
keyword, interpolacion, validacion de definiciones y scoping."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from nexolu_comms_api.core.flows.engine import FlowDefinitionError, interpolate, validate_definition

MESSAGES_URL = "https://graph.facebook.com/v21.0/123456/messages"
# El webhook TAMBIEN reenvia el evento crudo al callback del POS (el motor
# de flujos es una capa adicional, no un secuestro) - cada _inbound mockea
# esa entrega.
CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"
CITA_FLOW = {
    "start": "menu",
    "nodes": {
        "menu": {
            "type": "buttons",
            "text": "Hola {{contact.name}}, tu cita de {{servicio}} quedó para {{fecha}}.",
            "buttons": [
                {"id": "cancelacion", "title": "Cancelaciones", "next": "cancelacion"},
                {"id": "garantias", "title": "Garantías", "next": "garantias"},
                {"id": "gestionar", "title": "Gestionar cita", "next": "gestionar"},
            ],
        },
        "cancelacion": {
            "type": "message",
            "text": "Puedes cancelar sin costo hasta 24h antes.",
            "add_tags": ["pregunto_cancelacion"],
        },
        "garantias": {"type": "message", "text": "Garantía de 8 días en semipermanente."},
        "gestionar": {
            "type": "cta_url",
            "text": "Gestiona tu cita aquí:",
            "url": "https://agenda.nexolu.co/{{slug}}",
            "button": "Abrir agenda",
        },
    },
}


# --- unidad: validacion e interpolacion ---------------------------------------


def test_validate_accepts_the_real_flow():
    validate_definition(CITA_FLOW)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda d: d.update(start="no_existe"), "start"),
        (lambda d: d["nodes"]["menu"].update(buttons=[]), "1 y 3"),
        (lambda d: d["nodes"]["menu"]["buttons"][0].update(next="fantasma"), "inexistente"),
        (lambda d: d["nodes"]["gestionar"].pop("url"), "url"),
        (lambda d: d["nodes"]["cancelacion"].pop("text"), "text"),
    ],
)
def test_validate_rejects_broken_definitions(mutation, expected):
    definition = json.loads(json.dumps(CITA_FLOW))
    mutation(definition)

    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_interpolation_resolves_paths_and_blanks_the_unknown():
    context = {"fecha": "mañana 3pm", "contact": {"name": "Laura"}}

    assert (
        interpolate("Hola {{contact.name}}, cita: {{ fecha }}. {{no_existe}}", context)
        == "Hola Laura, cita: mañana 3pm. "
    )


# --- el caso real, de punta a punta -------------------------------------------


def _create_flow(client, platform_headers, **overrides):
    payload = {
        "app_id": "pos",
        "name": "post_agenda",
        "trigger_type": "api",
        "definition": CITA_FLOW,
    }
    payload.update(overrides)
    return client.post("/v1/admin/flows", headers=platform_headers, json=payload)


def _trigger(client, auth_headers, **overrides):
    payload = {
        "flow": "post_agenda",
        "to": "573001112233",
        "variables": {"servicio": "Semi", "fecha": "mañana 3pm", "slug": "luxury-nails"},
        "contact_name": "Laura",
    }
    payload.update(overrides)
    return client.post("/v1/flows/trigger", headers=auth_headers, json=payload)


def _inbound(client, httpx_mock, body: dict):
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(b"meta-app-secret", raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp/pos",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


def _button_reply(button_id: str, title: str) -> dict:
    return {
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
                                    "id": "wamid.btn",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {"id": button_id, "title": title},
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }


def _text_message(text: str) -> dict:
    return {
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
                                    "id": "wamid.txt",
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


def test_the_spa_appointment_flow_end_to_end(client, platform_headers, auth_headers, httpx_mock):
    assert _create_flow(client, platform_headers).status_code == 201

    # 1. El Spa agenda la cita y dispara el flujo: sale el menu con botones
    #    interpolado y la sesion queda esperando.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.menu"}]})
    response = _trigger(client, auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"

    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert sent["type"] == "interactive"
    assert sent["interactive"]["type"] == "button"
    assert sent["interactive"]["body"]["text"] == "Hola Laura, tu cita de Semi quedó para mañana 3pm."
    titles = [b["reply"]["title"] for b in sent["interactive"]["action"]["buttons"]]
    assert titles == ["Cancelaciones", "Garantías", "Gestionar cita"]

    # 2. La clienta toca "Cancelaciones": el motor responde y le deja el tag.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.cancel"}]})
    assert _inbound(client, httpx_mock, _button_reply("cancelacion", "Cancelaciones")).status_code == 200

    reply = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[1].content)
    assert reply["type"] == "text"
    assert "cancelar sin costo" in reply["text"]["body"]

    contacts = client.get("/v1/admin/contacts", headers=platform_headers).json()["items"]
    assert contacts[0]["phone"] == "573001112233"
    assert contacts[0]["name"] == "Laura"
    assert "pregunto_cancelacion" in contacts[0]["tags"]


def test_the_web_button_sends_a_cta_url_with_interpolated_link(
    client, platform_headers, auth_headers, httpx_mock
):
    _create_flow(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.menu"}]})
    _trigger(client, auth_headers)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.web"}]})
    _inbound(client, httpx_mock, _button_reply("gestionar", "Gestionar cita"))

    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[1].content)
    assert sent["interactive"]["type"] == "cta_url"
    assert sent["interactive"]["action"]["parameters"] == {
        "display_text": "Abrir agenda",
        "url": "https://agenda.nexolu.co/luxury-nails",
    }


def test_an_unrelated_reply_leaves_the_session_waiting_and_stays_silent(
    client, platform_headers, auth_headers, httpx_mock
):
    """La conversacion es de la app duena: si la clienta escribe otra cosa,
    el motor NO insiste (nada de "no te entendi" en un canal que la app
    tambien atiende) - la sesion sigue esperando el boton."""
    _create_flow(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.menu"}]})
    _trigger(client, auth_headers)

    assert _inbound(client, httpx_mock, _text_message("¿me regalas la dirección?")).status_code == 200
    # Solo el mensaje del menu salio; el texto libre no genero respuesta.
    assert len(httpx_mock.get_requests(url=MESSAGES_URL)) == 1

    # Y el boton sigue funcionando despues.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.g"}]})
    _inbound(client, httpx_mock, _button_reply("garantias", "Garantías"))
    assert len(httpx_mock.get_requests(url=MESSAGES_URL)) == 2


def test_a_keyword_starts_a_flow_without_any_session(client, platform_headers, httpx_mock):
    _create_flow(
        client,
        platform_headers,
        name="menu_ayuda",
        trigger_type="keyword",
        trigger_keywords=["ayuda", "menu"],
    )

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.kw"}]})
    assert _inbound(client, httpx_mock, _text_message("  AYUDA ")).status_code == 200

    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert sent["interactive"]["type"] == "button"


def test_a_plain_text_without_keyword_does_nothing(client, platform_headers, httpx_mock):
    _create_flow(client, platform_headers, trigger_type="api")

    assert _inbound(client, httpx_mock, _text_message("hola")).status_code == 200
    assert httpx_mock.get_requests(url=MESSAGES_URL) == []


def test_triggering_an_unknown_or_inactive_flow_fails_clearly(client, platform_headers, auth_headers):
    assert _trigger(client, auth_headers).status_code == 404

    _create_flow(client, platform_headers, is_active=False)
    assert _trigger(client, auth_headers).status_code == 409


def test_a_broken_definition_bounces_at_save_time(client, platform_headers):
    definition = json.loads(json.dumps(CITA_FLOW))
    definition["start"] = "fantasma"

    response = _create_flow(client, platform_headers, definition=definition)

    assert response.status_code == 422
    assert "start" in response.json()["detail"]


def test_flow_sends_are_audited_as_notifications(client, platform_headers, auth_headers, httpx_mock):
    _create_flow(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.menu"}]})
    _trigger(client, auth_headers)

    listed = client.get(
        "/v1/platform/notifications", headers=platform_headers, params={"reference": "flow:post_agenda"}
    ).json()
    assert listed["total"] == 1
    assert listed["notifications"][0]["status"] == "sent"
