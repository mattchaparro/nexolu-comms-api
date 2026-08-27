from __future__ import annotations


def test_list_apps_requires_the_platform_key(client):
    assert client.get("/v1/admin/apps").status_code == 401


def test_admin_apps_disabled_without_a_configured_platform_key(client, monkeypatch):
    monkeypatch.delenv("NEXOLU_PLATFORM_API_KEY", raising=False)
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    response = client.get("/v1/admin/apps", headers={"Authorization": "Bearer whatever"})
    assert response.status_code == 503


def test_create_app_returns_the_api_key_once(client, platform_headers):
    response = client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa", "name": "Nexolu Spa"})

    assert response.status_code == 201
    body = response.json()
    assert body["api_key"].startswith("ncm_")
    assert body["has_meta_whatsapp"] is False
    assert body["has_brevo"] is False


def test_create_app_conflicts_on_duplicate_app_id(client, platform_headers):
    client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa"})

    response = client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa"})

    assert response.status_code == 409


def test_list_apps_masks_the_api_key(client, platform_headers):
    created = client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa"}).json()

    response = client.get("/v1/admin/apps", headers=platform_headers)

    assert response.status_code == 200
    listed = next(a for a in response.json() if a["app_id"] == "spa")
    assert listed["api_key_masked"] != created["api_key"]
    assert "..." in listed["api_key_masked"]
    assert "api_key" not in listed


def test_update_app_patches_only_provided_fields(client, platform_headers):
    client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa", "name": "Nexolu Spa"})

    response = client.patch("/v1/admin/apps/spa", headers=platform_headers, json={"is_active": False})

    assert response.status_code == 200
    body = response.json()
    assert body["is_active"] is False
    assert body["name"] == "Nexolu Spa"  # no se toco


def test_update_app_404_for_unknown_app(client, platform_headers):
    response = client.patch("/v1/admin/apps/no-existe", headers=platform_headers, json={"is_active": False})
    assert response.status_code == 404


def test_regenerate_key_returns_a_new_key_once_and_invalidates_the_old_one(client, platform_headers):
    created = client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa"}).json()

    response = client.post("/v1/admin/apps/spa/regenerate-key", headers=platform_headers)

    assert response.status_code == 200
    new_key = response.json()["api_key"]
    assert new_key != created["api_key"]

    # la key vieja ya no autentica llamadas de la app
    old_key_response = client.get(
        "/v1/usage/summary", headers={"Authorization": f"Bearer {created['api_key']}"}
    )
    assert old_key_response.status_code == 401

    # la key nueva si
    new_key_response = client.get("/v1/usage/summary", headers={"Authorization": f"Bearer {new_key}"})
    assert new_key_response.status_code == 200
