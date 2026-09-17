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


# --- condicion multi-rama (else-if de ManyChat) -------------------------------

# Enruta por la sede guardada en el custom field: el primer caso que
# matchee gana; sin match, el else.
CASES_FLOW = {
    "start": "sede",
    "nodes": {
        "sede": {
            "type": "condition",
            "cases": [
                {"when": {"field": "sede", "equals": "norte"}, "next": "norte"},
                {"when": {"field": "sede", "equals": "centro"}, "next": "centro"},
                {"when": {"tag": "vip"}, "next": "vip"},
            ],
            "else": "generico",
        },
        "norte": {"type": "message", "text": "Sede Norte: Cra 10 #20-30."},
        "centro": {"type": "message", "text": "Sede Centro: Cll 5 #4-50."},
        "vip": {"type": "message", "text": "Tu asesora VIP te escribe ya."},
        "generico": {"type": "message", "text": "¿En cuál sede quieres tu cita?"},
    },
}


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda d: d["nodes"]["sede"].update(cases=[]), "1 a 8"),
        (lambda d: d["nodes"]["sede"]["cases"][0].update(next="fantasma"), "inexistente"),
        (lambda d: d["nodes"]["sede"]["cases"][1].pop("when"), "'when'"),
        (lambda d: d["nodes"]["sede"].update(**{"else": "fantasma"}), "inexistente"),
    ],
)
def test_validate_rejects_broken_condition_cases(mutation, expected):
    definition = json.loads(json.dumps(CASES_FLOW))
    mutation(definition)
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_condition_cases_first_match_wins_and_else_falls_through(
    client, platform_headers, auth_headers, httpx_mock
):
    _create_flow(client, platform_headers, name="por_sede", definition=CASES_FLOW)

    # Sin campos ni tags: cae al else.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})
    _trigger(client, auth_headers, flow="por_sede")
    first = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert "cuál sede" in first["text"]["body"]

    # Con sede=centro Y tag vip: gana el caso de 'centro' (orden, no el vip).
    contacts = client.get("/v1/admin/contacts", headers=platform_headers).json()["items"]
    contact = next(c for c in contacts if c["phone"] == "573001112233")
    client.patch(
        f"/v1/admin/contacts/{contact['id']}",
        headers=platform_headers,
        json={"tags": ["vip"], "fields": {"sede": "Centro"}},
    )
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.2"}]})
    _trigger(client, auth_headers, flow="por_sede")
    second = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[1].content)
    assert second["text"]["body"] == "Sede Centro: Cll 5 #4-50."


# --- motor v2: random (aleatorizador A/B) -------------------------------------

RANDOM_FLOW = {
    "start": "hola",
    "nodes": {
        "hola": {"type": "message", "text": "¡Hola!", "next": "dado"},
        "dado": {
            "type": "random",
            "branches": [{"weight": 1, "next": "a"}, {"weight": 1, "next": "b"}],
        },
        "a": {"type": "message", "text": "Promo A"},
        "b": {"type": "message", "text": "Promo B"},
    },
}


def test_pick_random_branch_respects_weights():
    from nexolu_comms_api.core.flows.engine import _pick_random_branch

    node = {
        "type": "random",
        "branches": [{"weight": 1, "next": "raro"}, {"weight": 999999, "next": "casi_siempre"}],
    }
    picks = {_pick_random_branch(node) for _ in range(50)}
    assert "casi_siempre" in picks
    # Una rama sin next termina el flujo.
    assert _pick_random_branch({"branches": [{"weight": 1}]}) is None


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"start": "r", "nodes": {"r": {"type": "random"}}}, "branches"),
        (
            {"start": "r", "nodes": {"r": {"type": "random", "branches": [{"weight": 1}]}}},
            "entre 2 y 5",
        ),
        (
            {
                "start": "r",
                "nodes": {"r": {"type": "random", "branches": [{"weight": 0, "next": "r"}, {"weight": 1}]}},
            },
            "weight",
        ),
        (
            {
                "start": "r",
                "nodes": {"r": {"type": "random", "branches": [{"weight": 1, "next": "nope"}, {"weight": 1}]}},
            },
            "inexistente",
        ),
    ],
)
def test_validate_rejects_broken_random_nodes(definition, expected):
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_random_node_picks_a_branch_end_to_end(client, platform_headers, auth_headers, httpx_mock):
    _create_flow(client, platform_headers, name="ab_promo", definition=RANDOM_FLOW)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.2"}]})
    response = _trigger(client, auth_headers, flow="ab_promo")

    assert response.json()["status"] == "completed"
    sent = [json.loads(r.content)["text"]["body"] for r in httpx_mock.get_requests(url=MESSAGES_URL)]
    assert sent[0] == "¡Hola!"
    assert sent[1] in ("Promo A", "Promo B")


