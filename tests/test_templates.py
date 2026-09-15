"""Espejo de plantillas (Fase 3 del plan): crear/sincronizar contra Meta,
estado al dia por webhook `message_template_status_update`, validacion en
el envio (una plantilla no aprobada se corta ANTES de llamar a Meta) y
scoping - un cliente externo solo ve las plantillas de sus apps."""
from __future__ import annotations

import hashlib
import hmac
import json

TEMPLATES_URL = "https://graph.facebook.com/v21.0/waba-pos/message_templates"
MESSAGES_URL = "https://graph.facebook.com/v21.0/123456/messages"


def _create(client, platform_headers, **overrides):
    payload = {
        "app_id": "pos",
        "name": "recordatorio_cita",
        "language": "es",
        "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Hola {{1}}, tu cita es el {{2}}."}],
    }
    payload.update(overrides)
    return client.post("/v1/admin/templates", headers=platform_headers, json=payload)


def _mock_create_ok(httpx_mock, template_id="tpl-1", status="PENDING"):
    httpx_mock.add_response(
        url=TEMPLATES_URL, json={"id": template_id, "status": status, "category": "UTILITY"}
    )


# El conftest registra la app 'pos' con waba_id=None - estos tests necesitan
# la WABA configurada, asi que la fijan via el admin de credenciales.
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
        },
    )
    assert response.status_code == 201


def test_creating_a_template_registers_it_in_meta_and_mirrors_it(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_create_ok(httpx_mock)

    response = _create(client, platform_headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["meta_template_id"] == "tpl-1"
    assert body["waba_id"] == "waba-pos"

    sent = json.loads(httpx_mock.get_requests(url=TEMPLATES_URL)[0].content)
    assert sent == {
        "name": "recordatorio_cita",
        "language": "es",
        "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Hola {{1}}, tu cita es el {{2}}."}],
    }


def test_an_app_without_waba_gets_a_clear_422(client, platform_headers):
    # 'pos' resuelve por el fallback de env (sin waba_id configurado en BD).
    response = _create(client, platform_headers)

    assert response.status_code == 422
    assert "waba_id" in response.json()["detail"]


def test_a_duplicate_identity_is_409_without_calling_meta(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_create_ok(httpx_mock)
    assert _create(client, platform_headers).status_code == 201

    response = _create(client, platform_headers)

    assert response.status_code == 409
    assert len(httpx_mock.get_requests(url=TEMPLATES_URL)) == 1


def test_a_meta_rejection_surfaces_as_502_and_stores_nothing(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    httpx_mock.add_response(
        url=TEMPLATES_URL, status_code=400, json={"error": {"message": "nombre invalido"}}
    )

    response = _create(client, platform_headers)

    assert response.status_code == 502
    listed = client.get("/v1/admin/templates", headers=platform_headers).json()
    assert listed["items"] == []


def test_sync_upserts_everything_meta_reports(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    httpx_mock.add_response(
        url=f"{TEMPLATES_URL}?fields=id%2Cname%2Clanguage%2Cstatus%2Ccategory%2Ccomponents%2Cquality_score&limit=200",
        json={
            "data": [
                {
                    "id": "tpl-9",
                    "name": "creada_por_fuera",
                    "language": "es",
                    "status": "APPROVED",
                    "category": "MARKETING",
                    "components": [{"type": "BODY", "text": "Promo"}],
                    "quality_score": {"score": "GREEN"},
                }
            ]
        },
    )

    response = client.post(
        "/v1/admin/templates/sync", headers=platform_headers, json={"app_id": "pos"}
    )

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["name"] == "creada_por_fuera"
    assert item["status"] == "APPROVED"
    assert item["quality_score"] == "GREEN"


def _template_webhook_body(event="APPROVED", name="recordatorio_cita", template_id="tpl-1"):
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {
                                "event": event,
                                "message_template_id": template_id,
                                "message_template_name": name,
                                "message_template_language": "es",
                                "reason": "NONE" if event == "APPROVED" else "INVALID_FORMAT",
                            },
                        }
                    ]
                }
            ]
        }
    ).encode()


def test_the_status_webhook_updates_the_mirror(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_create_ok(httpx_mock)
    assert _create(client, platform_headers).status_code == 201

    body = _template_webhook_body(event="APPROVED")
    signature = "sha256=" + hmac.new(b"meta-app-secret", body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/whatsapp/pos",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )

    assert response.status_code == 200
    listed = client.get("/v1/admin/templates", headers=platform_headers).json()["items"]
    assert listed[0]["status"] == "APPROVED"


def test_a_rejection_webhook_stores_the_reason(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_create_ok(httpx_mock)
    _create(client, platform_headers)

    body = _template_webhook_body(event="REJECTED")
    signature = "sha256=" + hmac.new(b"meta-app-secret", body, hashlib.sha256).hexdigest()
    client.post(
        "/webhooks/whatsapp/pos",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )

    item = client.get("/v1/admin/templates", headers=platform_headers).json()["items"][0]
    assert item["status"] == "REJECTED"
    assert item["reason"] == "INVALID_FORMAT"


def test_sending_a_non_approved_template_is_cut_before_meta(client, platform_headers, auth_headers, httpx_mock):
    """El corte usa el fallback (app, name, language): el envio del POS sale
    por la credencial de env (sin waba en la identidad), pero la fila del
    espejo es de la misma app - alcanza para avisar."""
    _configure_pos_waba(client, platform_headers)
    _mock_create_ok(httpx_mock, status="PAUSED")
    _create(client, platform_headers, name="promo_pausada", category="MARKETING")

    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": "42",
            "channels": ["whatsapp"],
            "to": {"whatsapp": "+573001234567"},
            "category": "marketing",
            "whatsapp_template": {"name": "promo_pausada", "language": "es"},
        },
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "failed"
    assert "PAUSED" in result["error"]
    assert httpx_mock.get_requests(url=MESSAGES_URL) == []


def test_an_unknown_template_still_sends(client, auth_headers, httpx_mock):
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.ok"}]})

    response = client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "channels": ["whatsapp"],
            "to": {"whatsapp": "+573001234567"},
            "category": "utility",
            "whatsapp_template": {"name": "no_esta_en_el_espejo", "language": "es"},
        },
    )

    assert response.json()["results"][0]["status"] == "sent"


def test_deleting_a_template_removes_it_from_meta_and_all_its_languages(client, platform_headers, httpx_mock):
    _configure_pos_waba(client, platform_headers)
    _mock_create_ok(httpx_mock, template_id="tpl-es")
    _mock_create_ok(httpx_mock, template_id="tpl-en")
    _create(client, platform_headers)
    _create(client, platform_headers, language="en")
    template_id = client.get("/v1/admin/templates", headers=platform_headers).json()["items"][0]["id"]

    httpx_mock.add_response(
        url=f"{TEMPLATES_URL}?name=recordatorio_cita", method="DELETE", json={"success": True}
    )
    response = client.delete(f"/v1/admin/templates/{template_id}", headers=platform_headers)

    assert response.status_code == 204
    # Meta borra el nombre en TODOS los idiomas: el espejo tambien.
    assert client.get("/v1/admin/templates", headers=platform_headers).json()["items"] == []
