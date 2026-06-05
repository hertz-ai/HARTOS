"""Omni-channel bridge — drive Legs 2 (outbound) + 3 (cross-channel relay).

Leg 1 (inbound) was driven live against the bundled Nunba. Legs 2 & 3's
EXTERNAL transport (a real Telegram API / a running crossbar router) isn't
available here, but the bridge's own LOGIC is — crossbar only delivers the
channel event to the bridge; we feed it directly. These exercise the real
registry + ChannelBridge code with recording adapters as the channel
endpoints, so both legs are verified end-to-end through the bridge:

  Leg 2: a Nunba reply -> registry.send_to_channel -> adapter.send_message.
  Leg 3: a channel-A event -> bridge rule match -> forward -> channel-B send.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _run(coro):
    from core.event_loop import run_async  # canonical sync->async runner
    return run_async(coro)


def _recording_adapter(name):
    from integrations.channels.base import ChannelAdapter, ChannelConfig, SendResult

    class _Rec(ChannelAdapter):
        def __init__(self):
            super().__init__(ChannelConfig())
            self.sent = []

        @property
        def name(self):
            return name

        def is_running(self):  # so registry.send_to_channel delivers
            return True

        async def connect(self):
            return True

        async def disconnect(self):
            pass

        async def send_message(self, chat_id, text, reply_to=None, media=None,
                               buttons=None):
            self.sent.append((chat_id, text))
            return SendResult(success=True, message_id=f'rec-{len(self.sent)}')

        async def edit_message(self, chat_id, message_id, text, buttons=None):
            return SendResult(success=True)

        async def delete_message(self, chat_id, message_id):
            return True

        async def send_typing(self, chat_id):
            pass

        async def get_chat_info(self, chat_id):
            return {}

    return _Rec()


def test_leg2_outbound_delivers_via_registry():
    """Leg 2: the outbound delivery primitive (used by announcement_broadcaster
    + response_router fan-out) reaches the channel adapter."""
    try:
        from integrations.channels.registry import ChannelRegistry
    except Exception as e:
        pytest.skip(f"channels unavailable: {e}")
    reg = ChannelRegistry()
    a = _recording_adapter('rec')
    reg.register(a)

    res = _run(reg.send_to_channel('rec', 'chat-1', 'outbound-hello'))

    assert res.success, f"send_to_channel failed: {getattr(res, 'error', '?')}"
    assert a.sent == [('chat-1', 'outbound-hello')], (
        "outbound message did not reach the channel adapter")


def test_leg3_cross_channel_relay_forwards_per_rule(tmp_path):
    """Leg 3: a message on channel src, run through the real ChannelBridge
    routing with a FORWARD rule, lands on channel dst (no crossbar needed —
    the event is fed directly to the bridge's handler)."""
    try:
        from integrations.channels.registry import ChannelRegistry
        from integrations.channels.bridge.wamp_bridge import (
            ChannelBridge, BridgeConfig, BridgeRule, RouteType)
    except Exception as e:
        pytest.skip(f"channel bridge unavailable: {e}")
    reg = ChannelRegistry()
    src = _recording_adapter('src')
    dst = _recording_adapter('dst')
    reg.register(src)
    reg.register(dst)

    # Per-test rules file so add_rule's persistence never touches a shared path.
    bridge = ChannelBridge(BridgeConfig(rules_file=str(tmp_path / 'rules.json')), reg)
    bridge.add_rule(BridgeRule(
        id='r1', name='src->dst', source_channel='src',
        target_channel='dst', route_type=RouteType.FORWARD))

    _run(bridge._on_channel_message({
        'channel': 'src', 'chat_id': 'chatX', 'text': 'relay-me',
        'message_id': 'm1', 'sender_name': 'alice'}))

    assert len(dst.sent) == 1, "message was not forwarded to the target channel"
    chat_id, text = dst.sent[0]
    assert chat_id == 'chatX'                 # same chat (no target_chat_id override)
    assert 'relay-me' in text                 # original content forwarded
    assert src.sent == [], "source channel must not be echoed back to"


def test_leg3_relay_skips_disabled_and_nonmatching_rules(tmp_path):
    """A disabled rule + a rule whose source_channel doesn't match must NOT
    forward — guards against over-broad relay."""
    try:
        from integrations.channels.registry import ChannelRegistry
        from integrations.channels.bridge.wamp_bridge import (
            ChannelBridge, BridgeConfig, BridgeRule, RouteType)
    except Exception as e:
        pytest.skip(f"channel bridge unavailable: {e}")
    reg = ChannelRegistry()
    src = _recording_adapter('src')
    dst = _recording_adapter('dst')
    reg.register(src)
    reg.register(dst)
    bridge = ChannelBridge(BridgeConfig(rules_file=str(tmp_path / 'rules.json')), reg)
    bridge.add_rule(BridgeRule(id='off', name='disabled', source_channel='src',
                               target_channel='dst', route_type=RouteType.FORWARD,
                               enabled=False))
    bridge.add_rule(BridgeRule(id='other', name='other-source',
                               source_channel='telegram', target_channel='dst',
                               route_type=RouteType.FORWARD))

    _run(bridge._on_channel_message({
        'channel': 'src', 'chat_id': 'cX', 'text': 'nope', 'message_id': 'm2'}))

    assert dst.sent == [], "disabled / non-matching rules must not forward"
