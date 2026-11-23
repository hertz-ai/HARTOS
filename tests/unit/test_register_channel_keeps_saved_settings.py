"""register_channel (the chat Connect_Channel tool, the pairing watcher's
re-register, /channels/<type>/connect) saves the channel's config into the
admin config.  It replaced the whole saved ``config``, so a setting the
caller did not send was erased: the owner's WhatsApp "Self-chat → Nunba"
switch, saved OFF from the Add Channel wizard, came back ON after a chat
connect or a re-pair (self_chat.saved_self_chat_setting reads it there).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def saved_channels(monkeypatch):
    channels = {}
    api = MagicMock()
    api._channels = channels
    monkeypatch.setattr("integrations.channels.admin.api.get_api", lambda: api)
    from integrations.channels import agent_tools
    # No user: no binding row is written.
    monkeypatch.setattr(agent_tools, "_get_user_id_from_threadlocal", lambda: None)
    return channels


def _register_channel():
    from integrations.channels.agent_tools import build_channel_tool_closures
    tools = build_channel_tool_closures({"user_id": None, "prompt_id": None})
    return next(fn for name, _desc, fn in tools if name == "register_channel")


def test_reconnect_keeps_the_saved_self_chat_switch(saved_channels):
    saved_channels["whatsapp"] = {
        "channel_type": "whatsapp", "name": "WhatsApp", "enabled": True,
        "config": {"enable_self_chat_agent": False},
    }

    out = _register_channel()("whatsapp", "{}")

    assert "registered and enabled" in out
    from integrations.channels.self_chat import saved_self_chat_setting
    assert saved_self_chat_setting("whatsapp") is False


def test_values_the_caller_sends_still_replace_saved_ones(saved_channels):
    saved_channels["whatsapp"] = {
        "channel_type": "whatsapp", "enabled": False,
        "config": {"enable_self_chat_agent": False,
                   "phone_number": "+15550000000"},
    }

    _register_channel()(
        "whatsapp",
        '{"enable_self_chat_agent": true, "phone_number": "+15551234567"}')

    entry = saved_channels["whatsapp"]
    assert entry["enabled"] is True
    assert entry["config"]["enable_self_chat_agent"] is True
    assert entry["config"]["phone_number"] == "+15551234567"


def test_a_repair_keeps_the_operators_saved_gateway_url(saved_channels, monkeypatch):
    """Review of #167 (20 Nov, S6): the auto-fill ran first, so a re-pair
    ('{}') put api_url back to the schema default (localhost) over the
    operator's saved gateway.  A saved value now comes before the
    environment and the default; a value the caller sends still wins."""
    monkeypatch.delenv('WHATSAPP_API_URL', raising=False)
    saved_channels["whatsapp"] = {
        "channel_type": "whatsapp", "enabled": True,
        "config": {"api_url": "https://waha.example.test",
                   "phone_number": "+15551234567"},
    }

    _register_channel()("whatsapp", "{}")

    config = saved_channels["whatsapp"]["config"]
    assert config["api_url"] == "https://waha.example.test"
    assert config["phone_number"] == "+15551234567"


def test_a_new_channel_still_gets_the_default_gateway_url(saved_channels, monkeypatch):
    monkeypatch.delenv('WHATSAPP_API_URL', raising=False)
    _register_channel()("whatsapp", "{}")
    assert saved_channels["whatsapp"]["config"]["api_url"] == "http://localhost:3000"


def test_a_gateway_url_the_caller_sends_replaces_the_saved_one(saved_channels):
    saved_channels["whatsapp"] = {
        "channel_type": "whatsapp", "enabled": True,
        "config": {"api_url": "https://old.example.test"},
    }
    _register_channel()("whatsapp", '{"api_url": "https://new.example.test"}')
    assert saved_channels["whatsapp"]["config"]["api_url"] == "https://new.example.test"


def test_the_env_gateway_url_still_wins_over_a_saved_default(saved_channels, monkeypatch):
    """Review of 58639311 (X1): the first connect saves the auto-filled
    default; a WHATSAPP_API_URL set later must still apply on a re-pair.
    Only a saved value that is not the default is the operator's own."""
    monkeypatch.delenv('WHATSAPP_API_URL', raising=False)
    _register_channel()("whatsapp", "{}")
    assert saved_channels["whatsapp"]["config"]["api_url"] == "http://localhost:3000"

    monkeypatch.setenv('WHATSAPP_API_URL', 'https://env.example.test')
    _register_channel()("whatsapp", "{}")
    assert saved_channels["whatsapp"]["config"]["api_url"] == "https://env.example.test"
