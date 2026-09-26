"""Difusiones desde Connect: la app publica lo que sabe de sus clientes y el
panel programa una plantilla para un publico.

Lo que no se puede romper:
- una plantilla de MARKETING solo le llega a quien acepta promociones;
- la lista se congela al enviar: correr el envio dos veces no le escribe
  dos veces a nadie;
- cancelar antes de la hora la detiene.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from tests.test_admin_chats import MESSAGES_URL

PLATFORM = {"Authorization": "Bearer platform-key"}


@pytest.fixture(autouse=True)
def _sin_pausa(monkeypatch):
    import nexolu_comms_api.core.broadcasts as broadcasts

    monkeypatch.setattr(broadcasts, "SEND_PAUSE_SECONDS", 0)


def _plantilla(category="MARKETING", status="APPROVED", name="promo_octubre"):
    from nexolu_comms_api.core.db.entities import WhatsAppTemplate
    from nexolu_comms_api.core.db.session import get_sessionmaker

    async def seed():
        async with get_sessionmaker()() as session:
            session.add(
                WhatsAppTemplate(
                    app_id="pos", waba_id="", name=name, language="es",
                    category=category, status=status,
                    components=[{"type": "BODY", "text": "Hola {{1}}, te extrañamos."}],
                )
            )
            await session.commit()

    asyncio.run(seed())


def _sync(client, auth_headers, contacts, business_id="7"):
    response = client.put(
        "/v1/contacts/bulk", json={"business_id": business_id, "contacts": contacts}, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _clientas(client, auth_headers):
    return _sync(
        client,
        auth_headers,
        [
            {"phone": "573001110001", "name": "Ana María", "fields": {"acepta_promociones": True, "ultima_visita": "2026-08-01", "visitas": 5}},
            {"phone": "573001110002", "name": ".", "fields": {"acepta_promociones": True, "ultima_visita": "2026-02-10", "visitas": 1}},
            {"phone": "573001110003", "name": "Luisa", "fields": {"acepta_promociones": False, "ultima_visita": "2026-08-20", "visitas": 9}},
        ],
    )


def _difusion(client, **overrides):
    body = {
        "app_id": "pos",
        "business_id": "7",
        "name": "Te extrañamos",
        "template_name": "promo_octubre",
        "template_params": ["{nombre}"],
        "audience": {},
        **overrides,
    }
    return client.post("/v1/admin/broadcasts", json=body, headers=PLATFORM)


def _dispatch_due():
    from nexolu_comms_api.core.broadcasts import dispatch_due

    return asyncio.run(dispatch_due(datetime.utcnow()))


def test_la_app_publica_sus_clientas_y_se_mezclan_los_campos(client, auth_headers):
    assert _clientas(client, auth_headers) == {"created": 3, "updated": 0}

    again = _sync(client, auth_headers, [{"phone": "+57 300 111 0001", "fields": {"visitas": 6, "ultima_visita": None}}])

    assert again == {"created": 0, "updated": 1}
    # No aparecen en la bandeja: nadie ha escrito todavia.
    assert client.get("/v1/admin/chats", headers=PLATFORM).json()["items"] == []


def test_marketing_solo_a_quien_acepta_promociones(client, auth_headers):
    _clientas(client, auth_headers)
    _plantilla("MARKETING")

    preview = client.post(
        "/v1/admin/broadcasts/preview",
        json={"app_id": "pos", "business_id": "7", "name": "x", "template_name": "promo_octubre"},
        headers=PLATFORM,
    ).json()

    assert preview["count"] == 2
    assert {c["phone"] for c in preview["sample"]} == {"573001110001", "573001110002"}


def test_utility_filtra_por_ultima_visita(client, auth_headers):
    _clientas(client, auth_headers)
    _plantilla("UTILITY", name="aviso")

    preview = client.post(
        "/v1/admin/broadcasts/preview",
        json={"app_id": "pos", "business_id": "7", "name": "x", "template_name": "aviso",
              "audience": {"last_visit_from": "2026-06-01"}},
        headers=PLATFORM,
    ).json()

    # Luisa no acepta promociones, pero esta no es de marketing.
    assert preview["count"] == 2
    assert {c["phone"] for c in preview["sample"]} == {"573001110001", "573001110003"}


def test_no_se_programa_una_plantilla_sin_aprobar(client, auth_headers):
    _plantilla(status="PENDING")

    response = _difusion(client, scheduled_at=(datetime.utcnow() + timedelta(hours=1)).isoformat())

    assert response.status_code == 422
    # Como borrador si se guarda.
    assert _difusion(client).json()["status"] == "draft"


def test_sale_a_la_hora_con_el_nombre_y_una_sola_vez(client, auth_headers, httpx_mock):
    _clientas(client, auth_headers)
    _plantilla("MARKETING")
    created = _difusion(client, scheduled_at=(datetime.utcnow() - timedelta(minutes=1)).isoformat()).json()
    assert created["status"] == "scheduled"

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.a"}]})
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.b"}]})
    assert _dispatch_due() == 2
    assert _dispatch_due() == 0  # ya salio: la segunda vuelta no hace nada

    sent = [json.loads(r.content) for r in httpx_mock.get_requests(url=MESSAGES_URL)]
    assert len(sent) == 2
    names = sorted(m["template"]["components"][0]["parameters"][0]["text"] for m in sent)
    # "." no sirve para saludar: va vacio y no "Hola ., ...".
    assert names == ["-", "Ana"]

    report = client.get(f"/v1/admin/broadcasts/{created['id']}/report", headers=PLATFORM).json()
    assert report["broadcast"]["status"] == "sent"
    assert report["broadcast"]["recipients"] == 2
    assert report["counts"]["sent"] == 2


def test_cancelar_antes_de_la_hora_la_detiene(client, auth_headers):
    _clientas(client, auth_headers)
    _plantilla("MARKETING")
    created = _difusion(client, scheduled_at=(datetime.utcnow() - timedelta(minutes=1)).isoformat()).json()

    cancelled = client.post(f"/v1/admin/broadcasts/{created['id']}/cancel", headers=PLATFORM)

    assert cancelled.json()["status"] == "cancelled"
    assert _dispatch_due() == 0
    assert client.post(f"/v1/admin/broadcasts/{created['id']}/cancel", headers=PLATFORM).status_code == 409


def test_otra_app_no_ve_las_difusiones(client, auth_headers):
    _plantilla()
    _difusion(client)

    assert client.get("/v1/admin/broadcasts", headers=auth_headers).status_code in (401, 403, 404)
