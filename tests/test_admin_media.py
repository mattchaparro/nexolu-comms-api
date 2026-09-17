"""Carga de multimedia del panel: la URL publica que viaja en los bloques
de imagen del builder. Guardado con nombre uuid (nada del cliente),
extension whitelisted y tope de peso; lo subido se sirve en /media/."""
from __future__ import annotations

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_upload_stores_and_serves_the_file(client, platform_headers):
    response = client.post(
        "/v1/admin/media",
        headers=platform_headers,
        files={"file": ("promo septiembre.png", PNG_BYTES, "image/png")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["url"].endswith(f"/media/{body['filename']}")
    assert body["filename"].endswith(".png")
    assert " " not in body["filename"]  # nombre uuid, no el del cliente

    served = client.get(f"/media/{body['filename']}")
    assert served.status_code == 200
    assert served.content == PNG_BYTES


def test_upload_rejects_disallowed_extensions_and_oversize(client, platform_headers):
    bad = client.post(
        "/v1/admin/media",
        headers=platform_headers,
        files={"file": ("script.exe", b"MZ", "application/octet-stream")},
    )
    assert bad.status_code == 422
    assert ".exe" in bad.json()["detail"] or "Extension" in bad.json()["detail"]

    huge = client.post(
        "/v1/admin/media",
        headers=platform_headers,
        files={"file": ("gigante.png", b"0" * (10 * 1024 * 1024 + 1), "image/png")},
    )
    assert huge.status_code == 413


def test_upload_requires_panel_auth(client):
    response = client.post(
        "/v1/admin/media",
        files={"file": ("x.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code in (401, 403)
