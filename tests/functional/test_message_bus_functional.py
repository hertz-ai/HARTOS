"""
Functional tests for MessageBus — real pub/sub, real dedup, real routing.

These tests exercise the ACTUAL MessageBus class with real instances.
No mocking of MessageBus internals. External transports (PeerLink, Crossbar)
are skipped via publish() flags so tests run without infrastructure.
"""
import threading
import time

import pytest

from core.peer_link.message_bus import MessageBus


def _make_bus() -> MessageBus:
    """Create a fresh MessageBus instance for each test."""
    return MessageBus()


# ─── 1. Basic publish/subscribe ────────────────────────


def test_publish_subscribe_basic():
    """Subscribe to 'test.topic', publish data, verify handler called with correct data."""
    bus = _make_bus()
    received = []

    def handler(topic, data):
        received.append((topic, data))

    bus.subscribe('test.topic', handler)
    bus.publish('test.topic', {'key': 'value'}, skip_peerlink=True, skip_crossbar=True)

    assert len(received) == 1
    assert received[0][0] == 'test.topic'
    assert received[0][1]['key'] == 'value'


# ─── 2. Wildcard subscribe ─────────────────────────────


def test_wildcard_subscribe():
    """Subscribe to 'chat.*'. Publish 'chat.response' and 'chat.action'. Verify both delivered."""
    bus = _make_bus()
    received = []

    def handler(topic, data):
        received.append(topic)

    bus.subscribe('chat.*', handler)
    bus.publish('chat.response', {'msg': 'r1'}, skip_peerlink=True, skip_crossbar=True)
    bus.publish('chat.action', {'msg': 'a1'}, skip_peerlink=True, skip_crossbar=True)

    assert len(received) == 2
    assert 'chat.response' in received
    assert 'chat.action' in received


# ─── 3. Wildcard no false match ────────────────────────


def test_wildcard_no_false_match():
    """Subscribe to 'chat.*'. Publish 'user.message'. Verify NOT delivered."""
    bus = _make_bus()
    received = []

    def handler(topic, data):
        received.append(topic)

    bus.subscribe('chat.*', handler)
    bus.publish('user.message', {'msg': 'nope'}, skip_peerlink=True, skip_crossbar=True)

    assert len(received) == 0


# ─── 4. Unsubscribe ────────────────────────────────────


def test_unsubscribe():
    """Subscribe handler, verify delivery. Unsubscribe. Publish again. Verify NOT delivered."""
    bus = _make_bus()
    received = []

    def handler(topic, data):
        received.append(topic)

    bus.subscribe('events.update', handler)
    bus.publish('events.update', {'n': 1}, skip_peerlink=True, skip_crossbar=True)
    assert len(received) == 1

    bus.unsubscribe('events.update', handler)
    bus.publish('events.update', {'n': 2}, skip_peerlink=True, skip_crossbar=True)
    assert len(received) == 1  # Still 1 — handler was NOT called again


# ─── 5. Multiple subscribers ───────────────────────────


def test_multiple_subscribers():
    """3 handlers on same topic. Publish once. Verify all 3 called."""
    bus = _make_bus()
    calls = {'h1': 0, 'h2': 0, 'h3': 0}

    def h1(topic, data):
        calls['h1'] += 1

    def h2(topic, data):
        calls['h2'] += 1

    def h3(topic, data):
        calls['h3'] += 1

    bus.subscribe('multi.topic', h1)
    bus.subscribe('multi.topic', h2)
    bus.subscribe('multi.topic', h3)

    bus.publish('multi.topic', {'x': 1}, skip_peerlink=True, skip_crossbar=True)

    assert calls['h1'] == 1
    assert calls['h2'] == 1
    assert calls['h3'] == 1


# ─── 6. Publish returns message ID ─────────────────────


def test_publish_returns_msg_id():
    """Verify publish() returns a non-empty string message ID."""
    bus = _make_bus()
    msg_id = bus.publish('id.check', {'data': True}, skip_peerlink=True, skip_crossbar=True)

    assert isinstance(msg_id, str)
    assert len(msg_id) > 0


# ─── 7. Stats tracking ─────────────────────────────────


def test_stats_tracking():
    """Publish 5 messages. Verify stats['published'] == 5."""
    bus = _make_bus()

    for i in range(5):
        bus.publish(f'stats.topic.{i}', {'i': i}, skip_peerlink=True, skip_crossbar=True)

    stats = bus.get_stats()
    assert stats['published'] == 5


# ─── 8. Thread safety ──────────────────────────────────


def test_thread_safety():
    """Spawn 10 threads, each publishing 100 messages. Verify all 1000 delivered without crash."""
    bus = _make_bus()
    delivery_count = {'value': 0}
    count_lock = threading.Lock()

    def handler(topic, data):
        with count_lock:
            delivery_count['value'] += 1

    bus.subscribe('stress.*', handler)

    errors = []
    barrier = threading.Barrier(10)

    def publisher(thread_id):
        try:
            barrier.wait(timeout=5)
            for i in range(100):
                bus.publish(
                    f'stress.t{thread_id}',
                    {'thread': thread_id, 'seq': i},
                    skip_peerlink=True,
                    skip_crossbar=True,
                )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=publisher, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(errors) == 0, f"Thread errors: {errors}"
    assert delivery_count['value'] == 1000

    stats = bus.get_stats()
    assert stats['published'] == 1000
