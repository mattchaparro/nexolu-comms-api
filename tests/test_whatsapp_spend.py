"""El gasto de WhatsApp sale de Meta (pricing_analytics), no de estimados."""
from __future__ import annotations

import re

import pytest


@pytest.fixture
def pos_with_waba(monkeypatch):
    import json

    apps = json.loads(__import__("os").environ["NEXOLU_APPS_JSON"])
    apps["pos"]["whatsapp"]["waba_id"] = "WABA1"
    monkeypatch.setenv("NEXOLU_APPS_JSON", json.dumps(apps))
    monkeypatch.setenv("USD_COP_RATE", "4000")
    import nexolu_comms_api.core.auth.apps as apps_module
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()
    apps_module._legacy = None
    import nexolu_comms_api.core.spend as spend

    spend._cache.clear()


def test_el_gasto_del_mes_viene_de_meta_en_pesos_y_dolares(client, auth_headers, httpx_mock, pos_with_waba):
    httpx_mock.add_response(
        url=re.compile(r"https://graph\.facebook\.com/v21\.0/WABA1\?.*"),
        json={
            "currency": "COP",
            "pricing_analytics": {"data": [{"data_points": [
                {"start": 1790380800, "pricing_category": "MARKETING", "pricing_type": "REGULAR", "volume": 100, "cost": 4600.0},
                {"start": 1790467200, "pricing_category": "MARKETING", "pricing_type": "REGULAR", "volume": 64, "cost": 2947.72},
                {"start": 1790467200, "pricing_category": "UTILITY", "pricing_type": "REGULAR", "volume": 10, "cost": 29.455},
                {"start": 1790467200, "pricing_category": "SERVICE", "pricing_type": "FREE_CUSTOMER_SERVICE", "volume": 153, "cost": 0},
            ]}]},
        },
    )

    body = client.get("/v1/usage/whatsapp-spend", params={"month": "2026-09"}, headers=auth_headers).json()

    assert body["currency"] == "COP"
    assert body["total"] == pytest.approx(7577.18, abs=0.02)
    assert body["total_usd"] == pytest.approx(1.89, abs=0.01)
    assert body["by_category"][0] == {"category": "MARKETING", "type": "REGULAR", "volume": 164, "cost": 7547.72}
    assert {d["date"] for d in body["daily"]} == {"2026-09-26", "2026-09-27"}


def test_si_meta_falla_se_dice(client, auth_headers, httpx_mock, pos_with_waba):
    httpx_mock.add_response(url=re.compile(r"https://graph\.facebook\.com/.*"), json={"error": {"message": "sin permiso"}})
    response = client.get("/v1/usage/whatsapp-spend", headers=auth_headers)
    assert response.status_code == 502
    assert "sin permiso" in response.json()["detail"]


def test_sin_llave_no_se_ve(client):
    assert client.get("/v1/usage/whatsapp-spend").status_code == 401
