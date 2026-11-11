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
