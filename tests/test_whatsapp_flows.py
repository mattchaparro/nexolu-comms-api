"""Formularios de WhatsApp (WhatsApp Flows de Meta): validador local,
ciclo de vida contra Graph API simulada (crear borrador -> subir JSON ->
publicar / deprecar / borrar), sincronizacion, biblioteca, generacion con IA
con reintentos guiados por errores, y auto-provision al conectar un canal."""
from __future__ import annotations

import copy
import json
import re

import pytest

from nexolu_comms_api.core.whatsapp_flows.library import LIBRARY, fresh_json
from nexolu_comms_api.core.whatsapp_flows.validator import has_errors, validate_flow_json

GRAPH = "https://graph.facebook.com/v21.0"
FLOWS_URL = f"{GRAPH}/waba-pos/flows"
ASSETS_URL = f"{GRAPH}/flow-1/assets"
FLOW_DETAIL = re.compile(r"https://graph\.facebook\.com/v21\.0/flow-1\?fields=.*")
IA_CORE_URL = "https://ia.nexolu.test/v1/completions"
POS_CALLBACK = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"


def _valid_json() -> dict:
    return fresh_json(LIBRARY["confirm_booking"])


def _layout_children(flow_json: dict) -> list:
    return flow_json["screens"][0]["layout"]["children"][0]["children"]


# --- validador local -----------------------------------------------------------


def test_the_library_booking_form_passes_the_local_validator():
    issues = validate_flow_json(_valid_json())

    assert not has_errors(issues)
    # El label largo de "para quien" es solo advertencia: Meta lo acepto.
    assert all(issue.severity == "warning" for issue in issues)


def test_init_value_on_a_component_is_rejected_with_the_fix():
    flow_json = _valid_json()
    _layout_children(flow_json)[3]["init-value"] = "Ana"

    issues = validate_flow_json(flow_json)

    assert has_errors(issues)
    assert any("init-values" in issue.message for issue in issues)


def test_an_unknown_component_name_is_reported_with_its_path():
    flow_json = _valid_json()
    _layout_children(flow_json)[2]["type"] = "DropDown"

    issues = validate_flow_json(flow_json)

    assert issues[0].path == "screens[0].layout.children[0].children[2].type"
    assert "DropDown" in issues[0].message


def test_a_terminal_screen_must_close_with_complete():
    flow_json = _valid_json()
    footer = _layout_children(flow_json)[5]
    footer["on-click-action"] = {"name": "navigate", "next": {"type": "screen", "name": "OTRA"}}

    messages = [issue.message for issue in validate_flow_json(flow_json)]

    assert any("complete" in message for message in messages)
    assert any("OTRA" in message for message in messages)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: c[2].update({"data-source": [{"id": "1", "title": "x" * 31}]}), "30"),
        (lambda c: c[5].update({"label": "y" * 36}), "35"),
        (lambda c: c[0].update({"text": "${data.no_declarado}"}), "no_declarado"),
        (lambda c: c[5]["on-click-action"]["payload"].update({"z": "${form.fantasma}"}), "fantasma"),
        (lambda c: c[5].update({"on-click-action": {"name": "data_exchange"}}), "endpoint"),
    ],
)
def test_semantic_rules(mutate, expected):
    flow_json = _valid_json()
    mutate(_layout_children(flow_json))

    issues = validate_flow_json(flow_json)

    assert has_errors(issues)
    assert any(expected in issue.message for issue in issues if issue.severity == "error")


def test_data_without_example_is_an_error():
    flow_json = _valid_json()
    del flow_json["screens"][0]["data"]["fecha"]["__example__"]

    assert any("__example__" in issue.message for issue in validate_flow_json(flow_json))


def test_the_validate_endpoint_runs_only_locally(client, platform_headers):
    flow_json = _valid_json()
    _layout_children(flow_json)[3]["init-value"] = "Ana"

    response = client.post(
        "/v1/admin/whatsapp-flows/validate", headers=platform_headers, json={"flow_json": flow_json}
    )

    assert response.status_code == 200
    assert any(issue["severity"] == "error" for issue in response.json()["issues"])


