"""Catalogo y comercio (Fase 4): sync por items_batch con content_hash
(no re-enviar lo que no cambio - el rate limit es ~100 batches/hora),
verificacion del lote por handle, mensajes de producto (SPM/MPM/catalogo
completo, payloads oficiales H.2-H.4) y creacion/conexion del catalogo."""
from __future__ import annotations

import json

CATALOG_ID = "cat-1001"
BATCH_URL = f"https://graph.facebook.com/v21.0/{CATALOG_ID}/items_batch"
CHECK_URL_PREFIX = f"https://graph.facebook.com/v21.0/{CATALOG_ID}/check_batch_request_status"
MESSAGES_URL = "https://graph.facebook.com/v21.0/777000/messages"


def _setup_app(client, platform_headers) -> dict[str, str]:
    """App 'tienda' en BD con WABA y catalogo configurados. @return headers
    de auth de la app (su api_key)."""
    created = client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "tienda"})
    assert created.status_code == 201
    api_key = created.json()["api_key"]
    response = client.post(
        "/v1/admin/apps/tienda/providers/meta-whatsapp",
        headers=platform_headers,
        json={
            "phone_number_id": "777000",
            "access_token": "tienda-token",
            "waba_id": "waba-tienda",
            "catalog_id": CATALOG_ID,
            "meta_business_id": "biz-500",
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {api_key}"}


def _item(**overrides) -> dict:
    item = {
        "retailer_id": "granizado-mango-8oz",
        "title": "Granizado de Mango",
        "description": "8 oz, fruta natural",
        "price": "9000 COP",
        "availability": "in stock",
        "image_link": "https://img.nexolu.co/mango.jpg",
        "brand": "Estación Polar",
    }
    item.update(overrides)
    return item


def _sync(client, headers, **overrides):
    payload = {"items": [_item()], "deletes": []}
    payload.update(overrides)
    return client.post("/v1/catalog/sync", headers=headers, json=payload)


def test_sync_sends_the_official_items_batch_and_tracks_pending(client, platform_headers, httpx_mock):
    headers = _setup_app(client, platform_headers)
    httpx_mock.add_response(url=BATCH_URL, json={"handles": ["h-abc"]})

    response = _sync(client, headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"sent": 1, "skipped": 0, "deleted": 0, "handle": "h-abc", "immediate_errors": []}

    sent = json.loads(httpx_mock.get_requests(url=BATCH_URL)[0].content)
    assert sent["item_type"] == "PRODUCT_ITEM"
    assert sent["allow_upsert"] is True
    assert sent["requests"][0]["method"] == "UPDATE"
    assert sent["requests"][0]["data"] == {
        "id": "granizado-mango-8oz",
        "title": "Granizado de Mango",
        "price": "9000 COP",
        "availability": "in stock",
        "condition": "new",
        "description": "8 oz, fruta natural",
        "image_link": "https://img.nexolu.co/mango.jpg",
        "brand": "Estación Polar",
    }

    status = client.get("/v1/catalog/status", headers=headers).json()
    assert status["catalog_id"] == CATALOG_ID
    assert status["items"][0]["sync_status"] == "pending"


def test_unchanged_items_are_skipped_without_calling_meta(client, platform_headers, httpx_mock):
    headers = _setup_app(client, platform_headers)
    httpx_mock.add_response(url=BATCH_URL, json={"handles": ["h-1"]})
    _sync(client, headers)
    httpx_mock.add_response(
        url=f"{CHECK_URL_PREFIX}?handle=h-1", json={"data": [{"status": "finished", "errors": []}]}
    )
    assert client.post("/v1/catalog/check", headers=headers, json={}).json() == {
        "synced": 1, "errors": 0, "still_pending": 0,
    }

    # Mismo contenido: 0 llamadas nuevas a Meta.
    response = _sync(client, headers)

    assert response.json()["skipped"] == 1
    assert response.json()["sent"] == 0
    assert len(httpx_mock.get_requests(url=BATCH_URL)) == 1

    # Cambia el precio: se reenvia.
    httpx_mock.add_response(url=BATCH_URL, json={"handles": ["h-2"]})
    response = _sync(client, headers, items=[_item(price="10000 COP")])
    assert response.json()["sent"] == 1


def test_check_marks_item_errors_reported_by_meta(client, platform_headers, httpx_mock):
    headers = _setup_app(client, platform_headers)
    httpx_mock.add_response(url=BATCH_URL, json={"handles": ["h-err"]})
    _sync(client, headers)

    httpx_mock.add_response(
        url=f"{CHECK_URL_PREFIX}?handle=h-err",
        json={
            "data": [
                {
                    "status": "finished",
                    "errors": [
                        {"retailer_id": "granizado-mango-8oz", "message": "image_link no accesible"}
                    ],
                }
            ]
        },
    )
    result = client.post("/v1/catalog/check", headers=headers, json={}).json()

    assert result["errors"] == 1
    item = client.get("/v1/catalog/status", headers=headers).json()["items"][0]
    assert item["sync_status"] == "error"
    assert "image_link" in item["last_error"]


def test_deletes_go_as_delete_method_and_drop_the_row(client, platform_headers, httpx_mock):
    headers = _setup_app(client, platform_headers)
    httpx_mock.add_response(url=BATCH_URL, json={"handles": ["h-1"]})
    _sync(client, headers)

    httpx_mock.add_response(url=BATCH_URL, json={"handles": ["h-2"]})
    response = _sync(client, headers, items=[], deletes=["granizado-mango-8oz"])

    assert response.json()["deleted"] == 1
    sent = json.loads(httpx_mock.get_requests(url=BATCH_URL)[1].content)
    assert sent["requests"] == [{"method": "DELETE", "data": {"id": "granizado-mango-8oz"}}]
    assert client.get("/v1/catalog/status", headers=headers).json()["items"] == []


def test_an_app_without_catalog_gets_a_clear_422(client, auth_headers):
    response = client.post(
        "/v1/catalog/sync", headers=auth_headers, json={"items": [_item()], "deletes": []}
    )

    assert response.status_code == 422
    assert "catalog_id" in response.json()["detail"]


# --- mensajes de producto (payloads oficiales H.2-H.4) ------------------------


def _send(client, headers, extra: dict):
    return client.post(
        "/v1/notifications/send",
        headers=headers,
        json={
            "channels": ["whatsapp"],
            "to": {"whatsapp": "+573001234567"},
            "category": "marketing",
            **extra,
        },
    )


def test_single_product_message_uses_the_connected_catalog(client, platform_headers, httpx_mock):
    headers = _setup_app(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.spm"}]})

    response = _send(
        client,
        headers,
        {"text": "Mira esto:", "whatsapp_product": {"product_retailer_id": "granizado-mango-8oz"}},
    )

    assert response.json()["results"][0]["status"] == "sent"
    sent = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)
    assert sent["type"] == "interactive"
    assert sent["interactive"]["type"] == "product"
    assert sent["interactive"]["action"] == {
        "catalog_id": CATALOG_ID,
        "product_retailer_id": "granizado-mango-8oz",
    }
    assert sent["interactive"]["body"] == {"text": "Mira esto:"}


def test_multi_product_message_builds_sections(client, platform_headers, httpx_mock):
    headers = _setup_app(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.mpm"}]})

    response = _send(
        client,
        headers,
        {
            "text": "Nuestros granizados:",
            "whatsapp_products": {
                "header": "Estación Polar",
                "sections": [
                    {"title": "Clásicos", "product_retailer_ids": ["mango", "fresa"]},
                    {"title": "Especiales", "product_retailer_ids": ["lulo"]},
                ],
            },
        },
    )

    assert response.json()["results"][0]["status"] == "sent"
    interactive = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)["interactive"]
    assert interactive["type"] == "product_list"
    assert interactive["header"] == {"type": "text", "text": "Estación Polar"}
    assert interactive["action"]["catalog_id"] == CATALOG_ID
    assert interactive["action"]["sections"][0]["product_items"] == [
        {"product_retailer_id": "mango"},
        {"product_retailer_id": "fresa"},
    ]


def test_catalog_message_sends_the_whole_catalog(client, platform_headers, httpx_mock):
    headers = _setup_app(client, platform_headers)
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.cat"}]})

    response = _send(
        client,
        headers,
        {
            "text": "Explora todo nuestro menú",
            "whatsapp_catalog": {"thumbnail_product_retailer_id": "mango"},
        },
    )

    assert response.json()["results"][0]["status"] == "sent"
    interactive = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[0].content)["interactive"]
    assert interactive["type"] == "catalog_message"
    assert interactive["action"] == {
        "name": "catalog_message",
        "parameters": {"thumbnail_product_retailer_id": "mango"},
    }


# --- crear / conectar catalogo -------------------------------------------------


def test_setup_creates_connects_and_stores_the_catalog(client, platform_headers, httpx_mock):
    client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "tienda"})
    client.post(
        "/v1/admin/apps/tienda/providers/meta-whatsapp",
        headers=platform_headers,
        json={
            "phone_number_id": "777000",
            "access_token": "tienda-token",
            "waba_id": "waba-tienda",
            "meta_business_id": "biz-500",
        },
    )
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/biz-500/owned_product_catalogs",
        json={"id": "cat-nuevo"},
    )
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/waba-tienda/product_catalogs",
        json={"success": True},
    )

    response = client.post(
        "/v1/admin/catalogs",
        headers=platform_headers,
        json={"app_id": "tienda", "name": "Catálogo Tienda"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"catalog_id": "cat-nuevo", "connected_to_waba": True}

    created = json.loads(
        httpx_mock.get_requests(url="https://graph.facebook.com/v21.0/biz-500/owned_product_catalogs")[0].content
    )
    assert created == {"name": "Catálogo Tienda", "vertical": "commerce"}

    status = client.get(
        "/v1/admin/apps/tienda/providers/meta-whatsapp", headers=platform_headers
    ).json()
    assert status["catalog_id"] == "cat-nuevo"


def test_connecting_an_existing_catalog_skips_creation(client, platform_headers, httpx_mock):
    client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "tienda"})
    client.post(
        "/v1/admin/apps/tienda/providers/meta-whatsapp",
        headers=platform_headers,
        json={"phone_number_id": "777000", "access_token": "t", "waba_id": "waba-tienda"},
    )
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/waba-tienda/product_catalogs",
        json={"success": True},
    )

    response = client.post(
        "/v1/admin/catalogs",
        headers=platform_headers,
        json={"app_id": "tienda", "catalog_id": "cat-existente"},
    )

    assert response.status_code == 200
    assert response.json()["catalog_id"] == "cat-existente"


def test_setup_without_name_or_catalog_fails_clearly(client, platform_headers):
    client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "tienda"})
    client.post(
        "/v1/admin/apps/tienda/providers/meta-whatsapp",
        headers=platform_headers,
        json={"phone_number_id": "777000", "access_token": "t", "waba_id": "waba-tienda"},
    )

    response = client.post(
        "/v1/admin/catalogs", headers=platform_headers, json={"app_id": "tienda"}
    )

    assert response.status_code == 422
    assert "name" in response.json()["detail"]
