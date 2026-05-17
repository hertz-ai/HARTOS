"""Tests for RSI-4: realtime usage-stat trigger.

Contract:
    1. UsageSignalTracker counts signals in a rolling window, enforces
       threshold + cooldown, prunes old timestamps, and caps the key
       count to prevent unbounded memory growth.
    2. RealtimeRSITrigger.on_signal records the signal unconditionally
       (so dashboards can see traffic before any gates trip) and only
       fires the injected enqueue callback when HEVOLVE_RSI_REALTIME=1,
       the threshold is crossed, and the cooldown has elapsed.
    3. Feature flag OFF is inert for the enqueue leg — no autoresearch
       iterations fire, no threads are spawned.
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


def _clock():
    """Deterministic clock for window-timing tests."""
    state = {'now': 1000.0}

    def get():
        return state['now']

    def advance(dt):
        state['now'] += dt

    return state, get, advance


# ── UsageSignalTracker ─────────────────────────────────────────


class TestTrackerCounting(unittest.TestCase):
    def test_records_and_counts(self):
        from integrations.agent_engine.rsi_trigger import UsageSignalTracker
        _, clock, advance = _clock()
        tr = UsageSignalTracker(window_s=60, threshold=3, cooldown_s=0,
                                now_fn=clock)
        tr.record('tool.error', 'web_search')
        advance(10)
        tr.record('tool.error', 'web_search')
        snap = tr.get_snapshot()
        self.assertEqual(snap['tool.error:web_search']['count_in_window'], 2)

    def test_threshold_false_below(self):
        from integrations.agent_engine.rsi_trigger import UsageSignalTracker
        _, clock, _ = _clock()
        tr = UsageSignalTracker(window_s=60, threshold=3, cooldown_s=0,
                                now_fn=clock)
        tr.record('tool.error', 'k')
        tr.record('tool.error', 'k')
        self.assertFalse(tr.should_trigger('tool.error', 'k'))

    def test_threshold_true_at_crossing(self):
        from integrations.agent_engine.rsi_trigger import UsageSignalTracker
        _, clock, _ = _clock()
        tr = UsageSignalTracker(window_s=60, threshold=3, cooldown_s=0,
                                now_fn=clock)
        for _ in range(3):
            tr.record('tool.error', 'k')
        self.assertTrue(tr.should_trigger('tool.error', 'k'))

    def test_prunes_outside_window(self):
        from integrations.agent_engine.rsi_trigger import UsageSignalTracker
        _, clock, advance = _clock()
        tr = UsageSignalTracker(window_s=60, threshold=3, cooldown_s=0,
                                now_fn=clock)
        for _ in range(3):
            tr.record('tool.error', 'k')
        advance(120)  # all three fall out of the 60s window
        self.assertFalse(tr.should_trigger('tool.error', 'k'))
        snap = tr.get_snapshot()
        self.assertEqual(snap['tool.error:k']['count_in_window'], 0)

    def test_cooldown_blocks_retrigger(self):
        from integrations.agent_engine.rsi_trigger import UsageSignalTracker
        _, clock, advance = _clock()
        tr = UsageSignalTracker(window_s=60, threshold=2, cooldown_s=30,
                                now_fn=clock)
        tr.record('tool.error', 'k')
        tr.record('tool.error', 'k')
        self.assertTrue(tr.should_trigger('tool.error', 'k'))
        tr.mark_triggered('tool.error', 'k')
        # Still over threshold but cooldown blocks.
        self.assertFalse(tr.should_trigger('tool.error', 'k'))
        advance(31)
        self.assertTrue(tr.should_trigger('tool.error', 'k'))

    def test_key_cap_enforced(self):
        from integrations.agent_engine.rsi_trigger import UsageSignalTracker
        _, clock, _ = _clock()
        tr = UsageSignalTracker(window_s=60, threshold=10, cooldown_s=0,
                                max_keys=5, now_fn=clock)
        for i in range(20):
            tr.record('tool.error', f'k{i}')
        snap = tr.get_snapshot()
        self.assertLessEqual(len(snap), 5)

    def test_empty_inputs_are_ignored(self):
        from integrations.agent_engine.rsi_trigger import UsageSignalTracker
        tr = UsageSignalTracker()
        tr.record('', 'k')
        tr.record('tool.error', '')
        self.assertEqual(tr.get_snapshot(), {})


# ── RealtimeRSITrigger glue ────────────────────────────────────


class TestTriggerGlue(unittest.TestCase):
    def setUp(self):
        os.environ.pop('HEVOLVE_RSI_REALTIME', None)

    def tearDown(self):
        os.environ.pop('HEVOLVE_RSI_REALTIME', None)

    def test_flag_off_never_enqueues(self):
        from integrations.agent_engine.rsi_trigger import (
            RealtimeRSITrigger, UsageSignalTracker,
        )
        _, clock, _ = _clock()
        tracker = UsageSignalTracker(window_s=60, threshold=2, cooldown_s=0,
                                     now_fn=clock)
        calls = []
        trig = RealtimeRSITrigger(
            tracker=tracker,
            enqueue_fn=lambda s, k: calls.append((s, k)),
        )
        for _ in range(5):
            trig.on_signal('tool.error', {'key': 'web_search'})
        self.assertEqual(calls, [], 'flag off must not fire enqueue')
        # But the tracker still records for dashboards.
        snap = tracker.get_snapshot()
        self.assertEqual(snap['tool.error:web_search']['count_in_window'], 5)

    def test_flag_on_fires_enqueue_after_threshold(self):
        from integrations.agent_engine.rsi_trigger import (
            RealtimeRSITrigger, UsageSignalTracker,
        )
        os.environ['HEVOLVE_RSI_REALTIME'] = '1'
        _, clock, _ = _clock()
        tracker = UsageSignalTracker(window_s=60, threshold=2, cooldown_s=0,
                                     now_fn=clock)
        done = threading.Event()
        calls = []

        def _enqueue(s, k):
            calls.append((s, k))
            done.set()

        trig = RealtimeRSITrigger(tracker=tracker, enqueue_fn=_enqueue)
        trig.on_signal('tool.error', {'key': 'web_search'})
        trig.on_signal('tool.error', {'key': 'web_search'})  # threshold crossed
        self.assertTrue(done.wait(2.0), 'enqueue must run on a daemon thread')
        self.assertEqual(calls, [('tool.error', 'web_search')])

    def test_enqueue_error_is_swallowed(self):
        from integrations.agent_engine.rsi_trigger import (
            RealtimeRSITrigger, UsageSignalTracker,
        )
        os.environ['HEVOLVE_RSI_REALTIME'] = '1'
        _, clock, _ = _clock()
        tracker = UsageSignalTracker(window_s=60, threshold=1, cooldown_s=0,
                                     now_fn=clock)
        fired = threading.Event()

        def _boom(s, k):
            fired.set()
            raise RuntimeError('downstream broke')

        trig = RealtimeRSITrigger(tracker=tracker, enqueue_fn=_boom)
        # Should not raise even though enqueue raised.
        trig.on_signal('tool.error', {'key': 'k'})
        self.assertTrue(fired.wait(2.0))

    def test_extracts_key_from_tool_field(self):
        from integrations.agent_engine.rsi_trigger import (
            RealtimeRSITrigger, UsageSignalTracker,
        )
        tracker = UsageSignalTracker()
        trig = RealtimeRSITrigger(tracker=tracker, enqueue_fn=lambda *a: None)
        trig.on_signal('tool.error', {'tool': 'codec_search'})
        snap = tracker.get_snapshot()
        self.assertIn('tool.error:codec_search', snap)

    def test_bind_to_bus_noop_if_registry_missing(self):
        from integrations.agent_engine.rsi_trigger import RealtimeRSITrigger
        trig = RealtimeRSITrigger()
        with patch(
            'integrations.agent_engine.rsi_trigger._get_event_bus',
            return_value=None,
        ):
            bound = trig.bind_to_bus()
        self.assertFalse(bound)


if __name__ == '__main__':
    unittest.main()
