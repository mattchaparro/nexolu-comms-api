"""Publicar historias en Instagram.

Lo que se defiende:

  - Que una app sin Instagram configurado NO falle: devuelve `skipped`, como
    cualquier canal sin credenciales. Un negocio que no publica en Instagram
    no es un error.
  - Que sean DOS llamadas a Meta (contenedor + publicar), porque asi es la
    API y equivocarse ahi produce historias que se crean y nunca salen.
  - Que un rechazo de Meta llegue con su MOTIVO. Entre "la imagen no es
    JPEG" y "el token caduco" esta la diferencia entre corregir el medio y
    renovar la credencial.
"""
from __future__ import annotations

import httpx
import pytest


@pytest.fixture
def spa_app(client, platform_headers):
    return client.post("/v1/admin/apps", headers=platform_headers, json={"app_id": "spa"}).json()


@pytest.fixture
def app_headers(spa_app):
    return {"Authorization": f"Bearer {spa_app['api_key']}"}


def _configure(client, platform_headers) -> None:
    client.post(
        "/v1/admin/apps/spa/providers/meta-instagram",
        headers=platform_headers,
        json={"ig_user_id": "17841400000000000", "access_token": "ig-token", "username": "luxurynails"},
    )


# -- Credenciales -------------------------------------------------------------


def test_status_before_configuring(client, platform_headers, spa_app):
    response = client.get("/v1/admin/apps/spa/providers/meta-instagram", headers=platform_headers)

    assert response.status_code == 200
    assert response.json()["configured"] is False


def test_configure_then_reveal(client, platform_headers, spa_app):
    _configure(client, platform_headers)

    status_response = client.get("/v1/admin/apps/spa/providers/meta-instagram", headers=platform_headers)
    assert status_response.json() == {
        "configured": True,
        "ig_user_id": "17841400000000000",
        "username": "luxurynails",
    }
    # El token NUNCA sale en el status: se revela en su propio endpoint.
    assert "access_token" not in status_response.json()

    secrets = client.get("/v1/admin/apps/spa/providers/meta-instagram/secrets", headers=platform_headers)
    assert secrets.json()["access_token"] == "ig-token"


def test_instagram_credentials_are_independent_from_whatsapp(client, platform_headers, spa_app):
    """Rotar una no debe obligar a repegar la otra.

    Son permisos distintos y una caduca (Instagram, 60 dias) mientras la
    otra puede ser permanente. Guardarlas juntas convertiria cada rotacion
    de token de Instagram en un riesgo de tumbar los recordatorios.
    """
    _configure(client, platform_headers)
    client.post(
        "/v1/admin/apps/spa/providers/meta-whatsapp",
        headers=platform_headers,
        json={"phone_number_id": "111", "access_token": "wa-token"},
    )

    # Reconfigurar Instagram no toca WhatsApp.
    client.post(
        "/v1/admin/apps/spa/providers/meta-instagram",
        headers=platform_headers,
        json={"ig_user_id": "999", "access_token": "ig-token-2"},
    )

    wa = client.get("/v1/admin/apps/spa/providers/meta-whatsapp/secrets", headers=platform_headers)
    assert wa.json()["access_token"] == "wa-token"


# -- Publicar ------------------------------------------------------------------


def test_sin_instagram_configurado_no_es_un_error(client, app_headers, spa_app):
    response = client.post(
        "/v1/instagram/stories", headers=app_headers, json={"image_url": "https://x.test/a.jpg"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"


def test_hay_que_mandar_imagen_o_video_pero_no_los_dos(client, app_headers, spa_app):
    sin_nada = client.post("/v1/instagram/stories", headers=app_headers, json={})
    assert sin_nada.status_code == 422

    ambos = client.post(
        "/v1/instagram/stories",
        headers=app_headers,
        json={"image_url": "https://x.test/a.jpg", "video_url": "https://x.test/a.mp4"},
    )
    assert ambos.status_code == 422


def test_publicar_son_dos_llamadas_contenedor_y_publicar(
    client, platform_headers, app_headers, spa_app, monkeypatch
):
    _configure(client, platform_headers)

    llamadas: list[tuple[str, dict]] = []

    async def fake_post(self, url, params=None, headers=None, **kwargs):
        llamadas.append((url, dict(params or {})))
        cuerpo = {"id": "container-1"} if "media_publish" not in url else {"id": "media-1"}
        return httpx.Response(200, json=cuerpo, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    response = client.post(
        "/v1/instagram/stories", headers=app_headers, json={"image_url": "https://x.test/a.jpg"}
    )

    assert response.json() == {"status": "published", "media_id": "media-1", "error": None}
    assert len(llamadas) == 2

    (url_contenedor, params_contenedor), (url_publicar, params_publicar) = llamadas
    assert url_contenedor.endswith("/17841400000000000/media")
    assert params_contenedor["media_type"] == "STORIES"
    assert params_contenedor["image_url"] == "https://x.test/a.jpg"
    # El segundo paso usa el id que devolvio el primero, no el de la app.
    assert url_publicar.endswith("/17841400000000000/media_publish")
    assert params_publicar["creation_id"] == "container-1"


def test_un_rechazo_de_meta_llega_con_su_motivo(
    client, platform_headers, app_headers, spa_app, monkeypatch
):
    """Entre "la imagen no es JPEG" y "el token caduco" esta la diferencia
    entre corregir el medio y renovar la credencial."""
    _configure(client, platform_headers)

    async def fake_post(self, url, params=None, headers=None, **kwargs):
        return httpx.Response(
            400,
            json={"error": {"message": "The image is not a valid JPEG"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    response = client.post(
        "/v1/instagram/stories", headers=app_headers, json={"image_url": "https://x.test/a.png"}
    )

    assert response.json()["status"] == "failed"
    assert "JPEG" in response.json()["error"]


def test_si_falla_el_contenedor_no_se_intenta_publicar(
    client, platform_headers, app_headers, spa_app, monkeypatch
):
    # Publicar un contenedor que no existe produce un segundo error que
    # esconde el primero, que es el que explica que paso.
    _configure(client, platform_headers)

    llamadas: list[str] = []

    async def fake_post(self, url, params=None, headers=None, **kwargs):
        llamadas.append(url)
        return httpx.Response(400, json={"error": {"message": "nope"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client.post("/v1/instagram/stories", headers=app_headers, json={"image_url": "https://x.test/a.jpg"})

    assert len(llamadas) == 1


def test_las_menciones_viajan_como_user_tags(
    client, platform_headers, app_headers, spa_app, monkeypatch
):
    # Es lo unico que la API deja poner encima de una historia.
    _configure(client, platform_headers)

    capturado: dict = {}

    async def fake_post(self, url, params=None, headers=None, **kwargs):
        if "media_publish" not in url:
            capturado.update(params or {})
        return httpx.Response(200, json={"id": "x"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client.post(
        "/v1/instagram/stories",
        headers=app_headers,
        json={"image_url": "https://x.test/a.jpg", "mentions": ["@luxurynails"]},
    )

    # Sin la arroba: Meta espera el username pelado.
    assert capturado["user_tags"] == '[{"username": "luxurynails"}]'