# --- bloques de contenido de Meta: media, list, capture -----------------------

CONTENT_FLOW = {
    "start": "foto",
    "nodes": {
        "foto": {
            "type": "media",
            "kind": "image",
            "url": "https://cdn.nexolu.co/promo/{{campana}}.jpg",
            "caption": "Nuestra promo de {{campana}} 💅",
            "next": "menu",
        },
        "menu": {
            "type": "list",
            "text": "¿Qué servicio te interesa?",
            "button": "Ver servicios",
            "rows": [
                {"id": "semi", "title": "Semipermanente", "description": "Desde $45.000", "next": "nombre"},
                {"id": "tradicional", "title": "Tradicional", "next": "nombre"},
            ],
        },
        "nombre": {
            "type": "capture",
            "text": "¿A nombre de quién agendamos?",
            "field": "nombre_cita",
            "next": "gracias",
        },
        "gracias": {"type": "message", "text": "¡Listo, {{contact.name}}! Te contactamos ya."},
    },
}


def _list_reply(row_id: str, title: str) -> dict:
    body = json.loads(json.dumps(_button_reply(row_id, title)))
    interactive = body["entry"][0]["changes"][0]["value"]["messages"][0]["interactive"]
    interactive["type"] = "list_reply"
    interactive["list_reply"] = interactive.pop("button_reply")
    return body


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"start": "m", "nodes": {"m": {"type": "media", "url": "https://x/y.jpg"}}}, "kind"),
        ({"start": "m", "nodes": {"m": {"type": "media", "kind": "image"}}}, "url"),
        ({"start": "l", "nodes": {"l": {"type": "list", "text": "x", "rows": []}}}, "1 y 10"),
        (
            {"start": "l", "nodes": {"l": {"type": "list", "text": "x", "rows": [{"id": "a"}]}}},
            "title",
        ),
        (
            {
                "start": "l",
                "nodes": {"l": {"type": "list", "text": "x", "rows": [{"id": "a", "title": "A", "next": "no"}]}},
            },
            "inexistente",
        ),
        ({"start": "c", "nodes": {"c": {"type": "capture", "text": "x"}}}, "field"),
    ],
)
def test_validate_rejects_broken_content_nodes(definition, expected):
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_media_list_and_capture_end_to_end(client, platform_headers, auth_headers, httpx_mock):
    """El circuito ManyChat completo: imagen -> menu de lista -> captura de
    texto libre al custom field -> mensaje final."""
    _create_flow(client, platform_headers, name="promo_servicios", definition=CONTENT_FLOW)

    # 1. Trigger: sale la imagen (interpolada) y el menu de lista; la sesion espera.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.img"}]})
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.list"}]})
    response = _trigger(
        client, auth_headers, flow="promo_servicios", variables={"campana": "septiembre"}
    )
    assert response.json()["status"] == "active"

    image = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert image["type"] == "image"
    assert image["image"]["link"] == "https://cdn.nexolu.co/promo/septiembre.jpg"
    assert "septiembre" in image["image"]["caption"]

    lista = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[1].content)
    assert lista["interactive"]["type"] == "list"
    assert lista["interactive"]["action"]["button"] == "Ver servicios"
    rows = lista["interactive"]["action"]["sections"][0]["rows"]
    assert rows[0] == {"id": "semi", "title": "Semipermanente", "description": "Desde $45.000"}

    # 2. Elige de la lista (list_reply): sale la pregunta de captura.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.q"}]})
    assert _inbound(client, httpx_mock, _list_reply("semi", "Semipermanente")).status_code == 200
    pregunta = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[2].content)
    assert "nombre" in pregunta["text"]["body"]

    # 3. Responde texto libre: queda en el custom field y sigue el flujo.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.fin"}]})
    assert _inbound(client, httpx_mock, _text_message("Carolina Restrepo")).status_code == 200

    contacts = client.get("/v1/admin/contacts", headers=platform_headers).json()["items"]
    contact = next(c for c in contacts if c["phone"] == "573001112233")
    assert contact["fields"]["nombre_cita"] == "Carolina Restrepo"

    final = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[3].content)
    assert final["text"]["body"].startswith("¡Listo, Laura!")


