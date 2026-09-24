"""La recepcionista del Spa atiende en Connect, y el celular le avisa.

Lo que se prueba es lo que duele si falla:
- que el enlace del Spa entre a la persona como usuaria de SU salon, una
  sola vez por pase, y nunca como el admin que tambien puede ser;
- que desde ahi no pueda administrar la app ni ver otros salones;
- que el aviso al celular le llegue a quien puede ver la conversacion, y
  a nadie mas.
"""
from __future__ import annotations

from urllib.parse import parse_qs

import pytest

from tests.test_embed_chat import _embed_headers, _inbound

PUBLIC = "BPubKey"
PRIVATE = "priv"


@pytest.fixture
def panel_env(monkeypatch):
    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_BASE_URL", "https://connect.nexolu.test")
    monkeypatch.setenv("PANEL_EMAIL", "ops@nexolu.test")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", PRIVATE)

    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def sent(monkeypatch):
    """Reemplaza el envio real: registra a que endpoint salio y con que."""
    from nexolu_comms_api.core import push

    calls: list[tuple[str, dict]] = []
    codes: dict[str, int] = {}

    def fake_send(subscription, data):
        calls.append((subscription.endpoint, data))
        return push.PushResult(status_code=codes.get(subscription.endpoint, 201))

    monkeypatch.setattr(push, "send_web_push", fake_send)
    monkeypatch.setattr("nexolu_comms_api.api.v1.push.send_web_push", fake_send)
    return calls, codes


