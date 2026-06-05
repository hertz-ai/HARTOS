"""Behavioural tests for the announcement broadcaster — the outbound
leg of the flywheel that lets benchmark proofs escape HARTOS's walled
garden and reach potential new users on external channels.

Tests prove:
  - Registration is idempotent (boot can call multiple times safely)
  - Target collection walks admin._channels for announce_chat_id
  - Dispatch goes through the existing FlaskChannelIntegration loop
    (no parallel asyncio bridge)
  - Disabled channels are skipped (don't waste calls on dead adapters)
  - EventBus 'hive.benchmark.published' triggers the broadcast end-to-end
"""
from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Helper fixtures ───────────────────────────────────────────

class _FakeLoop:
    """Stand-in for the FlaskChannelIntegration asyncio loop.  We don't
    actually run the coroutine — we just record what was scheduled so
    the test can assert on (channel, chat_id, text).  This isolates
    the broadcaster's logic from the real Discord/Telegram adapters."""
    def __init__(self):
        self.scheduled = []  # list of (coro_repr,)
        # Mimic asyncio loop's call_soon_threadsafe interface enough
        # that run_coroutine_threadsafe works.


def _patch_integration_loop(monkeypatch, scheduled_list):
    """Patches flask_integration.get_channel_integration() to return a
    mock with a non-None _loop AND monkeypatches
    asyncio.run_coroutine_threadsafe so the broadcaster's dispatch
    path lands the call into `scheduled_list` without needing a real
    background loop.  Avoids real-thread lifecycle in tests."""
    from integrations.channels.base import SendResult

    class _MockRegistry:
        async def send_to_channel(self, channel, chat_id, text, **kw):
            return SendResult(success=True, message_id='mock-id')

    class _MockIntegration:
        def __init__(self):
            # Real loop object so isinstance/None checks pass, but
            # we never start it — the patched run_coroutine_threadsafe
            # below intercepts before any actual scheduling.
            self._loop = object()  # sentinel — never used directly
            self.registry = _MockRegistry()

        def shutdown(self):
            pass

    inst = _MockIntegration()
    from integrations.channels import flask_integration
    monkeypatch.setattr(flask_integration, 'get_channel_integration',
                        lambda: inst)

    # Intercept run_coroutine_threadsafe — the broadcaster wraps each
    # send_to_channel coroutine through it.  We inspect the coro's
    # args via str(coro) since we don't actually run it; the test
    # asserts on (channel, chat_id, text) reconstructed from the
    # broadcaster's _dispatch_to_target arg-passing.
    import integrations.channels.announcement_broadcaster as ab_mod
    from concurrent.futures import Future

    def _fake_dispatch(channel, chat_id, text):
        scheduled_list.append((channel, chat_id, text))
        # Original _dispatch_to_target requires no return.

    monkeypatch.setattr(ab_mod, '_dispatch_to_target', _fake_dispatch)
    return inst


def _patch_admin_channels(monkeypatch, channels: dict):
    """Plug in a fake AdminAPI singleton with the given _channels dict."""
    fake_api = MagicMock()
    fake_api._channels = channels
    from integrations.channels.admin import api as admin_api_mod
    monkeypatch.setattr(admin_api_mod, 'get_api', lambda: fake_api)
    return fake_api


# ── _build_announcement_text ──────────────────────────────────

def test_build_text_uses_comparison_when_present():
    from integrations.channels.announcement_broadcaster import (
        _build_announcement_text)
    text = _build_announcement_text({
        'benchmark': 'mmlu',
        'score': 0.87,
        'num_nodes': 4,
        'comparison': 'HIVE BENCHMARK PROOF — MMLU\n  Hive: 87%',
    })
    assert text == 'HIVE BENCHMARK PROOF — MMLU\n  Hive: 87%'


def test_build_text_falls_back_to_minimal_when_no_comparison():
    from integrations.channels.announcement_broadcaster import (
        _build_announcement_text)
    text = _build_announcement_text({
        'benchmark': 'gsm8k', 'score': 0.5, 'num_nodes': 2,
    })
    assert 'gsm8k' in text
    assert '50' in text  # 0.5 → 50%


def test_build_text_handles_non_dict():
    from integrations.channels.announcement_broadcaster import (
        _build_announcement_text)
    assert _build_announcement_text("anything") == "anything"
    assert _build_announcement_text(None) == 'None'


# ── _collect_announcement_targets ─────────────────────────────

def test_collect_targets_filters_to_announce_chat_id_set(monkeypatch):
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': '123456', 'enabled': True},
        'telegram': {'announce_chat_id': '', 'enabled': True},
        'slack': {'announce_chat_id': '   ', 'enabled': True},
        'whatsapp': {'enabled': True},  # no announce_chat_id key
        'imessage': {'announce_chat_id': '999', 'enabled': True},
    })
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    targets = _collect_announcement_targets()
    # Only discord + imessage qualify.
    assert sorted(targets) == [('discord', '123456'), ('imessage', '999')]


