"""La bandeja muestra lo que de verdad le llego a la persona.

Dos cosas que se veian mal en el hilo de Marcela:

- La plantilla salia como "[plantilla cita_nueva_equipo] Marcela · Carolina ·
  ...", el resumen de lo que se envio, no el mensaje que ella recibio.
- Los dos avisos tenian el check verde de "enviado" mientras Meta los habia
  rechazado segundos despues.
"""
from nexolu_comms_api.core.chats import render_template_bubble

AVISO_EQUIPO = [
    {
        "type": "BODY",
        "text": "¡Hola, {{1}}! Te agendaron una cita 💅\n\nClienta: *{{2}}*\nServicio: *{{3}}*\n"
        "Día: {{4}}\nHora: {{5}}\n\nRevisa tu agenda para ver el detalle.",
    }
]

CONFIRMACION = [
    {"type": "HEADER", "format": "TEXT", "text": "Tu cita"},
    {"type": "BODY", "text": "Te esperamos el {{1}} a las {{2}}."},
    {"type": "FOOTER", "text": "Luxury Nails"},
    {
        "type": "BUTTONS",
        "buttons": [
            {"type": "QUICK_REPLY", "text": "Info de garantías"},
            {"type": "QUICK_REPLY", "text": "Cancelaciones"},
        ],
    },
]


def test_la_plantilla_se_lee_como_la_recibio_la_persona():
    texto, botones = render_template_bubble(
        AVISO_EQUIPO,
        ["Marcela", "Carolina Vivares", "Pedi + Jelly Spa + Semi", "Viernes 25 de septiembre", "6:00 pm"],
    )

    assert texto.startswith("¡Hola, Marcela! Te agendaron una cita")
    assert "Clienta: *Carolina Vivares*" in texto
    assert "Hora: 6:00 pm" in texto
    assert "[plantilla" not in texto
    assert botones == []


def test_encabezado_pie_y_botones_tambien_aparecen():
    texto, botones = render_template_bubble(CONFIRMACION, ["jueves 25", "3:00 pm"])

    assert texto.startswith("*Tu cita*")
    assert "Te esperamos el jueves 25 a las 3:00 pm." in texto
    assert texto.endswith("_Luxury Nails_")
    assert [b["title"] for b in botones] == ["Info de garantías", "Cancelaciones"]


def test_un_valor_que_falta_no_deja_un_hueco():
    # Mejor ver el marcador que un texto que parece salido incompleto.
    texto, _ = render_template_bubble(CONFIRMACION, ["jueves 25"])

    assert "a las {{2}}" in texto


# --- De punta a punta: la bandeja real ------------------------------------

import asyncio  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402

from tests.test_admin_chats import CALLBACK_URL, MESSAGES_URL, _inbound_text  # noqa: E402


def _con_plantilla(components):
    from nexolu_comms_api.core.db.entities import WhatsAppTemplate
    from nexolu_comms_api.core.db.session import get_sessionmaker

    async def seed():
        async with get_sessionmaker()() as session:
            session.add(
                WhatsAppTemplate(
                    app_id="pos", waba_id="", name="cita_nueva_equipo", language="es",
                    category="UTILITY", status="APPROVED", components=components,
                )
            )
            await session.commit()

    asyncio.run(seed())


def _acuse(client, httpx_mock, wamid: str, estado: dict):
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    body = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "123456"},
        "statuses": [{"id": wamid, "recipient_id": "573001112233", **estado}],
    }}]}]}
    raw = json.dumps(body).encode()
    firma = "sha256=" + hmac.new(b"meta-app-secret", raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp/pos", content=raw,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": firma})


def _mandar_aviso(client, platform_headers, httpx_mock) -> str:
    assert _inbound_text(client, httpx_mock, "hola").status_code == 200
    contact_id = client.get("/v1/admin/chats", headers=platform_headers).json()["items"][0]["contact_id"]
    _con_plantilla(AVISO_EQUIPO)

    httpx_mock.add_response(url=MESSAGES_URL, json={"messages": [{"id": "wamid.marcela"}]})
    httpx_mock.add_response(url=CALLBACK_URL, json={"ok": True})
    enviado = client.post(
        f"/v1/admin/chats/{contact_id}/messages",
        headers=platform_headers,
        json={"template": {"name": "cita_nueva_equipo", "language": "es",
                           "params": ["Marcela", "Carolina Vivares", "Pedi", "Viernes 25", "6:00 pm"]}},
    )
    assert enviado.status_code == 201, enviado.text
    return contact_id


def _burbuja(client, platform_headers, contact_id):
    mensajes = client.get(f"/v1/admin/chats/{contact_id}/messages", headers=platform_headers).json()
    return next(m for m in mensajes if m["message_type"] == "template")


def test_el_hilo_muestra_la_plantilla_armada(client, platform_headers, httpx_mock):
    contact_id = _mandar_aviso(client, platform_headers, httpx_mock)

    burbuja = _burbuja(client, platform_headers, contact_id)
    assert burbuja["body"].startswith("¡Hola, Marcela! Te agendaron una cita")
    assert "Clienta: *Carolina Vivares*" in burbuja["body"]


def test_si_meta_lo_rechaza_despues_la_bandeja_lo_dice(client, platform_headers, httpx_mock):
    contact_id = _mandar_aviso(client, platform_headers, httpx_mock)
    assert _burbuja(client, platform_headers, contact_id)["status"] != "failed"

    assert _acuse(client, httpx_mock, "wamid.marcela", {
        "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}],
    }).status_code == 200

    burbuja = _burbuja(client, platform_headers, contact_id)
    assert burbuja["status"] == "failed"
    assert "24 horas" in burbuja["payload"]["error"]


def test_un_acuse_tardio_no_hace_retroceder_el_estado(client, platform_headers, httpx_mock):
    contact_id = _mandar_aviso(client, platform_headers, httpx_mock)

    _acuse(client, httpx_mock, "wamid.marcela", {"status": "read"})
    _acuse(client, httpx_mock, "wamid.marcela", {"status": "delivered"})

    assert _burbuja(client, platform_headers, contact_id)["status"] == "read"
