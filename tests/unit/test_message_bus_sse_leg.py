"""Regression guard for the SSE leg of MessageBus.

Bug C in the chat-fail incident: the speculative dispatcher's expert
reply was published only to WAMP (Crossbar :8088).  When Crossbar
refused connection (every cold-start; every locked-down enterprise
install; the user's session today) the expert reply silently vanished
and the user was stranded on the draft's standby.

Root cause was structural — MessageBus had three legs (LOCAL,
PEERLINK, CROSSBAR) but no SSE leg.  Each caller that wanted SSE
fan-out had to ``broadcast_sse_safe`` by hand, and most callers
forgot.  ``_deliver_expert_to_user_async`` was one such forget.

Fix: SSE is now a first-class fourth leg of ``MessageBus.publish``.
Every caller of ``bus.publish`` (and therefore every legacy caller
of ``hart_intelligence.publish_async`` which routes through the bus)
automatically gains a same-machine same-process delivery channel
that does NOT depend on Crossbar being up.

This test fails if any future change:
  - removes the SSE leg from publish()
  - changes the SSE event_type away from the bus topic
  - inverts the skip_sse default (must remain False so existing
    callers gain the leg without opt-in)
  - drops the SSE call when skip_sse=False
  - reverses the trust assumption (SSE must remain unredacted —
    same-machine same-trust as LOCAL)
"""
import sys
from unittest.mock import patch

import pytest

from core.peer_link.message_bus import MessageBus, reset_message_bus


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Each test gets a fresh singleton so stat counters don't leak."""
    reset_message_bus()
    yield
    reset_message_bus()


def test_publish_default_invokes_sse_leg():
    """bus.publish on any topic must call broadcast_sse_safe with topic=event_type."""
    bus = MessageBus()
    captured = []

    def _fake_broadcast(event_type, data, user_id=None):
        captured.append({
            'event_type': event_type,
            'data': data,
            'user_id': user_id,
        })
        return True

    with patch('core.platform.events.broadcast_sse_safe', _fake_broadcast):
        bus.publish('chat.response', {'text': 'hi'}, user_id='user_42')

    assert len(captured) == 1, "SSE leg must fire exactly once per publish"
    assert captured[0]['event_type'] == 'chat.response', (
        "SSE event_type must be the bus topic 1:1 — frontends rely on this "
        "mapping to subscribe by topic")
    assert captured[0]['user_id'] == 'user_42', (
        "SSE delivery must be scoped to the publishing user_id (per-user "
        "fan-out, no cross-user leak)")
    assert captured[0]['data'].get('text') == 'hi', (
        "Payload must reach SSE unredacted (same-machine same-trust)")
    assert bus.get_stats()['delivered_sse'] == 1, (
        "Successful SSE delivery must be counted in stats for observability")


def test_publish_skip_sse_true_omits_leg():
    """skip_sse=True must skip the SSE call without affecting other legs."""
    bus = MessageBus()
    sse_calls = []

    def _fake_broadcast(event_type, data, user_id=None):
        sse_calls.append(event_type)
        return True

    with patch('core.platform.events.broadcast_sse_safe', _fake_broadcast):
        bus.publish(
            'telemetry.node', {'cpu': 0.42},
            user_id='node_a', skip_sse=True)

    assert sse_calls == [], (
        "skip_sse=True must produce zero broadcast_sse_safe calls — used for "
        "pure server-to-server events that must not reach the local UI")
    assert bus.get_stats()['delivered_sse'] == 0


def test_publish_default_skip_sse_is_false():
    """Default behaviour must include SSE — flipping the default would silently
    strand every existing caller exactly the way the original Bug C did."""
    bus = MessageBus()
    sse_called = {'count': 0}

    def _fake_broadcast(event_type, data, user_id=None):
        sse_called['count'] += 1
        return True

    with patch('core.platform.events.broadcast_sse_safe', _fake_broadcast):
        bus.publish('any.topic', {'k': 'v'})  # no skip_sse kwarg

    assert sse_called['count'] == 1, (
        "Default publish() (no skip_sse passed) must hit the SSE leg. If this "
        "ever fails, every chat upgrade and dashboard event will silently "
        "stop reaching the local UI when Crossbar is down — regression of "
        "the original 'I cannot access GitHub URLs / no upgrade ever arrived' "
        "incident.")


