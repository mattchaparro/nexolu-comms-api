"""Audios, imagenes y notas de voz en el chat.

El chat mostraba solo la palabra «audio»: Meta manda un id, no el archivo,
y nadie lo descargaba. Y no habia como grabar: solo imagen o PDF.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from tests.test_contact_name_sync import PLATFORM, _contact_id
from tests.test_embed_chat import CALLBACK_URL, _inbound

GRAPH = "https://graph.facebook.com/v21.0"


def _audio_entrante(client, httpx_mock, phone: str) -> None:
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    body = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "123456"},
        "contacts": [{"profile": {"name": "Clienta"}, "wa_id": phone}],
        "messages": [{
            "from": phone, "id": "wamid.audio1", "type": "audio",
            "audio": {"id": "MEDIA123", "mime_type": "audio/ogg; codecs=opus", "voice": True},
        }],
    }}]}]}
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(b"meta-app-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/whatsapp/pos",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )
    assert response.status_code == 200


def test_el_audio_de_la_clienta_se_puede_escuchar(client, auth_headers, httpx_mock, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    _audio_entrante(client, httpx_mock, "573001112233")
    contact_id = _contact_id(client)
    thread = client.get(f"/v1/admin/chats/{contact_id}/messages", headers=PLATFORM).json()
    audio = next(m for m in thread if m["message_type"] == "audio")

    # Meta: el id da una URL temporal, y la URL da los bytes.
    httpx_mock.add_response(
        url=f"{GRAPH}/MEDIA123",
        json={"url": "https://lookaside.fbsbx.com/whatsapp/abc", "mime_type": "audio/ogg; codecs=opus"},
    )
    httpx_mock.add_response(url="https://lookaside.fbsbx.com/whatsapp/abc", content=b"OggS-audio")

    response = client.get(f"/v1/admin/chats/{contact_id}/messages/{audio['id']}/media", headers=PLATFORM)

    assert response.status_code == 200
    assert response.content == b"OggS-audio"
    assert response.headers["content-type"].startswith("audio/ogg")

    # La segunda vez sale del disco: Meta lo borra a los 30 dias.
    otra = client.get(f"/v1/admin/chats/{contact_id}/messages/{audio['id']}/media", headers=PLATFORM)
    assert otra.content == b"OggS-audio"
    assert len(httpx_mock.get_requests(url=f"{GRAPH}/MEDIA123")) == 1
    get_settings.cache_clear()


def test_un_texto_no_trae_archivo(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    contact_id = _contact_id(client)
    texto = client.get(f"/v1/admin/chats/{contact_id}/messages", headers=PLATFORM).json()[-1]

    response = client.get(f"/v1/admin/chats/{contact_id}/messages/{texto['id']}/media", headers=PLATFORM)

    assert response.status_code == 404


def test_la_nota_de_voz_grabada_se_convierte_a_ogg(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    async def convertir(content: bytes, suffix: str) -> bytes:
        assert suffix == ".webm"
        return b"OggS-convertido"

    monkeypatch.setattr("nexolu_comms_api.core.media_inbound.to_whatsapp_voice", convertir)

    response = client.post(
        "/v1/admin/media/voice",
        files={"file": ("nota.webm", b"webm-bytes", "audio/webm")},
        headers=PLATFORM,
    )

    assert response.status_code == 201, response.text
    assert response.json()["url"].endswith(".ogg")
    assert (tmp_path / response.json()["filename"]).read_bytes() == b"OggS-convertido"
    get_settings.cache_clear()


def test_la_nota_de_voz_se_sirve_como_audio_ogg(client, tmp_path):
    """Meta rechazaba la nota de voz porque el link respondia
    application/octet-stream: la imagen slim no sabe que es un .ogg."""
    import mimetypes

    from nexolu_comms_api.config import get_settings

    assert mimetypes.guess_type("nota.ogg")[0] == "audio/ogg"

    media_dir = __import__("pathlib").Path(get_settings().media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "prueba-tipo.ogg").write_bytes(b"OggS")
    try:
        response = client.get("/media/prueba-tipo.ogg")
        assert response.headers["content-type"].startswith("audio/ogg")
    finally:
        (media_dir / "prueba-tipo.ogg").unlink()
