"""Motor de flujos (Fase 3c): el caso real que lo motivo - el Spa agenda
una cita, dispara el flujo por API con variables, el cliente recibe
botones (condiciones / garantias / gestionar en la web) y el motor
atiende cada respuesta, dejando tags en el contacto. Tambien: disparo por
keyword, interpolacion, validacion de definiciones, scoping, y los nodos
`condition` (ramas por tag/field) y `delay` (waiting + worker de
reanudacion) del motor v2."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest

from nexolu_comms_api.core.flows.engine import (
    FlowDefinitionError,
    _evaluate_condition,
    interpolate,
    resume_due_sessions,
    validate_definition,
)

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


# --- motor v2: condition y delay ----------------------------------------------

# Primer contacto: saluda y marca `vip`; el proximo disparo toma la otra rama.
VIP_FLOW = {
    "start": "decide",
    "nodes": {
        "decide": {"type": "condition", "when": {"tag": "vip"}, "then": "de_nuevo", "else": "primera"},
        "primera": {"type": "message", "text": "Bienvenida por primera vez.", "add_tags": ["vip"]},
        "de_nuevo": {"type": "message", "text": "Hola de nuevo, {{contact.name}}."},
    },
}

DELAY_FLOW = {
    "start": "confirmacion",
    "nodes": {
        "confirmacion": {"type": "message", "text": "Tu cita quedó agendada.", "next": "espera"},
        "espera": {"type": "delay", "minutes": 60, "next": "recordatorio"},
        "recordatorio": {"type": "message", "text": "¡Te esperamos mañana, {{contact.name}}!"},
    },
}


@pytest.mark.parametrize(
    ("when", "context", "expected"),
    [
        ({"tag": "vip"}, {"contact": {"tags": ["vip"]}}, True),
        ({"tag": "vip"}, {"contact": {"tags": []}}, False),
        ({"not_tag": "vip"}, {"contact": {"tags": []}}, True),
        ({"field": "sede", "equals": " Norte "}, {"sede": "norte", "contact": {}}, True),
        ({"field": "sede", "not_equals": "sur"}, {"sede": "norte", "contact": {}}, True),
        ({"field": "contact.name", "contains": "lau"}, {"contact": {"name": "Laura"}}, True),
        # Nombre simple que no esta en el contexto: cae a contact.fields.
        ({"field": "ciudad", "equals": "cali"}, {"contact": {"fields": {"ciudad": "Cali"}}}, True),
        ({"field": "ciudad", "exists": True}, {"contact": {"fields": {}}}, False),
        ({"field": "ciudad", "exists": False}, {"contact": {"fields": {}}}, True),
    ],
)
def test_condition_evaluation(when, context, expected):
    assert _evaluate_condition({"type": "condition", "when": when}, context) is expected


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        # condition sin when / when invalido / sin ramas / rama fantasma.
        (
            {"start": "c", "nodes": {"c": {"type": "condition", "then": "c"}}},
            "'when'",
        ),
        (
            {"start": "c", "nodes": {"c": {"type": "condition", "when": {"tag": "x", "field": "y"}, "then": "c"}}},
            "no admite otras claves",
        ),
        (
            {"start": "c", "nodes": {"c": {"type": "condition", "when": {"field": "x"}, "then": "c"}}},
            "exactamente",
        ),
        (
            {"start": "c", "nodes": {"c": {"type": "condition", "when": {"tag": "x"}}}},
            "al menos una rama",
        ),
        (
            {"start": "c", "nodes": {"c": {"type": "condition", "when": {"tag": "x"}, "then": "nope"}}},
            "inexistente",
        ),
        # delay sin minutes / fuera de rango.
        (
            {"start": "d", "nodes": {"d": {"type": "delay"}}},
            "minutes",
        ),
        (
            {"start": "d", "nodes": {"d": {"type": "delay", "minutes": 999999}}},
            "minutes",
        ),
    ],
)
def test_validate_rejects_broken_v2_nodes(definition, expected):
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_validate_keeps_the_builder_ui_key():
    definition = json.loads(json.dumps(VIP_FLOW))
    definition["ui"] = {"positions": {"decide": {"x": 100, "y": 40}}}

    validate_definition(definition)  # el motor la ignora, el panel la usa


def test_condition_branches_by_tag_end_to_end(client, platform_headers, auth_headers, httpx_mock):
    _create_flow(client, platform_headers, name="saludo_vip", definition=VIP_FLOW)

    # Primer disparo: contacto sin tags -> rama else, y gana el tag vip.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})
    response = _trigger(client, auth_headers, flow="saludo_vip")
    assert response.json()["status"] == "completed"
    first = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert first["text"]["body"] == "Bienvenida por primera vez."

    # Segundo disparo: el tag ya esta -> rama then, con interpolacion.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.2"}]})
    _trigger(client, auth_headers, flow="saludo_vip")
    second = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[1].content)
    assert second["text"]["body"] == "Hola de nuevo, Laura."


def _force_resume(session_id: str | None = None) -> int:
    """Vence el delay (resume_at al pasado) y corre el worker, como si
    hubiera pasado la hora."""

    async def inner() -> int:
        from sqlalchemy import update

        from nexolu_comms_api.core.db.entities import FlowSession
        from nexolu_comms_api.core.db.session import get_sessionmaker

        async with get_sessionmaker()() as session:
            query = update(FlowSession).values(resume_at=datetime.utcnow() - timedelta(minutes=1))
            if session_id:
                query = query.where(FlowSession.id == session_id)
            await session.execute(query)
            await session.commit()
        return await resume_due_sessions()

    return asyncio.run(inner())


def _session_statuses() -> list[str]:
    async def inner() -> list[str]:
        from sqlalchemy import select

        from nexolu_comms_api.core.db.entities import FlowSession
        from nexolu_comms_api.core.db.session import get_sessionmaker

        async with get_sessionmaker()() as session:
            rows = await session.execute(
                select(FlowSession.status).order_by(FlowSession.started_at)
            )
            return [row[0] for row in rows]

    return asyncio.run(inner())


def test_delay_waits_and_the_worker_resumes(client, platform_headers, auth_headers, httpx_mock):
    _create_flow(client, platform_headers, name="recordatorio", definition=DELAY_FLOW)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})
    response = _trigger(client, auth_headers, flow="recordatorio")
    assert response.json()["status"] == "waiting"
    # Solo salio la confirmacion; el recordatorio espera su hora.
    assert len(httpx_mock.get_requests(url=MESSAGES_URL)) == 1

    # Antes de vencerse, el worker no toca nada.
    assert asyncio.run(resume_due_sessions()) == 0

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.2"}]})
    assert _force_resume() == 1

    reminder = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[1].content)
    assert reminder["text"]["body"] == "¡Te esperamos mañana, Laura!"
    assert _session_statuses() == ["completed"]


def test_a_new_flow_supersedes_a_waiting_delay(client, platform_headers, auth_headers, httpx_mock):
    """ManyChat-semantica: el flujo mas reciente gana - un delay pendiente
    no debe despertar si otro flujo ya tomo la conversacion."""
    _create_flow(client, platform_headers, name="recordatorio", definition=DELAY_FLOW)
    _create_flow(client, platform_headers)  # post_agenda (botones)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})
    _trigger(client, auth_headers, flow="recordatorio")

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.2"}]})
    _trigger(client, auth_headers)  # el nuevo flujo reemplaza al delay

    assert _session_statuses() == ["superseded", "active"]
    # Aunque el delay "venza", el worker no lo toca: ya no esta waiting.
    assert _force_resume() == 0
    assert len(httpx_mock.get_requests(url=MESSAGES_URL)) == 2


def test_flow_sends_are_audited_as_notifications(client, platform_headers, auth_headers, httpx_mock):
    _create_flow(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.menu"}]})
    _trigger(client, auth_headers)

    listed = client.get(
        "/v1/platform/notifications", headers=platform_headers, params={"reference": "flow:post_agenda"}
    ).json()
    assert listed["total"] == 1
    assert listed["notifications"][0]["status"] == "sent"