# --- nodo template: reabrir la conversacion fuera de la ventana de 24h --------

TEMPLATE_FLOW = {
    "start": "espera",
    "nodes": {
        "espera": {"type": "delay", "minutes": 2880, "next": "seguimiento"},
        "seguimiento": {
            "type": "template",
            "template": "post_visita",
            "language": "es",
            "params": ["{{contact.name}}", "{{servicio}}"],
        },
    },
}


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"start": "t", "nodes": {"t": {"type": "template"}}}, "template"),
        (
            {"start": "t", "nodes": {"t": {"type": "template", "template": "x", "params": [1]}}},
            "params",
        ),
    ],
)
def test_validate_rejects_broken_template_nodes(definition, expected):
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_template_node_sends_the_approved_template_with_params(
    client, platform_headers, auth_headers, httpx_mock
):
    """El caso real: delay de 2 dias -> la ventana de 24h murio -> el
    seguimiento sale como plantilla aprobada, con las variables del flujo."""
    _create_flow(client, platform_headers, name="post_visita_48h", definition=TEMPLATE_FLOW)

    response = _trigger(
        client, auth_headers, flow="post_visita_48h", variables={"servicio": "Semi"}
    )
    assert response.json()["status"] == "waiting"  # el delay quedo armado

    # Vence el delay: el worker retoma y envia la PLANTILLA.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.tpl"}]})
    assert _force_resume() == 1

    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert sent["type"] == "template"
    assert sent["template"]["name"] == "post_visita"
    assert sent["template"]["language"] == {"code": "es"}
    params = sent["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["Laura", "Semi"]


# --- nodo product: el catalogo dentro del flujo -------------------------------


def _setup_tienda(client, platform_headers) -> dict[str, str]:
    """App con WABA + catalogo configurados (mismo arreglo de test_catalog)."""
    created = client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "tienda"})
    api_key = created.json()["api_key"]
    client.post(
        "/v1/admin/apps/tienda/providers/meta-whatsapp",
        headers=platform_headers,
        json={
            "phone_number_id": "777000",
            "access_token": "tienda-token",
            "waba_id": "waba-tienda",
            "catalog_id": "cat-99",
            "meta_business_id": "biz-500",
        },
    )
    return {"Authorization": f"Bearer {api_key}"}


TIENDA_MESSAGES_URL = "https://graph.facebook.com/v21.0/777000/messages"

PRODUCT_FLOW = {
    "start": "oferta",
    "nodes": {
        "oferta": {
            "type": "product",
            "text": "Mira lo que te tenemos hoy:",
            "retailer_id": "b3-semi-clasico",
        },
    },
}