def _ticket(client, auth_headers, user_ref="12", business_id="7", name="Ana", next_path="/chat"):
    response = client.post(
        "/v1/app-users/login-ticket",
        json={"user_ref": user_ref, "business_id": business_id, "full_name": name, "next": next_path},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    assert url.startswith("https://connect.nexolu.test/entrar#")
    fragment = parse_qs(url.split("#", 1)[1])
    return fragment["ticket"][0], fragment["next"][0]


def _login_as_app_user(client, auth_headers, **kwargs) -> dict[str, str]:
    ticket, _ = _ticket(client, auth_headers, **kwargs)
    response = client.post("/panel/auth/ticket/exchange", json={"ticket": ticket})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _platform_session(client) -> dict[str, str]:
    """El operador de emergencia: rol platform, ve todo."""
    import os

    import bcrypt

    from nexolu_comms_api.config import get_settings

    os.environ["PANEL_PASSWORD_HASH"] = bcrypt.hashpw(b"clave", bcrypt.gensalt()).decode()
    get_settings.cache_clear()
    response = client.post("/panel/auth/login", json={"email": "ops@nexolu.test", "password": "clave"})
    os.environ.pop("PANEL_PASSWORD_HASH", None)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _subscribe(client, headers, endpoint):
    response = client.put(
        "/v1/push/subscriptions",
        json={"endpoint": endpoint, "keys": {"p256dh": "p256", "auth": "auth"}},
        headers=headers,
    )
    assert response.status_code == 200, response.text


# --- El pase -----------------------------------------------------------------


def test_el_pase_lo_pide_solo_una_app_autenticada(client, panel_env):
    body = {"user_ref": "12", "business_id": "7"}
    assert client.post("/v1/app-users/login-ticket", json=body).status_code == 401
    assert (
        client.post(
            "/v1/app-users/login-ticket", json=body, headers={"Authorization": "Bearer inventada"}
        ).status_code
        == 401
    )


def test_el_pase_entra_una_sola_vez_como_usuaria_de_su_salon(client, auth_headers, panel_env):
    ticket, next_path = _ticket(client, auth_headers, next_path="/chat?c=abc")
    assert next_path == "/chat?c=abc"

    first = client.post("/panel/auth/ticket/exchange", json={"ticket": ticket})
    assert first.status_code == 200
    user = first.json()["user"]
    assert user["roles"] == ["client"]
    assert user["app_ids"] == ["pos"]
    assert user["business_ids"] == ["7"]
    assert user["origin_app_id"] == "pos"
    assert user["full_name"] == "Ana"

    # Se quema al usarlo.
    assert client.post("/panel/auth/ticket/exchange", json={"ticket": ticket}).status_code == 401


def test_un_pase_vencido_no_entra(client, auth_headers, panel_env):
    import asyncio
    from datetime import datetime, timedelta

    from sqlalchemy import update

    from nexolu_comms_api.core.db.entities import PanelUser
    from nexolu_comms_api.core.db.session import get_sessionmaker

    ticket, _ = _ticket(client, auth_headers)

    async def expire():
        async with get_sessionmaker()() as session:
            await session.execute(
                update(PanelUser).values(login_ticket_expires_at=datetime.utcnow() - timedelta(seconds=1))
            )
            await session.commit()

    asyncio.run(expire())
    assert client.post("/panel/auth/ticket/exchange", json={"ticket": ticket}).status_code == 401


def test_la_ruta_de_destino_solo_puede_ser_local(client, auth_headers, panel_env):
    _, next_path = _ticket(client, auth_headers, next_path="//evil.test/robar")
    assert next_path == "/chat"


def test_cambiarla_de_salon_en_el_spa_le_quita_el_anterior(client, auth_headers, panel_env):
    _login_as_app_user(client, auth_headers, business_id="7")
    headers = _login_as_app_user(client, auth_headers, business_id="9")
    assert client.get("/panel/me", headers=headers).json()["business_ids"] == ["9"]


# --- Lo que puede ver --------------------------------------------------------


def test_ve_solo_los_chats_de_su_salon(client, auth_headers, httpx_mock, panel_env):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola, soy de Luxury", business_id="7")
    _inbound(client, httpx_mock, auth_headers, "573009998877", "Hola, soy del otro salon", business_id="9")

    headers = _login_as_app_user(client, auth_headers, business_id="7")
    chats = client.get("/v1/admin/chats", headers=headers).json()
    assert [c["phone"] for c in chats["items"]] == ["573001112233"]
    assert chats["unread_total"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "/v1/admin/flows",
        "/v1/admin/contacts",
        "/v1/admin/business-channels",
        "/v1/admin/webhook-events",
        "/v1/admin/catalog-items",
        "/v1/admin/whatsapp-flows",
        "/v1/admin/users",
    ],
)
def test_no_administra_la_app_de_todos_los_salones(client, auth_headers, panel_env, path):
    """Los flujos, contactos y canales del Spa son de TODOS los salones:
    quien vino a contestar los mensajes de uno no entra ahi. Tampoco la
    bandeja embebida, que tiene el mismo alcance."""
    for headers in (
        _login_as_app_user(client, auth_headers, business_id="7"),
        _embed_headers(client, "7", auth_headers),
    ):
        assert client.get(path, headers=headers).status_code in (401, 404), path


def test_su_enlace_no_la_entra_como_el_admin_que_tambien_es(client, auth_headers, panel_env):
    """La misma persona puede ser admin de plataforma con su correo real.
    El enlace del Spa la entra como usuaria del salon, no como admin."""
    headers = _login_as_app_user(client, auth_headers, user_ref="1")
    me = client.get("/panel/me", headers=headers).json()
    assert me["roles"] == ["client"]
    assert me["email"].endswith(".apps.connect")


def test_el_contador_del_menu_cuenta_solo_su_salon(client, auth_headers, httpx_mock, panel_env):
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    _inbound(client, httpx_mock, auth_headers, "573009998877", "Hola", business_id="9")
    response = client.get("/v1/app-users/unread", params={"business_id": "7"}, headers=auth_headers)
    assert response.json() == {"unread": 1}


# --- Los avisos --------------------------------------------------------------


def test_la_llave_publica_es_publica(client, panel_env):
    assert client.get("/v1/push/public-key").json() == {"enabled": True, "public_key": PUBLIC}


def test_suscribirse_exige_una_persona(client, auth_headers, panel_env):
    body = {"endpoint": "https://push.test/x", "keys": {"p256dh": "a", "auth": "b"}}
    assert client.put("/v1/push/subscriptions", json=body).status_code == 401
    # Ni la bandeja embebida ni la platform key tienen celular.
    assert client.put("/v1/push/subscriptions", json=body, headers=_embed_headers(client, "7", auth_headers)).status_code == 401
    assert client.put("/v1/push/subscriptions", json=body, headers={"Authorization": "Bearer platform-key"}).status_code == 401