# --- ciclo de vida contra Meta ---------------------------------------------------


def _configure_pos_waba(client, platform_headers):
    client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "pos"})
    response = client.post(
        "/v1/admin/apps/pos/providers/meta-whatsapp",
        headers=platform_headers,
        json={
            "phone_number_id": "123456",
            "access_token": "wa-token",
            "waba_id": "waba-pos",
            "meta_app_secret": "meta-app-secret",
            "callback_url": POS_CALLBACK,
            "callback_secret": "pos-callback-secret",
        },
    )
    assert response.status_code == 201


def _mock_meta_draft(httpx_mock, *, validation_errors=None, status="DRAFT"):
    httpx_mock.add_response(url=FLOWS_URL, method="POST", json={"id": "flow-1"})
    httpx_mock.add_response(
        url=ASSETS_URL,
        method="POST",
        json={"success": True, "validation_errors": validation_errors or []},
        is_reusable=True,
    )
    _mock_detail(httpx_mock, status=status, validation_errors=validation_errors)


def _mock_detail(httpx_mock, *, status="DRAFT", validation_errors=None):
    httpx_mock.add_response(
        url=FLOW_DETAIL,
        method="GET",
        json={
            "id": "flow-1",
            "name": "confirmar_cita",
            "status": status,
            "categories": ["APPOINTMENT_BOOKING"],
            "validation_errors": validation_errors or [],
            "json_version": "7.2",
            "preview": {"preview_url": "https://business.facebook.com/wa/manage/flows/flow-1/preview/?token=t", "expires_at": "2026-10-22T00:00:00+0000"},
        },
        is_reusable=True,
    )


def _create(client, platform_headers, flow_json=None, name="confirmar_cita"):
    return client.post(
        "/v1/admin/whatsapp-flows",
        headers=platform_headers,
        json={
            "app_id": "pos",
            "name": name,
            "categories": ["APPOINTMENT_BOOKING"],
            "flow_json": flow_json or _valid_json(),
        },
    )


