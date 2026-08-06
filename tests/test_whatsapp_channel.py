from __future__ import annotations

import json

from nexolu_comms_api.config import EmailAppConfig, WhatsAppAppConfig, get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.whatsapp import WhatsAppChannel


def _app(whatsapp: WhatsAppAppConfig | None) -> AppIdentity:
    return AppIdentity(app_id="pos", api_key="k", name="Nexolu POS", whatsapp=whatsapp, email=EmailAppConfig(from_email="a@b.com"))


async def test_skips_when_the_app_has_no_whatsapp_configured():
    channel = WhatsAppChannel(get_settings())
    result = await channel.send(_app(None), OutboundMessage(to="+573001234567", text="hola"))

    assert result.status == "skipped"
    assert "no configurado" in result.error


async def test_fails_when_the_message_has_neither_text_nor_template():
    channel = WhatsAppChannel(get_settings())
    app = _app(WhatsAppAppConfig(phone_number_id="123", access_token="tok"))

    result = await channel.send(app, OutboundMessage(to="+573001234567"))

    assert result.status == "failed"


async def test_sends_free_text_and_reports_the_provider_message_id(httpx_mock):
    httpx_mock.add_response(
        url="https://graph.facebook.com/v21.0/123/messages",
        method="POST",
        json={"messages": [{"id": "wamid.abc"}]},
    )
    channel = WhatsAppChannel(get_settings())
    app = _app(WhatsAppAppConfig(phone_number_id="123", access_token="tok"))

    result = await channel.send(app, OutboundMessage(to="+573001234567", text="hola"))

    assert result.status == "sent"
    assert result.provider_message_id == "wamid.abc"
    assert result.cost_micros is None  # texto libre sin categoria: costo desconocido, no cero

    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer tok"


async def test_sends_a_template_and_estimates_cost_by_category(httpx_mock):
    httpx_mock.add_response(json={"messages": [{"id": "wamid.def"}]})
    channel = WhatsAppChannel(get_settings())
    app = _app(WhatsAppAppConfig(phone_number_id="123", access_token="tok"))

    result = await channel.send(
        app,
        OutboundMessage(
            to="+573001234567",
            template_name="low_stock_alert",
            template_language="es_CO",
            category="utility",
        ),
    )

    assert result.status == "sent"
    assert result.cost_micros == get_settings().whatsapp_rate_utility_micros


async def test_sends_a_flow_with_the_data_and_flow_token_untouched(httpx_mock):
    httpx_mock.add_response(json={"messages": [{"id": "wamid.flow1"}]})
    channel = WhatsAppChannel(get_settings())
    app = _app(WhatsAppAppConfig(phone_number_id="123", access_token="tok"))

    result = await channel.send(
        app,
        OutboundMessage(
            to="+573001234567",
            text="Confirma los datos del gasto:",
            flow_id="9988",
            flow_screen="GASTO",
            flow_cta="Confirmar",
            flow_token="draft-abc-123",
            flow_data={"concepto": "Arriendo", "monto": 50000},
        ),
    )

    assert result.status == "sent"
    assert result.provider_message_id == "wamid.flow1"

    request = httpx_mock.get_requests()[0]
    body = json.loads(request.content)
    assert body["interactive"]["type"] == "flow"
    assert body["interactive"]["action"]["parameters"]["flow_id"] == "9988"
    assert body["interactive"]["action"]["parameters"]["flow_token"] == "draft-abc-123"
    assert body["interactive"]["action"]["parameters"]["flow_action_payload"]["screen"] == "GASTO"
    assert body["interactive"]["action"]["parameters"]["flow_action_payload"]["data"]["monto"] == 50000


async def test_reports_the_meta_error_message_on_rejection(httpx_mock):
    httpx_mock.add_response(status_code=400, json={"error": {"message": "Numero invalido"}})
    channel = WhatsAppChannel(get_settings())
    app = _app(WhatsAppAppConfig(phone_number_id="123", access_token="tok"))

    result = await channel.send(app, OutboundMessage(to="bad-number", text="hola"))

    assert result.status == "failed"
    assert result.error == "Numero invalido"