MPM_FLOW = {
    "start": "menu",
    "nodes": {
        "menu": {
            "type": "product",
            "text": "Nuestros servicios estrella:",
            "header": "Catálogo Luxury",
            "sections": [
                {"title": "Manos", "retailer_ids": ["b3-semi", "b3-tradicional"]},
                {"title": "Pies", "retailer_ids": ["b3-pedicure"]},
            ],
        },
    },
}


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"start": "p", "nodes": {"p": {"type": "product"}}}, "retailer_id"),
        (
            {"start": "p", "nodes": {"p": {"type": "product", "sections": [{"title": "x"}]}}},
            "retailer_ids",
        ),
        (
            {
                "start": "p",
                "nodes": {
                    "p": {
                        "type": "product",
                        "sections": [{"title": "x", "retailer_ids": [f"r{i}" for i in range(31)]}],
                    }
                },
            },
            "maximo 30",
        ),
    ],
)
def test_validate_rejects_broken_product_nodes(definition, expected):
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_product_node_sends_spm_and_mpm(client, platform_headers, httpx_mock):
    tienda_headers = _setup_tienda(client, platform_headers)
    client.post(
        "/v1/admin/flows",
        headers=platform_headers,
        json={"app_id": "tienda", "name": "oferta_dia", "trigger_type": "api", "definition": PRODUCT_FLOW},
    )
    client.post(
        "/v1/admin/flows",
        headers=platform_headers,
        json={"app_id": "tienda", "name": "menu_servicios", "trigger_type": "api", "definition": MPM_FLOW},
    )

    # SPM: un producto, con el catalog_id de la identidad de la app.
    httpx_mock.add_response(url=TIENDA_MESSAGES_URL, json={"messages": [{"id": "wamid.spm"}]})
    response = client.post(
        "/v1/flows/trigger",
        headers=tienda_headers,
        json={"flow": "oferta_dia", "to": "573001112233"},
    )
    assert response.json()["status"] == "completed"
    spm = json.loads(httpx_mock.get_requests(url=TIENDA_MESSAGES_URL)[0].content)
    assert spm["interactive"]["type"] == "product"
    assert spm["interactive"]["action"] == {
        "catalog_id": "cat-99",
        "product_retailer_id": "b3-semi-clasico",
    }

    # MPM: secciones con header, mismo catalogo.
    httpx_mock.add_response(url=TIENDA_MESSAGES_URL, json={"messages": [{"id": "wamid.mpm"}]})
    client.post(
        "/v1/flows/trigger",
        headers=tienda_headers,
        json={"flow": "menu_servicios", "to": "573001112233"},
    )
    mpm = json.loads(httpx_mock.get_requests(url=TIENDA_MESSAGES_URL)[1].content)
    assert mpm["interactive"]["type"] == "product_list"
    assert mpm["interactive"]["action"]["catalog_id"] == "cat-99"
    assert [s["title"] for s in mpm["interactive"]["action"]["sections"]] == ["Manos", "Pies"]


# --- nodo actions: el "Realiza las siguientes acciones..." de ManyChat --------

ACTIONS_FLOW = {
    "start": "consultar",
    "nodes": {
        "consultar": {
            "type": "actions",
            "next": "ofrecer",
            "actions": [
                {"type": "add_tags", "tags": ["consulto_disponibilidad"]},
                {"type": "set_fields", "fields": {"origen": "flujo"}},
                {
                    "type": "http_request",
                    "method": "GET",
                    "url": "https://api.luxurynails.test/disponibilidad?tel={{contact.phone}}",
                    "save": {"proxima_hora": "disponible.hora", "profesional": "disponible.con"},
                },
                {"type": "notify_app", "message": "{{contact.name}} consultó disponibilidad"},
            ],
        },
        "ofrecer": {
            "type": "message",
            "text": "Te sirve {{contact.fields.proxima_hora}} con {{contact.fields.profesional}}?",
        },
    },
}

JUMP_FLOW_A = {
    "start": "salto",
    "nodes": {
        "salto": {
            "type": "actions",
            "actions": [{"type": "start_flow", "flow": "destino"}],
        },
    },
}

JUMP_FLOW_B = {
    "start": "hola",
    "nodes": {"hola": {"type": "message", "text": "Llegaste al flujo destino."}},
}


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"start": "a", "nodes": {"a": {"type": "actions"}}}, "actions"),
        (
            {"start": "a", "nodes": {"a": {"type": "actions", "actions": [{"type": "nope"}]}}},
            "type en",
        ),
        (
            {"start": "a", "nodes": {"a": {"type": "actions", "actions": [{"type": "add_tags", "tags": []}]}}},
            "tags",
        ),
        (
            {
                "start": "a",
                "nodes": {"a": {"type": "actions", "actions": [{"type": "http_request", "url": "ftp://x"}]}},
            },
            "http",
        ),
        (
            {
                "start": "a",
                "nodes": {
                    "a": {
                        "type": "actions",
                        "actions": [
                            {"type": "start_flow", "flow": "x"},
                            {"type": "add_tags", "tags": ["t"]},
                        ],
                    }
                },
            },
            "ULTIMA",
        ),
    ],
)
def test_validate_rejects_broken_actions_nodes(definition, expected):
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_actions_node_runs_http_request_and_notifies_the_app(
    client, platform_headers, auth_headers, httpx_mock
):
    """El circuito de la 'Solicitud externa': tags + campos + GET a la API
    del negocio (respuesta -> custom fields) + evento firmado al callback,
    y el mensaje siguiente interpola lo guardado."""
    _create_flow(client, platform_headers, name="disponibilidad", definition=ACTIONS_FLOW)

    httpx_mock.add_response(
        url="https://api.luxurynails.test/disponibilidad?tel=573001112233",
        json={"disponible": {"hora": "mañana 3pm", "con": "María"}},
    )
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})  # notify_app
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})

    response = _trigger(client, auth_headers, flow="disponibilidad")
    assert response.json()["status"] == "completed"

    # El mensaje final interpola lo que la API respondio.
    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert sent["text"]["body"] == "Te sirve mañana 3pm con María?"

    # El contacto quedo con tag, campo propio y campos de la respuesta.
    contacts = client.get("/v1/admin/contacts", headers=platform_headers).json()["items"]
    contact = next(c for c in contacts if c["phone"] == "573001112233")
    assert "consulto_disponibilidad" in contact["tags"]
    assert contact["fields"]["origen"] == "flujo"
    assert contact["fields"]["profesional"] == "María"

    # La notificacion a la app: evento flow_notify FIRMADO al callback.
    notify = [r for r in httpx_mock.get_requests(url=CALLBACK_URL) if b"flow_notify" in r.content]
    assert len(notify) == 1
    body = json.loads(notify[0].content)
    assert body["message"] == "Laura consultó disponibilidad"
    assert notify[0].headers.get("X-Nexolu-Event") == "flow-notify"


