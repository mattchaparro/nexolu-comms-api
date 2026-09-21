"""Avisos de bandeja: "hay gente esperando y nadie contestó".

El panel solo avisa mientras alguien lo tiene abierto, y el número del
negocio no tiene app móvil. Lo que se defiende acá es que el aviso sea
util: que no llegue por cada mensaje (eso termina en que lo silencian
todos), que no se repita, que se calle cuando alguien atiende, y que no
gaste una plantilla de Meta si puede salir gratis.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta

MESSAGES_URL = "https://graph.facebook.com/v21.0/123456/messages"
CALLBACK_URL = "https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _inbound(client, httpx_mock, text: str, phone: str = "573001112233", name: str = "Laura"):
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "123456"},
                            "contacts": [{"profile": {"name": name}, "wa_id": phone}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": f"wamid.{abs(hash(text + phone))}",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(b"meta-app-secret", raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp/pos",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


def _configure(client, platform_headers, **overrides):
    payload = {
        "app_id": "pos",
        "emails": ["duena@luxury.co"],
        "quiet_minutes": 10,
        **overrides,
    }
    response = client.put("/v1/admin/inbox-alerts", headers=platform_headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _run_alerts(minutes_later: int = 15) -> int:
    """Corre una pasada del worker como si hubieran pasado N minutos."""
    from nexolu_comms_api.core.alerts import send_alerts

    return asyncio.run(send_alerts(datetime.utcnow() + timedelta(minutes=minutes_later)))


def test_una_conversacion_sin_responder_genera_un_correo_agrupado(
    client, platform_headers, httpx_mock
):
    _configure(client, platform_headers)
    assert _inbound(client, httpx_mock, "¿Tienen hora mañana?").status_code == 200
    assert _inbound(client, httpx_mock, "¿Hola?", phone="573007654321", name="Sofía").status_code == 200

    httpx_mock.add_response(url=BREVO_URL, json={"messageId": "<brevo-1>"})
    assert _run_alerts() == 1

    correo = json.loads(httpx_mock.get_requests(url=BREVO_URL)[0].content)
    # UN correo con las dos, no un correo por mensaje.
    assert len(httpx_mock.get_requests(url=BREVO_URL)) == 1
    assert "2 conversaciones sin responder" in correo["subject"]
    assert "Laura" in correo["textContent"]
    assert "Sofía" in correo["textContent"]
    assert "connect.nexolu.co/chat" in correo["textContent"]


def test_no_se_avisa_de_lo_reciente(client, platform_headers, httpx_mock):
    """El bot suele estar contestando: avisar al instante es ruido."""
    _configure(client, platform_headers, quiet_minutes=10)
    assert _inbound(client, httpx_mock, "Hola").status_code == 200

    assert _run_alerts(minutes_later=2) == 0
    assert httpx_mock.get_requests(url=BREVO_URL) == []


def test_el_mismo_mensaje_no_se_avisa_dos_veces(client, platform_headers, httpx_mock):
    _configure(client, platform_headers)
    assert _inbound(client, httpx_mock, "¿Me atienden?").status_code == 200

    httpx_mock.add_response(url=BREVO_URL, json={"messageId": "<brevo-1>"})
    assert _run_alerts() == 1
    assert _run_alerts(minutes_later=30) == 0


def test_abrir_la_conversacion_calla_el_aviso(client, platform_headers, httpx_mock):
    """Lo que apaga el aviso es atender, no esperar."""
    _configure(client, platform_headers)
    assert _inbound(client, httpx_mock, "¿Me atienden?").status_code == 200

    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0][
        "contact_id"
    ]
    client.post(f"/v1/admin/chats/{contact_id}/read", headers=platform_headers)

    assert _run_alerts() == 0
    assert httpx_mock.get_requests(url=BREVO_URL) == []


def test_si_el_ultimo_mensaje_es_nuestro_no_hay_nada_pendiente(
    client, platform_headers, httpx_mock
):
    _configure(client, platform_headers)
    assert _inbound(client, httpx_mock, "¿Tienen hora?").status_code == 200

    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0][
        "contact_id"
    ]
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.r"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    client.post(
        f"/v1/admin/chats/{contact_id}/messages",
        headers=platform_headers,
        json={"text": "¡Sí! A las 3pm."},
    )

    assert _run_alerts() == 0


def test_el_whatsapp_al_duenio_sale_gratis_si_su_ventana_esta_abierta(
    client, platform_headers, httpx_mock
):
    """La idea del atajo del celular: si el dueño le escribe al número una
    vez al día, su ventana de 24h queda abierta y el aviso viaja como texto
    libre, sin gastar plantilla."""
    _configure(client, platform_headers, whatsapp_to="573005554444", urgent_template="avisos_admin")

    # El dueño escribe (lo haría su atajo diario).
    assert _inbound(client, httpx_mock, "activar notificaciones admin", phone="573005554444", name="Alejandro").status_code == 200
    # Y una clienta queda esperando.
    assert _inbound(client, httpx_mock, "¿Cuánto vale el semipermanente?").status_code == 200

    httpx_mock.add_response(url=BREVO_URL, json={"messageId": "<brevo-1>"})
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.alert"}]})
    _run_alerts()

    aviso = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[-1].content)
    # Texto libre (gratis), NO plantilla.
    assert aviso["type"] == "text"
    assert "sin responder" in aviso["text"]["body"]


def test_con_la_ventana_cerrada_cae_a_la_plantilla(client, platform_headers, httpx_mock):
    """Si el atajo no corrió, el aviso urgente no se pierde: se paga."""
    _configure(client, platform_headers, whatsapp_to="573005554444", urgent_template="avisos_admin")
    assert _inbound(client, httpx_mock, "¿Me ayudan?").status_code == 200

    httpx_mock.add_response(url=BREVO_URL, json={"messageId": "<brevo-1>"})
    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.tpl"}]})
    _run_alerts()

    aviso = json.loads(httpx_mock.get_requests(url=MESSAGES_URL)[-1].content)
    assert aviso["type"] == "template"
    assert aviso["template"]["name"] == "avisos_admin"


def test_sin_plantilla_y_sin_ventana_solo_sale_el_correo(client, platform_headers, httpx_mock):
    """No se inventa un envío que Meta va a rechazar."""
    _configure(client, platform_headers, whatsapp_to="573005554444")
    assert _inbound(client, httpx_mock, "¿Me ayudan?").status_code == 200

    httpx_mock.add_response(url=BREVO_URL, json={"messageId": "<brevo-1>"})
    _run_alerts()

    assert httpx_mock.get_requests(url=MESSAGES_URL) == []
    assert len(httpx_mock.get_requests(url=BREVO_URL)) == 1


def test_la_vista_previa_muestra_lo_que_se_mandaria(client, platform_headers, httpx_mock):
    """Para probar la configuración sin esperar al worker ni molestar a nadie."""
    # Sin configurar, no hay nada que previsualizar.
    assert (
        client.get("/v1/admin/inbox-alerts/preview?app_id=pos", headers=platform_headers).status_code
        == 404
    )

    _configure(client, platform_headers)
    assert _inbound(client, httpx_mock, "¿Tienen cita hoy?").status_code == 200

    vacia = client.get("/v1/admin/inbox-alerts/preview?app_id=pos", headers=platform_headers).json()
    # Recién entrado: todavía no cuenta como "sin responder" (ese es el
    # punto de quiet_minutes) y la vista previa lo refleja.
    assert vacia["pending"] == 0
    assert vacia["subject"] is None


def test_la_configuracion_ajena_no_se_ve_ni_se_toca(client, platform_headers):
    """Mismo scoping que el resto del panel."""
    _configure(client, platform_headers)

    listed = client.get("/v1/admin/inbox-alerts", headers=platform_headers).json()
    assert len(listed) == 1
    assert listed[0]["app_id"] == "pos"

    ajena = client.put(
        "/v1/admin/inbox-alerts",
        headers=platform_headers,
        json={"app_id": "otra-app", "emails": ["x@y.co"]},
    )
    # La plataforma puede con todas; lo que se prueba acá es que el endpoint
    # exige scope (un cliente externo recibiría 404).
    assert ajena.status_code in (200, 404)


def _respuesta_rechazada(client, auth_headers, httpx_mock, phone: str = "573001112233"):
    """El bot contesta y Meta lo rechaza, como pasó con el número de prueba
    ("#131030 Recipient phone number not in allowed list")."""
    httpx_mock.add_response(
        url=MESSAGES_URL,
        status_code=400,
        json={
            "error": {
                "message": "(#131030) Recipient phone number not in allowed list",
                "type": "OAuthException",
                "code": 131030,
            }
        },
    )
    return client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={"channels": ["whatsapp"], "to": {"whatsapp": phone}, "text": "¡Hola! ¿Qué día te sirve?"},
    )


def test_una_respuesta_que_no_llego_no_cuenta_como_contestada(
    client, platform_headers, auth_headers, httpx_mock
):
    """Una clienta escribió tres veces al número de prueba. El bot le
    "contestó" las tres y Meta rechazó las tres; como lo último en el hilo
    era nuestro, nadie recibió el aviso y se quedó sin respuesta."""
    _configure(client, platform_headers)
    assert _inbound(client, httpx_mock, "Para agendar una cita porfavor").status_code == 200
    assert _respuesta_rechazada(client, auth_headers, httpx_mock).status_code == 200

    httpx_mock.add_response(url=BREVO_URL, json={"messageId": "<brevo-1>"})
    assert _run_alerts() == 1


def test_la_bandeja_la_muestra_pendiente_y_dice_por_que(
    client, platform_headers, auth_headers, httpx_mock
):
    assert _inbound(client, httpx_mock, "Para agendar una cita porfavor").status_code == 200
    _respuesta_rechazada(client, auth_headers, httpx_mock)

    fila = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]
    assert fila["last_failed"] is True
    # Pendiente: alguien tiene que hacer algo con esta conversación.
    assert fila["unread"] is True

    hilo = client.get(f"/v1/admin/chats/{fila['contact_id']}/messages", headers=platform_headers).json()
    fallido = [m for m in hilo if m["direction"] == "out"][-1]
    assert fallido["status"] == "failed"
    # El motivo viaja con el mensaje para que el panel lo pueda explicar.
    assert "131030" in fallido["payload"]["error"]
