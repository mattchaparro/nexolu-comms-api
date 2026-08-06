from __future__ import annotations

import pytest

from nexolu_comms_api.core.channels.exceptions import UnknownChannelError
from nexolu_comms_api.core.channels.registry import ChannelRegistry


def test_available_lists_registered_channels():
    registry = ChannelRegistry()

    assert registry.available() == ["email", "whatsapp"]


def test_resolve_returns_the_matching_sender():
    registry = ChannelRegistry()

    assert registry.resolve("whatsapp").name == "whatsapp"
    assert registry.resolve("email").name == "email"


def test_resolve_raises_for_an_unknown_channel():
    registry = ChannelRegistry()

    with pytest.raises(UnknownChannelError):
        registry.resolve("sms")


def test_list_channels_endpoint(client, auth_headers):
    response = client.get("/v1/channels")

    assert response.status_code == 200
    assert response.json() == {"channels": ["email", "whatsapp"]}
