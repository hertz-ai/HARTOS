"""Unit tests for SelfChatHandler (owner-messages-own-number flow).

Tests cover:
  * Phone normalization (+, spaces, @c.us, @s.whatsapp.net)
  * is_self_message detection (match, mismatch, disabled, no config)
  * handle(): persist → dispatch → reply-in-thread (mocked)
  * handle(): fan-out is NOT invoked (private thread)
  * handle(): agent-API failure returns None and logs
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


# Arbitrary fake number for all fixtures — NEVER use a real one.
FAKE_PHONE = "+15551234567"
FAKE_JID = "15551234567@c.us"


# ─── Fixtures ──────────────────────────────────────────────────────
@pytest.fixture
def fake_message():
    m = MagicMock()
    m.channel = "whatsapp"
    m.sender_id = FAKE_JID
    m.sender_name = "Owner"
    m.chat_id = FAKE_JID
    m.is_group = False
    m.is_bot_mentioned = False
    m.content = "remind me to call dad at 6pm"
    m.id = "msg-1"
    return m


@pytest.fixture
def fake_adapter_with_owner():
    adapter = MagicMock()
    adapter.config.extra = {
        "phone_number": FAKE_PHONE,
        "enable_self_chat_agent": True,
    }
    return adapter


@pytest.fixture
def fake_registry(fake_adapter_with_owner):
    reg = MagicMock()
    reg.get.return_value = fake_adapter_with_owner
    async def _send(*args, **kwargs):
        return MagicMock(success=True, message_id="out-1")
    reg.send_to_channel.side_effect = _send
    return reg


@pytest.fixture
def handler(fake_registry):
    from integrations.channels.self_chat import SelfChatHandler
    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    h = SelfChatHandler(
        agent_api_url="http://localhost:6777/chat",
        owner_user_id=10077,
        owner_prompt_id=8888,
        device_id="dev-abc",
        session_manager=MagicMock(),
        response_router=MagicMock(),
        registry=fake_registry,
        get_loop=lambda: loop,
    )
    yield h
    loop.call_soon_threadsafe(loop.stop)


# ─── normalize_phone ─────────────────────────────────────────────
@pytest.mark.parametrize("given, expected", [
    ("+1 555 123 4567",                "15551234567"),
    ("15551234567@c.us",                "15551234567"),
    ("15551234567@s.whatsapp.net",      "15551234567"),
    ("+15551234567",                    "15551234567"),
    ("5551234567",                      "5551234567"),
    ("",                                ""),
    (None,                              ""),
])
def test_normalize_phone(given, expected):
    from integrations.channels.self_chat import normalize_phone
    assert normalize_phone(given) == expected


# ─── is_self_message ─────────────────────────────────────────────
def test_is_self_message_matches(handler, fake_message):
    assert handler.is_self_message(fake_message) is True


def test_is_self_message_mismatch_returns_false(handler, fake_message):
    fake_message.sender_id = "19995551234@c.us"
    assert handler.is_self_message(fake_message) is False


def test_is_self_message_disabled_returns_false(handler, fake_message,
                                                 fake_adapter_with_owner):
    fake_adapter_with_owner.config.extra["enable_self_chat_agent"] = False
    assert handler.is_self_message(fake_message) is False


def test_is_self_message_no_owner_config_returns_false(handler, fake_message,
                                                        fake_adapter_with_owner):
    fake_adapter_with_owner.config.extra = {}
    assert handler.is_self_message(fake_message) is False


def test_is_self_message_no_adapter_returns_false(handler, fake_message,
                                                   fake_registry):
    fake_registry.get.return_value = None
    assert handler.is_self_message(fake_message) is False


# ─── handle() ────────────────────────────────────────────────────
@patch("integrations.channels.self_chat.pooled_post")
def test_handle_persists_dispatches_and_replies(mock_post, handler,
                                                  fake_message):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"response": "✓ reminder set for 6pm"},
    )
    session = MagicMock()

    reply = handler.handle(fake_message, session)

    assert reply == "✓ reminder set for 6pm"

    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["user_id"] == 10077
    assert payload["prompt_id"] == 8888
    assert payload["channel_context"]["is_self_chat"] is True
    assert payload["prompt"] == "remind me to call dad at 6pm"

    assert session.add_message.call_count == 2
    session.add_message.assert_any_call("user", fake_message.content)
    session.add_message.assert_any_call("assistant", "✓ reminder set for 6pm")

    handler.response_router.log_user_message.assert_called_once_with(
        10077, "whatsapp", fake_message.content,
    )
    handler.response_router.route_response.assert_not_called()  # no fan-out

    handler.registry.send_to_channel.assert_called_once()
    args = handler.registry.send_to_channel.call_args.args
    assert args[0] == "whatsapp"
    assert args[1] == fake_message.chat_id
    assert args[2] == "✓ reminder set for 6pm"


@patch("integrations.channels.self_chat.pooled_post")
def test_handle_agent_500_returns_none(mock_post, handler, fake_message):
    mock_post.return_value = MagicMock(status_code=500, text="boom")
    assert handler.handle(fake_message, None) is None


@patch("integrations.channels.self_chat.pooled_post")
def test_handle_empty_reply_falls_back_to_noted(mock_post, handler,
                                                 fake_message):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"response": ""},
    )
    reply = handler.handle(fake_message, None)
    assert reply == "✓ noted"
