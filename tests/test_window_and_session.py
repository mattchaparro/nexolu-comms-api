"""La ventana de 24 h es con UN numero, y la sesion del panel no se cae.

Luxury cambia del 304 al 301: quien le escribio al 304 tiene la ventana
abierta con el 304, y un texto desde el 301 no le llega. La bandeja no
puede decir "puedes escribir" en ese caso.

Y Connect es el WhatsApp del negocio: una sesion que vence cada dia deja
a la recepcionista frente a un login que no sabe usar.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import update

from tests.test_embed_chat import _inbound

PLATFORM = {"Authorization": "Bearer platform-key"}


def _conversation(client):
    return client.get("/v1/admin/chats", headers=PLATFORM).json()["items"][0]


def _set_inbound_number(number: str | None) -> None:
    from nexolu_comms_api.core.db.entities import Contact
    from nexolu_comms_api.core.db.session import get_sessionmaker

    async def run():
        async with get_sessionmaker()() as session:
            await session.execute(update(Contact).values(last_inbound_phone_number_id=number))
            await session.commit()

    asyncio.run(run())


def test_quien_escribio_al_numero_que_envia_tiene_la_ventana_abierta(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")

    # El webhook de prueba entra por el numero de la app (123456).
    conversation = _conversation(client)
    assert conversation["window_open"] is True
    card = client.get(f"/v1/admin/chats/{conversation['contact_id']}", headers=PLATFORM).json()
    assert card["window_open"] is True


def test_quien_escribio_a_otro_numero_del_negocio_no_tiene_ventana(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    # Le escribio al numero VIEJO; el negocio hoy envia desde 123456.
    _set_inbound_number("999-numero-viejo")

    conversation = _conversation(client)
    assert conversation["window_open"] is False
    card = client.get(f"/v1/admin/chats/{conversation['contact_id']}", headers=PLATFORM).json()
    assert card["window_open"] is False


def test_filas_viejas_sin_numero_se_juzgan_por_la_fecha(client, auth_headers, httpx_mock):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    _set_inbound_number(None)

    assert _conversation(client)["window_open"] is True


def test_la_sesion_se_renueva_y_dura(client, monkeypatch):
    import bcrypt
    import jwt

    from nexolu_comms_api.config import get_settings

    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_EMAIL", "ops@nexolu.test")
    monkeypatch.setenv("PANEL_PASSWORD_HASH", bcrypt.hashpw(b"clave", bcrypt.gensalt()).decode())
    get_settings.cache_clear()

    token = client.post("/panel/auth/login", json={"email": "ops@nexolu.test", "password": "clave"}).json()["token"]
    claims = jwt.decode(token, "secreto-de-prueba", algorithms=["HS256"])
    # Un año, no un dia.
    assert claims["exp"] - claims["iat"] >= 364 * 24 * 3600

    renewed = client.post("/panel/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert renewed.status_code == 200
    assert renewed.json()["user"]["email"] == "ops@nexolu.test"
    assert renewed.json()["token"]

    assert client.post("/panel/auth/refresh", headers={"Authorization": "Bearer basura"}).status_code == 401