def test_sse_failure_does_not_break_publish():
    """SSE delivery is best-effort.  An exception in the SSE broker MUST NOT
    crash publish() or block the LOCAL / PEERLINK / CROSSBAR legs."""
    bus = MessageBus()
    local_received = []
    bus.subscribe('chat.response', lambda topic, data: local_received.append(data))

    def _broken_broadcast(event_type, data, user_id=None):
        raise RuntimeError('SSE broker hard failure')

    with patch('core.platform.events.broadcast_sse_safe', _broken_broadcast):
        # Must not raise
        msg_id = bus.publish('chat.response', {'text': 'hi'}, user_id='u1')

    assert msg_id, "publish() must still return a msg_id when SSE fails"
    assert len(local_received) == 1, (
        "LOCAL leg must still deliver even when SSE raises — failures in "
        "one transport must never cascade to others")
    assert bus.get_stats()['delivered_sse'] == 0, (
        "Stat counter must not increment on failed delivery")


def test_sse_no_broker_returns_false_silent():
    """When broadcast_sse_safe returns False (broker not yet wired — tests,
    pre-Flask boot, headless workers), the bus must NOT increment stats and
    MUST NOT raise."""
    bus = MessageBus()

    def _no_broker(event_type, data, user_id=None):
        return False  # Nunba main not loaded — same as production reality on workers

    with patch('core.platform.events.broadcast_sse_safe', _no_broker):
        bus.publish('chat.response', {'text': 'hi'}, user_id='u1')

    assert bus.get_stats()['delivered_sse'] == 0, (
        "delivered_sse stat must reflect actual deliveries, not attempts. "
        "Otherwise observability would say 'SSE OK' on a node where the "
        "broker is silently unwired — exactly the failure mode the bug was.")


def test_sse_event_module_unimportable_does_not_crash():
    """Worst-case fail-closed: ``core.platform.events`` itself can't be
    imported (broken install, transient pip activity).  publish() must
    still complete the LOCAL / PEERLINK / CROSSBAR legs."""
    bus = MessageBus()
    local_received = []
    bus.subscribe('chat.response', lambda topic, data: local_received.append(data))

    # Simulate ImportError on the events module
    saved = sys.modules.pop('core.platform.events', None)
    sys.modules['core.platform.events'] = None  # forces ImportError on import
    try:
        msg_id = bus.publish('chat.response', {'text': 'hi'}, user_id='u1')
    finally:
        if saved is not None:
            sys.modules['core.platform.events'] = saved
        else:
            sys.modules.pop('core.platform.events', None)

    assert msg_id
    assert len(local_received) == 1, (
        "LOCAL leg must remain unaffected by SSE module import failure")


def test_publish_sse_uses_resolved_user_id_not_data_field():
    """When user_id is passed as kwarg, it must travel to broadcast_sse_safe
    so the SSE broker can per-user fan out.  None when no user_id (broadcast)."""
    bus = MessageBus()
    captured = []

    def _fake_broadcast(event_type, data, user_id=None):
        captured.append(user_id)
        return True

    with patch('core.platform.events.broadcast_sse_safe', _fake_broadcast):
        bus.publish('community.feed', {'post_id': 'p1'})  # no user_id
        bus.publish('chat.response', {'text': 'hi'}, user_id='u1')

    assert captured == [None, 'u1'], (
        "broadcast_sse_safe must receive None when no user_id (community-wide "
        "fan-out) and the explicit user_id when scoped (per-user fan-out). "
        "Empty-string user_id must be normalised to None to avoid an SSE "
        "broker treating '' as a literal user_id key.")