def test_creating_a_form_makes_a_draft_uploads_the_json_and_mirrors_the_preview(
    client, platform_headers, httpx_mock
):
    _configure_pos_waba(client, platform_headers)
    _mock_meta_draft(httpx_mock)

    response = _create(client, platform_headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["meta_flow_id"] == "flow-1"
    assert body["json_version"] == "7.2"
    assert body["preview_url"].startswith("https://business.facebook.com/")
    assert body["flow_json"]["screens"][0]["id"] == "CONFIRMAR"

    created = json.loads(httpx_mock.get_requests(url=FLOWS_URL)[0].content)
    assert created == {"name": "confirmar_cita", "categories": ["APPOINTMENT_BOOKING"]}

    upload = httpx_mock.get_requests(url=ASSETS_URL)[0]
    assert upload.headers["Authorization"] == "Bearer wa-token"
    assert upload.headers["Content-Type"].startswith("multipart/form-data")
    content = upload.content.decode()
    assert 'name="asset_type"' in content and "FLOW_JSON" in content
    assert 'filename="flow.json"' in content
    assert '"init-values"' in content


def test_local_errors_block_the_upload_without_calling_meta(client, platform_headers):
    _configure_pos_waba(client, platform_headers)
    flow_json = _valid_json()
    _layout_children(flow_json)[3]["init-value"] = "Ana"

    response = _create(client, platform_headers, flow_json)

    # Sin respuestas registradas en httpx_mock: cualquier llamada a Meta
    # habria reventado el test.
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("init-values" in issue["message"] for issue in detail["issues"])


def test_meta_validation_errors_are_stored_and_block_publishing(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    meta_error = {
        "error": "INVALID_PROPERTY_VALUE",
        "error_type": "FLOW_JSON_ERROR",
        "message": "Invalid value found for property 'label'.",
        "line_start": 10,
        "column_start": 21,
        "pointers": [{"path": "screens[0].layout.children[0]"}],
    }
    _mock_meta_draft(httpx_mock, validation_errors=[meta_error])

    body = _create(client, platform_headers).json()

    meta = [issue for issue in body["validation_errors"] if issue["source"] == "meta"]
    assert meta[0]["message"].startswith("INVALID_PROPERTY_VALUE")
    assert "linea 10" in meta[0]["path"]
    assert meta[0]["line"] == 10

    response = client.post(f"/v1/admin/whatsapp-flows/{body['id']}/publish", headers=platform_headers)
    assert response.status_code == 409


def test_publish_marks_published_and_a_published_form_cannot_be_edited_or_deleted(
    client, platform_headers, httpx_mock
):
    _configure_pos_waba(client, platform_headers)
    httpx_mock.add_response(url=FLOWS_URL, method="POST", json={"id": "flow-1"})
    httpx_mock.add_response(url=ASSETS_URL, method="POST", json={"success": True, "validation_errors": []})
    # Primer detalle (tras subir): borrador. Segundo (tras publicar): publicado.
    _mock_detail_once(httpx_mock, status="DRAFT")
    _mock_detail_once(httpx_mock, status="PUBLISHED")
    httpx_mock.add_response(url=f"{GRAPH}/flow-1/publish", method="POST", json={"success": True})
    flow_id = _create(client, platform_headers).json()["id"]

    published = client.post(f"/v1/admin/whatsapp-flows/{flow_id}/publish", headers=platform_headers)
    assert published.json()["status"] == "PUBLISHED"

    edit = client.put(
        f"/v1/admin/whatsapp-flows/{flow_id}/json", headers=platform_headers, json={"flow_json": _valid_json()}
    )
    assert edit.status_code == 409
    assert client.delete(f"/v1/admin/whatsapp-flows/{flow_id}", headers=platform_headers).status_code == 409

    httpx_mock.add_response(url=f"{GRAPH}/flow-1/deprecate", method="POST", json={"success": True})
    deprecated = client.post(f"/v1/admin/whatsapp-flows/{flow_id}/deprecate", headers=platform_headers)
    assert deprecated.json()["status"] == "DEPRECATED"


def _mock_detail_once(httpx_mock, *, status):
    httpx_mock.add_response(
        url=FLOW_DETAIL,
        method="GET",
        json={"id": "flow-1", "status": status, "validation_errors": [], "json_version": "7.2"},
    )


def test_deleting_a_draft_deletes_it_in_meta(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_meta_draft(httpx_mock)
    flow_id = _create(client, platform_headers).json()["id"]
    httpx_mock.add_response(url=f"{GRAPH}/flow-1", method="DELETE", json={"success": True})

    response = client.delete(f"/v1/admin/whatsapp-flows/{flow_id}", headers=platform_headers)

    assert response.status_code == 204
    assert client.get("/v1/admin/whatsapp-flows", headers=platform_headers).json()["items"] == []


def test_a_duplicate_name_in_the_same_waba_is_409_without_calling_meta(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_meta_draft(httpx_mock)
    assert _create(client, platform_headers).status_code == 201

    assert _create(client, platform_headers).status_code == 409
    assert len(httpx_mock.get_requests(url=FLOWS_URL)) == 1


def test_sync_mirrors_forms_made_in_whatsapp_manager_with_their_json(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    httpx_mock.add_response(
        url=f"{FLOWS_URL}?fields=id%2Cname%2Cstatus%2Ccategories%2Cvalidation_errors&limit=200",
        json={
            "data": [
                {
                    "id": "flow-9",
                    "name": "encuesta",
                    "status": "PUBLISHED",
                    "categories": ["SURVEY"],
                    "validation_errors": [],
                }
            ]
        },
    )
    httpx_mock.add_response(
        url=f"{GRAPH}/flow-9/assets",
        json={"data": [{"name": "flow.json", "asset_type": "FLOW_JSON", "download_url": "https://cdn.meta.test/flow.json"}]},
    )
    httpx_mock.add_response(url="https://cdn.meta.test/flow.json", json=_valid_json())

    response = client.post("/v1/admin/whatsapp-flows/sync", headers=platform_headers, json={"app_id": "pos"})

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["meta_flow_id"] == "flow-9"
    assert item["status"] == "PUBLISHED"
    assert item["categories"] == ["SURVEY"]
    assert item["flow_json"]["version"] == "7.2"


def test_a_meta_failure_is_502(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    httpx_mock.add_response(url=FLOWS_URL, method="POST", status_code=400, json={"error": {"message": "sin permiso"}})

    response = _create(client, platform_headers)

    assert response.status_code == 502
    assert "sin permiso" in response.json()["detail"]


def test_a_client_never_sees_another_apps_forms(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_meta_draft(httpx_mock)
    flow_id = _create(client, platform_headers).json()["id"]

    from nexolu_comms_api.core.auth.panel import PanelScope

    other = PanelScope(app_ids=("spa",))
    from nexolu_comms_api.core.auth.dependencies import get_panel_scope

    client.app.dependency_overrides[get_panel_scope] = lambda: other
    try:
        assert client.get("/v1/admin/whatsapp-flows", headers=platform_headers).json()["items"] == []
        assert client.get(f"/v1/admin/whatsapp-flows/{flow_id}", headers=platform_headers).status_code == 404
    finally:
        client.app.dependency_overrides.clear()


# --- biblioteca y auto-provision ---------------------------------------------------


def test_creating_from_the_library_publishes_and_is_idempotent(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_meta_draft(httpx_mock)
    httpx_mock.add_response(url=f"{GRAPH}/flow-1/publish", method="POST", json={"success": True})
    payload = {"app_id": "pos", "key": "confirm_booking", "publish": True}

    first = client.post("/v1/admin/whatsapp-flows/from-library", headers=platform_headers, json=payload)
    second = client.post("/v1/admin/whatsapp-flows/from-library", headers=platform_headers, json=payload)

    assert first.status_code == 201, first.text
    assert first.json()["library_key"] == "confirm_booking"
    assert first.json()["published_at"] is not None
    assert second.json()["id"] == first.json()["id"]
    assert len(httpx_mock.get_requests(url=FLOWS_URL)) == 1


async def test_autoprovision_creates_publishes_and_tells_the_owner_app(app_env, monkeypatch, httpx_mock):
    monkeypatch.setenv("WHATSAPP_FLOW_AUTOPROVISION", json.dumps({"pos": ["confirm_booking"]}))
    from tests.conftest import _clear_caches

    _clear_caches()
    from nexolu_comms_api.core.db.entities import BusinessChannel
    from nexolu_comms_api.core.db.session import get_sessionmaker, init_models
    from nexolu_comms_api.core.whatsapp_flows.provisioning import autoprovision_for_channel

    await init_models()
    async with get_sessionmaker()() as session:
        session.add(
            BusinessChannel(
                app_id="pos",
                business_id="42",
                waba_id="waba-42",
                phone_number_id="555",
                access_token="biz-token",
                status="active",
            )
        )
        await session.commit()

    httpx_mock.add_response(url=f"{GRAPH}/waba-42/flows", method="POST", json={"id": "flow-1"})
    httpx_mock.add_response(url=ASSETS_URL, method="POST", json={"success": True, "validation_errors": []})
    _mock_detail_once(httpx_mock, status="DRAFT")
    _mock_detail_once(httpx_mock, status="PUBLISHED")
    httpx_mock.add_response(url=f"{GRAPH}/flow-1/publish", method="POST", json={"success": True})
    httpx_mock.add_response(url=POS_CALLBACK, method="POST", json={"ok": True})

    await autoprovision_for_channel("pos", "42")

    upload = httpx_mock.get_requests(url=ASSETS_URL)[0]
    assert upload.headers["Authorization"] == "Bearer biz-token"
    event = json.loads(httpx_mock.get_requests(url=POS_CALLBACK)[0].content)
    assert event["event"] == "whatsapp_flow_provisioned"
    assert event["business_id"] == "42"
    assert event["flow_id"] == "flow-1"
    assert event["status"] == "PUBLISHED"


async def test_autoprovision_does_nothing_for_apps_that_did_not_ask(app_env):
    from nexolu_comms_api.core.whatsapp_flows.provisioning import autoprovision_for_channel

    await autoprovision_for_channel("pos", "42")  # sin respuestas registradas: ninguna llamada


# --- generacion con IA ------------------------------------------------------------


@pytest.fixture
def ia_core(monkeypatch):
    monkeypatch.setenv("IA_CORE_BASE_URL", "https://ia.nexolu.test")
    monkeypatch.setenv("IA_CORE_API_KEY", "connect-key")
    from tests.conftest import _clear_caches

    _clear_caches()


def _ia_reply(httpx_mock, flow_json_or_text):
    text = flow_json_or_text if isinstance(flow_json_or_text, str) else json.dumps(flow_json_or_text)
    httpx_mock.add_response(url=IA_CORE_URL, method="POST", json={"text": text})


def _generate(client, platform_headers):
    return client.post(
        "/v1/admin/whatsapp-flows/generate",
        headers=platform_headers,
        json={
            "app_id": "pos",
            "name": "confirmar_cita",
            "categories": ["APPOINTMENT_BOOKING"],
            "description": "formulario para confirmar cita: servicio y fecha de solo lectura, hora de una lista, nombre opcional",
        },
    )


def test_generation_retries_with_local_then_meta_errors_until_valid(ia_core, client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    broken = _valid_json()
    _layout_children(broken)[3]["init-value"] = "Ana"
    _ia_reply(httpx_mock, "```json\n" + json.dumps(broken) + "\n```")
    _ia_reply(httpx_mock, _valid_json())
    _ia_reply(httpx_mock, _valid_json())

    httpx_mock.add_response(url=FLOWS_URL, method="POST", json={"id": "flow-1"})
    meta_error = {"error": "INVALID_PROPERTY_VALUE", "message": "Label demasiado largo."}
    httpx_mock.add_response(url=ASSETS_URL, method="POST", json={"validation_errors": [meta_error]})
    httpx_mock.add_response(url=ASSETS_URL, method="POST", json={"validation_errors": []})
    _mock_detail(httpx_mock)

    response = _generate(client, platform_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["attempts"] == 3
    assert body["flow"]["meta_flow_id"] == "flow-1"
    assert not [i for i in body["flow"]["validation_errors"] if i["source"] == "meta"]
    # Un solo borrador en Meta, dos subidas del JSON.
    assert len(httpx_mock.get_requests(url=FLOWS_URL)) == 1
    assert len(httpx_mock.get_requests(url=ASSETS_URL)) == 2

    prompts = [json.loads(r.content) for r in httpx_mock.get_requests(url=IA_CORE_URL)]
    assert prompts[0]["context"]["user_id"] == "connect-panel"
    assert "init-values" in prompts[1]["user"]  # el error local viajo al reintento
    assert "Label demasiado largo" in prompts[2]["user"]  # y el de Meta tambien
    assert httpx_mock.get_requests(url=IA_CORE_URL)[0].headers["Authorization"] == "Bearer connect-key"


def test_generation_gives_up_after_two_retries_and_returns_the_last_attempt(
    ia_core, client, platform_headers, httpx_mock
):
    _configure_pos_waba(client, platform_headers)
    for _ in range(3):
        _ia_reply(httpx_mock, "no se me ocurrio nada")

    body = _generate(client, platform_headers).json()

    assert body["ok"] is False
    assert body["attempts"] == 3
    assert body["flow"] is None  # nunca paso lo local: nada se creo en Meta
    assert "JSON" in body["issues"][0]["message"]


def test_generation_without_ia_core_is_503(client, platform_headers):
    _configure_pos_waba(client, platform_headers)

    assert _generate(client, platform_headers).status_code == 503


def test_regenerating_reuses_the_existing_draft(ia_core, client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_meta_draft(httpx_mock)
    flow_id = _create(client, platform_headers).json()["id"]
    changed = copy.deepcopy(_valid_json())
    _layout_children(changed)[5]["label"] = "Confirmar"
    _ia_reply(httpx_mock, changed)

    response = client.post(
        f"/v1/admin/whatsapp-flows/{flow_id}/generate",
        headers=platform_headers,
        json={"description": "lo mismo pero con el boton mas corto"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["flow"]["flow_json"]["screens"][0]["layout"]["children"][0]["children"][5]["label"] == "Confirmar"
    assert len(httpx_mock.get_requests(url=FLOWS_URL)) == 1
