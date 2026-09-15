from __future__ import annotations

import pytest


@pytest.fixture
def spa_app(client, platform_headers):
    return client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa"}).json()


def test_meta_whatsapp_status_before_configuring(client, platform_headers, spa_app):
    response = client.get("/v1/admin/apps/spa/providers/meta-whatsapp", headers=platform_headers)

    assert response.status_code == 200
    assert response.json() == {"configured": False, "phone_number_id": None, "waba_id": None, "callback_url": None, "enforce_meta_signature": False, "catalog_id": None, "meta_business_id": None}


def test_meta_whatsapp_secrets_404_before_configuring(client, platform_headers, spa_app):
    response = client.get("/v1/admin/apps/spa/providers/meta-whatsapp/secrets", headers=platform_headers)
    assert response.status_code == 404


def test_configure_then_reveal_meta_whatsapp(client, platform_headers, spa_app):
    payload = {
        "phone_number_id": "111",
        "access_token": "wa-token-1",
        "waba_id": "waba-1",
        "webhook_verify_token": "verify-1",
        "meta_app_secret": "app-secret-1",
        "callback_secret": "callback-secret-1",
        "callback_url": "https://spa.nexolu.test/webhooks/whatsapp",
    }

    configure = client.post("/v1/admin/apps/spa/providers/meta-whatsapp", headers=platform_headers, json=payload)
    assert configure.status_code == 201
    assert configure.json()["configured"] is True
    assert configure.json()["phone_number_id"] == "111"
    assert "access_token" not in configure.json()

    status_response = client.get("/v1/admin/apps/spa/providers/meta-whatsapp", headers=platform_headers)
    assert status_response.json()["configured"] is True
    assert "access_token" not in status_response.json()

    secrets = client.get("/v1/admin/apps/spa/providers/meta-whatsapp/secrets", headers=platform_headers)
    assert secrets.status_code == 200
    assert secrets.json()["access_token"] == "wa-token-1"
    assert secrets.json()["meta_app_secret"] == "app-secret-1"


def test_reconfigure_meta_whatsapp_overwrites_the_previous_token(client, platform_headers, spa_app):
    base_payload = {"phone_number_id": "111", "access_token": "wa-token-1"}
    client.post("/v1/admin/apps/spa/providers/meta-whatsapp", headers=platform_headers, json=base_payload)

    rotated_payload = {"phone_number_id": "111", "access_token": "wa-token-2"}
    response = client.post(
        "/v1/admin/apps/spa/providers/meta-whatsapp", headers=platform_headers, json=rotated_payload
    )
    assert response.status_code == 201

    secrets = client.get("/v1/admin/apps/spa/providers/meta-whatsapp/secrets", headers=platform_headers)
    assert secrets.json()["access_token"] == "wa-token-2"

    apps = client.get("/v1/admin/apps", headers=platform_headers).json()
    spa = next(a for a in apps if a["app_id"] == "spa")
    assert spa["has_meta_whatsapp"] is True


def test_configure_then_reveal_brevo(client, platform_headers, spa_app):
    payload = {"from_email": "no-reply@spa.nexolu.co", "from_name": "Nexolu Spa", "brevo_api_key": "brevo-key-1"}

    configure = client.post("/v1/admin/apps/spa/providers/brevo", headers=platform_headers, json=payload)
    assert configure.status_code == 201
    assert configure.json()["configured"] is True
    assert "brevo_api_key" not in configure.json()

    secrets = client.get("/v1/admin/apps/spa/providers/brevo/secrets", headers=platform_headers)
    assert secrets.status_code == 200
    assert secrets.json()["brevo_api_key"] == "brevo-key-1"


def test_providers_require_the_platform_key(client, spa_app):
    assert client.get("/v1/admin/apps/spa/providers/meta-whatsapp").status_code == 401
    assert client.get("/v1/admin/apps/spa/providers/brevo").status_code == 401


def test_configure_provider_404_for_unknown_app(client, platform_headers):
    response = client.post(
        "/v1/admin/apps/no-existe/providers/meta-whatsapp",
        headers=platform_headers,
        json={"phone_number_id": "1", "access_token": "t"},
    )
    assert response.status_code == 404
