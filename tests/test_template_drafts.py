"""Redactar plantillas con IA: lo que vuelve tiene que poder pasar la
revision de Meta, y si la primera propuesta no cumple, se corrige sola una
vez. Nada se crea en Meta desde aca."""
from __future__ import annotations

import json

import pytest

from nexolu_comms_api.core.templates.drafter import TemplateDraft, to_components, validate_draft

PLATFORM = {"Authorization": "Bearer platform-key"}
IA_URL = "http://ia-core.test/v1/completions"

BUENA = {
    "name": "abrir_conversacion",
    "category": "UTILITY",
    "body": "Hola {{1}}, te escribimos de Luxury Nails. Tenemos un mensaje sobre tu atención con nosotras. ¿Nos regalas un minuto?",
    "footer": "",
    "buttons": ["Sí, cuéntame", "Ahora no"],
    "example_params": ["María"],
    "notes": "Utility: no promociona nada.",
}


@pytest.fixture
def ia_core(monkeypatch):
    from nexolu_comms_api.config import get_settings

    monkeypatch.setenv("IA_CORE_BASE_URL", "http://ia-core.test")
    monkeypatch.setenv("IA_CORE_API_KEY", "llave-connect")
    get_settings.cache_clear()


def test_la_generica_de_hola_y_variable_no_pasa():
    generica = TemplateDraft(name="generica", category="UTILITY", body="Hola {{1}}, {{2}}", example_params=["a", "b"])

    issues = validate_draft(generica)

    assert any("terminar con una variable" in i for i in issues)
    assert any("genérica" in i for i in issues)


def test_botones_con_emoji_o_largos_no_pasan():
    draft = TemplateDraft(**{**BUENA, "buttons": ["Sí 💅", "Quiero que me cuenten todo ya mismo"]})

    issues = validate_draft(draft)

    assert any("emoji" in i for i in issues)
    assert any("25 caracteres" in i for i in issues)


def test_la_buena_pasa_y_sale_con_ejemplo_y_botones():
    draft = TemplateDraft(**BUENA)

    assert validate_draft(draft) == []
    body, buttons = to_components(draft)
    assert body["example"] == {"body_text": [["María"]]}
    assert [b["text"] for b in buttons["buttons"]] == ["Sí, cuéntame", "Ahora no"]


def test_si_la_primera_no_cumple_se_corrige_una_vez(client, ia_core, httpx_mock):
    mala = {**BUENA, "body": "{{1}} {{2}}", "example_params": ["a", "b"]}
    httpx_mock.add_response(url=IA_URL, json={"text": json.dumps(mala)})
    httpx_mock.add_response(url=IA_URL, json={"text": "```json\n" + json.dumps(BUENA) + "\n```"})

    response = client.post(
        "/v1/admin/templates/draft",
        json={"app_id": "pos", "description": "abrir una conversación sin vender nada", "business_name": "Luxury Nails"},
        headers=PLATFORM,
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["issues"] == []
    assert data["name"] == "abrir_conversacion"
    assert data["components"][0]["example"] == {"body_text": [["María"]]}
    primera, segunda = (json.loads(r.content) for r in httpx_mock.get_requests(url=IA_URL))
    # Firma con el negocio que dijo quien redacta, no con el nombre de la app.
    assert "Negocio: Luxury Nails" in primera["user"]
    assert "Corrige esto" in segunda["user"]


def test_sin_ia_core_configurado_lo_dice(client):
    response = client.post(
        "/v1/admin/templates/draft",
        json={"app_id": "pos", "description": "aviso de festivo"},
        headers=PLATFORM,
    )

    assert response.status_code == 503