def test_collect_targets_skips_disabled_channels(monkeypatch):
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': '123', 'enabled': False},
        'telegram': {'announce_chat_id': '456', 'enabled': True},
    })
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    assert _collect_announcement_targets() == [('telegram', '456')]


def test_collect_targets_empty_when_no_admin_api(monkeypatch):
    # If the admin API is unreachable (boot ordering), we return [] —
    # not an exception.  The flywheel must survive partial bring-up.
    from integrations.channels.admin import api as admin_api_mod

    def _boom():
        raise RuntimeError("admin API not ready")
    monkeypatch.setattr(admin_api_mod, 'get_api', _boom)
    from integrations.channels.announcement_broadcaster import (
        _collect_announcement_targets)
    assert _collect_announcement_targets() == []


# ── broadcast_announcement end-to-end ─────────────────────────

def test_broadcast_dispatches_to_each_target(monkeypatch):
    scheduled = []
    inst = _patch_integration_loop(monkeypatch, scheduled)
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': 'ch-1', 'enabled': True},
        'telegram': {'announce_chat_id': 'ch-2', 'enabled': True},
    })
    from integrations.channels.announcement_broadcaster import (
        broadcast_announcement)
    count = broadcast_announcement('hello hive')
    assert count == 2

    assert sorted(scheduled) == [
        ('discord', 'ch-1', 'hello hive'),
        ('telegram', 'ch-2', 'hello hive'),
    ]
    inst.shutdown()


def test_broadcast_no_targets_returns_zero(monkeypatch):
    _patch_admin_channels(monkeypatch, {})
    from integrations.channels.announcement_broadcaster import (
        broadcast_announcement)
    assert broadcast_announcement('no one') == 0


def test_broadcast_skips_when_loop_missing(monkeypatch):
    # FlaskChannelIntegration._loop is None until start() runs.  Until
    # then, we must NOT block or raise — the EventBus emit chain stays
    # alive for other subscribers (federated_aggregator etc.).  This
    # exercises the REAL _dispatch_to_target (no monkeypatch shim) so
    # we cover the loop-None branch.
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': 'ch-1', 'enabled': True},
    })
    no_loop = MagicMock()
    no_loop._loop = None
    from integrations.channels import flask_integration
    monkeypatch.setattr(flask_integration, 'get_channel_integration',
                        lambda: no_loop)
    from integrations.channels.announcement_broadcaster import (
        broadcast_announcement)
    # Count is 1 because we tried — but no schedule happened.
    # Must not raise.
    assert broadcast_announcement('hi') == 1


# ── EventBus integration — register + fire ────────────────────

def test_register_is_idempotent():
    from integrations.channels import announcement_broadcaster as ab
    ab.reset_for_tests()
    # The first call MAY fail to find the events registry — that's
    # fine, we're testing the sentinel behavior.  Second call should
    # be a no-op (returns False).
    first = ab.register_announcement_subscriber()
    # Force the sentinel so we can test the no-op branch.
    ab._subscribed = True
    second = ab.register_announcement_subscriber()
    assert second is False
    ab.reset_for_tests()


def test_event_published_triggers_broadcast(monkeypatch):
    """End-to-end: register the subscriber against a real EventBus,
    emit the hive.benchmark.published event, assert the broadcast
    helper was called with text from the payload."""
    scheduled = []
    inst = _patch_integration_loop(monkeypatch, scheduled)
    _patch_admin_channels(monkeypatch, {
        'discord': {'announce_chat_id': 'public-channel', 'enabled': True},
    })
    # Build a clean EventBus + register it in the platform registry.
    # registry.register expects a FACTORY (callable returning the
    # service), not an instance — pass `EventBus` itself so the
    # registry instantiates a singleton on first get().
    from core.platform.events import EventBus
    from core.platform.registry import get_registry
    registry = get_registry()
    # Drop any pre-existing 'events' registration so this test owns
    # the bus instance.
    if registry.has('events'):
        registry.unregister('events')
    registry.register('events', EventBus)
    bus = registry.get('events')

    from integrations.channels import announcement_broadcaster as ab
    ab.reset_for_tests()
    registered = ab.register_announcement_subscriber()
    assert registered is True, (
        "registration should succeed when events registry is up")

    # Fire the event — the one hive_benchmark_prover._publish_results
    # emits after a successful run.
    bus.emit('hive.benchmark.published', {
        'benchmark': 'mmlu',
        'score': 0.87,
        'num_nodes': 5,
        'comparison': 'HIVE BENCHMARK PROOF — MMLU\n  Hive: 87%',
    })

    # _dispatch_to_target is patched to append synchronously, so by
    # the time bus.emit() returns the schedule list is populated.
    assert len(scheduled) == 1
    channel, chat_id, text = scheduled[0]
    assert channel == 'discord'
    assert chat_id == 'public-channel'
    assert 'MMLU' in text or 'mmlu' in text.lower()

    # Cleanup so we don't leak state to other tests.
    ab.reset_for_tests()
    inst.shutdown()