def test_notify_app_action_also_emails_the_admins(
    client, platform_headers, auth_headers, httpx_mock
):
    """'Pidio hablar con un humano': ademas del evento al callback, correo
    directo a los admins/agentes por el canal de email de la app."""
    definition = {
        "start": "aviso",
        "nodes": {
            "aviso": {
                "type": "actions",
                "actions": [
                    {
                        "type": "notify_app",
                        "message": "{{contact.name}} pidió hablar con un humano",
                        "emails": ["admin@luxurynails.co", "agente@luxurynails.co"],
                    }
                ],
            },
        },
    }
    _create_flow(client, platform_headers, name="pide_humano", definition=definition)

    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m1>"})
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m2>"})

    response = _trigger(client, auth_headers, flow="pide_humano")
    assert response.json()["status"] == "completed"

    emails = httpx_mock.get_requests(url="https://api.brevo.com/v3/smtp/email")
    assert len(emails) == 2
    first = json.loads(emails[0].content)
    assert first["to"][0]["email"] == "admin@luxurynails.co"
    assert "pidió hablar con un humano" in first["subject"]
    assert "573001112233" in first["textContent"]


def test_start_flow_action_jumps_to_another_flow(
    client, platform_headers, auth_headers, httpx_mock
):
    _create_flow(client, platform_headers, name="origen", definition=JUMP_FLOW_A)
    _create_flow(client, platform_headers, name="destino", definition=JUMP_FLOW_B)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})
    _trigger(client, auth_headers, flow="origen")

    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert sent["text"]["body"] == "Llegaste al flujo destino."
    # La sesion del flujo origen quedo superseded por la del destino.
    assert sorted(_session_statuses()) == ["completed", "superseded"]


# --- nodo blocks: el paso "Enviar mensaje" de ManyChat ------------------------

# El caso real calcado de "Mis citas" de Luxury: imagen + texto con las
# variables de la cita + botones, todo en UN paso del canvas.
BLOCKS_FLOW = {
    "start": "cita",
    "nodes": {
        "cita": {
            "type": "blocks",
            "blocks": [
                {"type": "image", "url": "https://cdn.nexolu.co/logo.png"},
                {"type": "wait", "seconds": 1},
                {
                    "type": "text",
                    "text": "Hemos encontrado la siguiente cita:\n📅 Día: {{fecha}}\n⏰ Hora: {{hora}}",
                    "buttons": [
                        {"id": "reagendar", "title": "Reagendar cita", "next": "reagendada"},
                        {"id": "cancelar", "title": "Cancelar cita"},
                    ],
                },
            ],
        },
        "reagendada": {"type": "message", "text": "Listo, te reagendamos."},
    },
}

