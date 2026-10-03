"""Omni-channel inbound bridge: dual /chat contract (live-driven on bundled Nunba).

Driving the inbound leg against the INSTALLED bundled Nunba surfaced two
contract mismatches that a prompt-only payload could never catch:
  - request: standalone HARTOS /chat reads "prompt"; the bundled Nunba
    chat_route (which shadows it on :5000) reads "text" → 400 "Text is required".
  - response: HARTOS returns the reply under "response"; chat_route under "text"
    → the bridge fell back to "I processed your request." instead of the reply.

The bridge now sends BOTH request keys and reads EITHER response key, so the
channel→agent leg works in both topologies. These pin that.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import Mock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _bare_integration():
    """A FlaskChannelIntegration with __init__ bypassed + deps mocked, so we
    can exercise _handle_message's payload + response handling in isolation."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    fi = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    fi.agent_api_url = 'http://test-local/chat'
    fi.default_user_id = 1
    fi.default_prompt_id = 1
    fi.create_mode = False
    fi._device_id = 'devtest'
    fi.registry = Mock()
    fi.registry.get.return_value = None  # no adapter → skip group-mention gate
    sess = Mock(user_id=None, prompt_id=None)
    sess.add_message = Mock()
    fi._session_manager = Mock()
    fi._session_manager.get_session.return_value = sess
    fi._self_chat = Mock()
    fi._self_chat.is_self_message.return_value = False
    fi._response_router = Mock()
    fi._resolve_user_id_for_sender = Mock(return_value=1)
    fi._get_channel_prompt_id = Mock(return_value=None)
    return fi


def _msg(text='hello agent'):
    from integrations.channels.base import Message
    return Message(id='m1', channel='telegram', sender_id='s1',
                   sender_name='S', chat_id='c1', text=text)


def test_inbound_payload_carries_both_prompt_and_text():
    try:
        fi = _bare_integration()
    except Exception as e:
        pytest.skip(f"flask_integration unavailable: {e}")
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None, **kwargs):
        captured['payload'] = json
        return Mock(status_code=200, json=lambda: {'response': 'ok'})

    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        fi._handle_message(_msg('drive me'))

    p = captured['payload']
    assert p['prompt'] == 'drive me', "standalone HARTOS /chat reads 'prompt'"
    assert p['text'] == 'drive me', "bundled Nunba chat_route reads 'text'"


def test_channel_sender_is_never_minted_an_admin_token():
    """A relay speaks for a stranger on a channel: its /chat token is a user's."""
    fi = _bare_integration()
    with patch('integrations.agent_engine.dispatch._internal_auth_headers',
               return_value=None) as mint, \
            patch('integrations.channels.flask_integration.pooled_post',
                  lambda *a, **k: Mock(status_code=200,
                                       json=lambda: {'response': 'ok'})):
        fi._handle_message(_msg())
    assert mint.call_args.kwargs['role'] == 'user'


def test_inbound_reads_reply_from_text_when_no_response_key():
    """Bundled chat_route returns the reply under 'text' — the bridge must use
    it, not fall back to the canned 'I processed your request.'"""
    try:
        fi = _bare_integration()
    except Exception as e:
        pytest.skip(f"flask_integration unavailable: {e}")

    def fake_post(url, json=None, timeout=None, headers=None, **kwargs):
        return Mock(status_code=200,
                    json=lambda: {'text': 'the real agent reply', 'agent_id': 'a1'})

    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        reply = fi._handle_message(_msg())
    assert reply == 'the real agent reply'


def test_inbound_reads_reply_from_response_key_standalone():
    """Standalone HARTOS /chat returns 'response' — still honored."""
    try:
        fi = _bare_integration()
    except Exception as e:
        pytest.skip(f"flask_integration unavailable: {e}")

    def fake_post(url, json=None, timeout=None, headers=None, **kwargs):
        return Mock(status_code=200, json=lambda: {'response': 'pong'})

    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        reply = fi._handle_message(_msg())
    assert reply == 'pong'


def test_self_chat_reply_is_not_delivered_twice():
    """SelfChatHandler performs its own registry delivery."""
    try:
        fi = _bare_integration()
    except Exception as e:
        pytest.skip(f"flask_integration unavailable: {e}")
    fi._self_chat.is_self_message.return_value = True
    fi._self_chat.handle.return_value = 'already delivered'

    assert fi._handle_message(_msg()) is None
    fi._self_chat.handle.assert_called_once()


