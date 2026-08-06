from __future__ import annotations

from nexolu_comms_api.config import EmailAppConfig, Settings, WhatsAppAppConfig, get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.email import EmailChannel


def _app(email: EmailAppConfig | None) -> AppIdentity:
    return AppIdentity(
        app_id="pos",
        api_key="k",
        name="Nexolu POS",
        whatsapp=WhatsAppAppConfig(phone_number_id="1", access_token="t"),
        email=email,
    )


async def test_skips_when_the_app_has_no_email_configured():
    channel = EmailChannel(get_settings())
    result = await channel.send(_app(None), OutboundMessage(to="a@b.com", subject="Hola", text="hola"))

    assert result.status == "skipped"


async def test_skips_when_no_brevo_key_is_available():
    settings = Settings(brevo_api_key="", nexolu_apps_json="{}")
    channel = EmailChannel(settings)
    app = _app(EmailAppConfig(from_email="no-reply@pos.nexolu.co"))  # sin brevo_api_key propia

    result = await channel.send(app, OutboundMessage(to="a@b.com", subject="Hola", text="hola"))

    assert result.status == "skipped"
    assert "Brevo" in result.error


async def test_falls_back_to_the_plain_text_wrapped_in_html_when_html_is_missing(httpx_mock):
    httpx_mock.add_response(json={"messageId": "<msg-1@brevo>"})
    channel = EmailChannel(get_settings())
    app = _app(EmailAppConfig(from_email="no-reply@pos.nexolu.co", from_name="Nexolu POS"))

    result = await channel.send(app, OutboundMessage(to="a@b.com", subject="Hola", text="3 productos bajos"))

    assert result.status == "sent"
    assert result.provider_message_id == "<msg-1@brevo>"
    assert result.cost_micros is None

    request = httpx_mock.get_requests()[0]
    assert request.headers["api-key"] == "platform-brevo-key"


async def test_uses_the_apps_own_brevo_key_when_it_has_one(httpx_mock):
    httpx_mock.add_response(json={"messageId": "<msg-2@brevo>"})
    channel = EmailChannel(get_settings())
    app = _app(EmailAppConfig(from_email="no-reply@pos.nexolu.co", brevo_api_key="pos-own-brevo-key"))

    await channel.send(app, OutboundMessage(to="a@b.com", subject="Hola", html="<p>hola</p>"))

    request = httpx_mock.get_requests()[0]
    assert request.headers["api-key"] == "pos-own-brevo-key"


async def test_reports_the_brevo_error_message_on_rejection(httpx_mock):
    httpx_mock.add_response(status_code=400, json={"message": "invalid sender"})
    channel = EmailChannel(get_settings())
    app = _app(EmailAppConfig(from_email="no-reply@pos.nexolu.co"))

    result = await channel.send(app, OutboundMessage(to="a@b.com", subject="Hola", text="hola"))

    assert result.status == "failed"
    assert result.error == "invalid sender"