CAPTURE_BLOCKS_FLOW = {
    "start": "bienvenida",
    "nodes": {
        "bienvenida": {
            "type": "blocks",
            "next": "gracias",
            "blocks": [
                {"type": "text", "text": "¡Hola! Bienvenida a Luxury 💅"},
                {"type": "capture", "text": "¿Cuál es tu correo?", "field": "correo"},
            ],
        },
        "gracias": {"type": "message", "text": "Gracias, te escribimos a {{contact.fields.correo}}."},
    },
}


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"start": "b", "nodes": {"b": {"type": "blocks"}}}, "blocks"),
        (
            {"start": "b", "nodes": {"b": {"type": "blocks", "blocks": [{"type": "nope"}]}}},
            "type en",
        ),
        (
            {
                "start": "b",
                "nodes": {
                    "b": {
                        "type": "blocks",
                        "blocks": [
                            {"type": "list", "text": "x", "rows": [{"id": "a", "title": "A"}]},
                            {"type": "text", "text": "y"},
                        ],
                    }
                },
            },
            "ULTIMO",
        ),
        (
            {
                "start": "b",
                "nodes": {
                    "b": {
                        "type": "blocks",
                        "blocks": [
                            {"type": "text", "text": "x", "buttons": [{"id": "a", "title": "A"}]},
                            {"type": "capture", "text": "y", "field": "z"},
                        ],
                    }
                },
            },
            "ambigua",
        ),
        (
            {
                "start": "b",
                "nodes": {
                    "b": {
                        "type": "blocks",
                        "blocks": [
                            {"type": "text", "text": "x", "buttons": [{"id": "a", "title": "A"}]},
                            {"type": "text", "text": "y", "buttons": [{"id": "a", "title": "B"}]},
                        ],
                    }
                },
            },
            "se repite",
        ),
        (
            {"start": "b", "nodes": {"b": {"type": "blocks", "blocks": [{"type": "wait", "seconds": 99}]}}},
            "seconds",
        ),
    ],
)
def test_validate_rejects_broken_blocks_nodes(definition, expected):
    with pytest.raises(FlowDefinitionError, match=expected):
        validate_definition(definition)


def test_blocks_node_sends_the_stack_and_waits_on_buttons(
    client, platform_headers, auth_headers, httpx_mock
):
    _create_flow(client, platform_headers, name="mis_citas", definition=BLOCKS_FLOW)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.img"}]})
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.txt"}]})
    response = _trigger(
        client, auth_headers, flow="mis_citas", variables={"fecha": "jueves 18", "hora": "3 pm"}
    )
    assert response.json()["status"] == "active"  # espera el boton

    sent = [json.loads(r.content) for r in httpx_mock.get_requests(url=MESSAGES_URL)]
    assert sent[0]["type"] == "image"
    assert sent[1]["interactive"]["type"] == "button"
    assert "jueves 18" in sent[1]["interactive"]["body"]["text"]
    titles = [b["reply"]["title"] for b in sent[1]["interactive"]["action"]["buttons"]]
    assert titles == ["Reagendar cita", "Cancelar cita"]

    # El boton de un BLOQUE avanza igual que el de un nodo buttons.
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.ok"}]})
    _inbound(client, httpx_mock, _button_reply("reagendar", "Reagendar cita"))
    final = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[2].content)
    assert final["text"]["body"] == "Listo, te reagendamos."


def test_blocks_capture_saves_the_field_and_continues(
    client, platform_headers, auth_headers, httpx_mock
):
    _create_flow(client, platform_headers, name="alta", definition=CAPTURE_BLOCKS_FLOW)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.1"}]})
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.2"}]})
    response = _trigger(client, auth_headers, flow="alta")
    assert response.json()["status"] == "active"  # espera el correo

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.3"}]})
    _inbound(client, httpx_mock, _text_message("laura@mail.com"))

    contacts = client.get("/v1/admin/contacts", headers=platform_headers).json()["items"]
    contact = next(c for c in contacts if c["phone"] == "573001112233")
    assert contact["fields"]["correo"] == "laura@mail.com"

    final = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[2].content)
    assert final["text"]["body"] == "Gracias, te escribimos a laura@mail.com."


def test_flow_sends_are_audited_as_notifications(client, platform_headers, auth_headers, httpx_mock):
    _create_flow(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.menu"}]})
    _trigger(client, auth_headers)

    listed = client.get(
        "/v1/platform/notifications", headers=platform_headers, params={"reference": "flow:post_agenda"}
    ).json()
    assert listed["total"] == 1
    assert listed["notifications"][0]["status"] == "sent"