def test_channel_reply_has_exactly_one_sender_for_the_originating_chat():
    """_handle_message RETURNS the reply and ChannelRegistry._route_to_agent
    sends it.  The response router must therefore not also send it to the
    originating chat -- it did once route_response gained its originating
    leg, delivering every channel reply twice."""
    try:
        fi = _bare_integration()
        from integrations.channels.response.router import ChannelResponseRouter
    except Exception as e:
        pytest.skip(f"flask_integration unavailable: {e}")
    router = ChannelResponseRouter(registry=Mock())
    fi._response_router = router

    def fake_post(url, json=None, timeout=None, headers=None, **kwargs):
        return Mock(status_code=200, json=lambda: {'response': 'pong'})

    with patch('integrations.channels.flask_integration.pooled_post', fake_post), \
         patch.object(router, 'upsert_binding'), \
         patch.object(router, 'log_user_message'), \
         patch.object(router, '_log_conversation'), \
         patch.object(router, '_async_fan_out') as fan_out, \
         patch.object(router, '_notify_desktop_wamp'), \
         patch.object(router, 'deliver_to_chat') as to_origin:
        reply = fi._handle_message(_msg())

    assert reply == 'pong', "the registry delivers the returned reply"
    to_origin.assert_not_called()
    # Fan-out still excludes the originating chat, as before.
    assert fan_out.call_args.kwargs['exclude_chat_id'] == 'c1'


# ── chat_contract: the single source both inbound paths share ──────────

def test_chat_contract_request_sends_both_keys():
    from integrations.channels.chat_contract import chat_request_fields
    assert chat_request_fields('hi') == {'prompt': 'hi', 'text': 'hi'}


def test_chat_contract_reply_reads_either_key():
    from integrations.channels.chat_contract import chat_reply
    assert chat_reply({'response': 'a'}) == 'a'           # standalone HARTOS
    assert chat_reply({'text': 'b'}) == 'b'               # bundled Nunba
    assert chat_reply({'response': 'a', 'text': 'b'}) == 'a'  # response preferred
    assert chat_reply({}, 'fallback') == 'fallback'
    assert chat_reply(None, 'fallback') == 'fallback'     # non-dict safe


def test_agent_turn_timeout_is_one_budget(monkeypatch):
    """Every /chat client of the same agent turn reads one budget."""
    from integrations.channels.chat_contract import (
        agent_turn_timeout, DEFAULT_AGENT_TURN_TIMEOUT_S)
    monkeypatch.delenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', raising=False)
    assert agent_turn_timeout() == DEFAULT_AGENT_TURN_TIMEOUT_S == 120
    monkeypatch.setenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', '300')
    assert agent_turn_timeout() == 300


def test_inbound_channel_post_uses_agent_turn_timeout(monkeypatch):
    try:
        fi = _bare_integration()
    except Exception as e:
        pytest.skip(f"flask_integration unavailable: {e}")
    monkeypatch.setenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', '444')
    seen = {}

    def fake_post(url, json=None, timeout=None, headers=None, **kwargs):
        seen['timeout'] = timeout
        return Mock(status_code=200, json=lambda: {'response': 'ok'})

    with patch('integrations.channels.flask_integration.pooled_post', fake_post):
        fi._handle_message(_msg())
    assert seen['timeout'] == 444


def test_self_chat_reply_not_returned_to_avoid_double_send():
    """SelfChatHandler.handle() already sends its own reply (private
    reply-in-thread) AND returns that same text. _handle_message must NOT
    hand that text back up to registry._route_to_agent, which
    unconditionally re-sends any non-empty string it receives — found
    live 2026-08-31: every escalated self-chat turn delivered "Let me
    check that for you…" TWICE, ~1ms apart, one send from
    SelfChatHandler._send_reply_in_thread and one from _route_to_agent
    re-sending _handle_message's return value."""
    try:
        fi = _bare_integration()
    except Exception as e:
        pytest.skip(f"flask_integration unavailable: {e}")

    fi._self_chat.is_self_message.return_value = True
    fi._self_chat.handle.return_value = "Let me check that for you…"

    reply = fi._handle_message(_msg())

    fi._self_chat.handle.assert_called_once()
    assert reply is None, (
        "a non-None return here gets re-sent verbatim by "
        "registry._route_to_agent, duplicating self-chat's own send"
    )


def test_self_chat_uses_shared_dual_contract():
    """SelfChatHandler (the 2nd inbound path) must send 'text' too + read
    'text' — it used to be prompt-only/response-only (parallel-path bug)."""
    import inspect
    from integrations.channels import self_chat
    src = inspect.getsource(self_chat)
    assert 'chat_request_fields' in src and 'chat_reply' in src, (
        "self_chat must go through the shared chat_contract, not a private "
        "prompt-only/response-only path")


@pytest.mark.parametrize('default_uid,session_uid,expected', [
    (10077, None, 'telegram:s1'),   # the shared guest id is never one identity
    (10077, 'u9', 'u9'),            # a paired session keeps its user
    (7, None, 7),                   # an operator-configured default is kept
])
def test_unbound_sender_never_shares_the_guest_identity(
        default_uid, session_uid, expected):
    fi = _bare_integration()
    fi.default_user_id = default_uid
    fi._session_manager.get_session.return_value.user_id = session_uid
    fi._resolve_user_id_for_sender = Mock(return_value='resolved')
    with patch('integrations.channels.flask_integration.pooled_post',
               lambda *a, **k: Mock(status_code=200,
                                    json=lambda: {'response': 'ok'})):
        fi._handle_message(_msg())
    assert fi._resolve_user_id_for_sender.call_args.kwargs['fallback'] == expected