def test_el_aviso_le_llega_a_quien_puede_ver_la_conversacion(client, auth_headers, httpx_mock, panel_env, sent):
    calls, _ = sent
    _subscribe(client, _login_as_app_user(client, auth_headers, user_ref="12", business_id="7"), "https://push.test/ana")
    _subscribe(client, _login_as_app_user(client, auth_headers, user_ref="13", business_id="9"), "https://push.test/otra")
    _subscribe(client, _platform_session(client), "https://push.test/admin")

    _inbound(client, httpx_mock, auth_headers, "573001112233", "Quiero una cita el sabado", business_id="7")

    endpoints = sorted(endpoint for endpoint, _ in calls)
    assert endpoints == ["https://push.test/admin", "https://push.test/ana"]

    data = calls[0][1]
    assert data["title"] == "Clienta 2233"
    assert data["body"] == "Quiero una cita el sabado"
    assert data["url"].startswith("/chat?c=")
    assert data["tag"] == f"chat-{data['contact_id']}"


def test_lo_que_sale_del_negocio_no_avisa(client, auth_headers, httpx_mock, panel_env, sent):
    calls, _ = sent
    _subscribe(client, _platform_session(client), "https://push.test/admin")

    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/123456/messages", json={"messages": [{"id": "wamid.solo-salida"}]}
    )
    client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={"business_id": "7", "channels": ["whatsapp"], "to": {"whatsapp": "573001112233"}, "text": "Hola"},
    )
    assert calls == []


def test_una_suscripcion_que_el_navegador_retiro_se_borra(client, auth_headers, httpx_mock, panel_env, sent):
    calls, codes = sent
    headers = _login_as_app_user(client, auth_headers, business_id="7")
    _subscribe(client, headers, "https://push.test/vieja")
    codes["https://push.test/vieja"] = 410

    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    assert len(calls) == 1

    calls.clear()
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Sigo aca", business_id="7")
    assert calls == []


def test_desactivarla_en_el_spa_corta_sesion_y_avisos(client, auth_headers, httpx_mock, panel_env, sent):
    calls, _ = sent
    headers = _login_as_app_user(client, auth_headers, user_ref="12", business_id="7")
    _subscribe(client, headers, "https://push.test/ana")

    assert client.delete("/v1/app-users/12", headers=auth_headers).status_code == 204

    assert client.get("/panel/me", headers=headers).status_code == 401
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    assert calls == []


def test_desuscribirse_desde_el_boton(client, auth_headers, httpx_mock, panel_env, sent):
    calls, _ = sent
    headers = _login_as_app_user(client, auth_headers, business_id="7")
    _subscribe(client, headers, "https://push.test/ana")

    response = client.request(
        "DELETE", "/v1/push/subscriptions", json={"endpoint": "https://push.test/ana"}, headers=headers
    )
    assert response.json() == {"subscribed": False}

    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    assert calls == []


def test_el_aviso_de_prueba_llega_solo_a_mis_celulares(client, auth_headers, panel_env, sent):
    calls, _ = sent
    ana = _login_as_app_user(client, auth_headers, user_ref="12", business_id="7")
    _subscribe(client, ana, "https://push.test/ana")
    _subscribe(client, _login_as_app_user(client, auth_headers, user_ref="13", business_id="7"), "https://push.test/otra")

    assert client.post("/v1/push/test", headers=ana).json() == {"sent": 1}
    assert [endpoint for endpoint, _ in calls] == ["https://push.test/ana"]


def test_sin_llaves_no_hay_avisos_y_nada_se_rompe(client, auth_headers, httpx_mock, monkeypatch, sent):
    monkeypatch.setenv("PANEL_JWT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PANEL_BASE_URL", "https://connect.nexolu.test")
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    assert client.get("/v1/push/public-key").json() == {"enabled": False, "public_key": ""}
    _inbound(client, httpx_mock, auth_headers, "573001112233", "Hola", business_id="7")
    assert sent[0] == []
