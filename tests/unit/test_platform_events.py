"""
Tests for core.platform.events — EventBus.

Covers: on/off, emit, wildcard matching, once, async emit,
error handling, has_listeners, topics, health, thread safety.
"""

import threading
import time
import unittest

try:
    from core.platform.events import EventBus
except ImportError:
    import sys
    if 'pytest' in sys.modules:
        import pytest
        pytest.skip("core.platform.events not available", allow_module_level=True)
    raise


class TestEventBusBasic(unittest.TestCase):
    """Basic on/off/emit."""

    def setUp(self):
        self.bus = EventBus()

    def test_emit_calls_listener(self):
        events = []
        self.bus.on('test.event', lambda t, d: events.append((t, d)))
        self.bus.emit('test.event', {'value': 42})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ('test.event', {'value': 42}))

    def test_emit_returns_listener_count(self):
        self.bus.on('x', lambda t, d: None)
        self.bus.on('x', lambda t, d: None)
        count = self.bus.emit('x')
        self.assertEqual(count, 2)

    def test_emit_no_listeners_returns_zero(self):
        count = self.bus.emit('nobody.listening')
        self.assertEqual(count, 0)

    def test_emit_none_data(self):
        events = []
        self.bus.on('x', lambda t, d: events.append(d))
        self.bus.emit('x')
        self.assertEqual(events, [None])

    def test_off_removes_listener(self):
        events = []
        cb = lambda t, d: events.append(1)
        self.bus.on('x', cb)
        self.bus.emit('x')
        self.assertEqual(len(events), 1)

        self.bus.off('x', cb)
        self.bus.emit('x')
        self.assertEqual(len(events), 1)  # no new event

    def test_off_nonexistent_no_error(self):
        self.bus.off('ghost', lambda t, d: None)  # should not raise

    def test_multiple_topics(self):
        a_events = []
        b_events = []
        self.bus.on('a', lambda t, d: a_events.append(1))
        self.bus.on('b', lambda t, d: b_events.append(1))
        self.bus.emit('a')
        self.assertEqual(len(a_events), 1)
        self.assertEqual(len(b_events), 0)

    def test_multiple_listeners_same_topic(self):
        events = []
        self.bus.on('x', lambda t, d: events.append('a'))
        self.bus.on('x', lambda t, d: events.append('b'))
        self.bus.emit('x')
        self.assertEqual(events, ['a', 'b'])


class TestWildcardSubscriptions(unittest.TestCase):
    """Wildcard pattern matching."""

    def setUp(self):
        self.bus = EventBus()

    def test_star_matches_all(self):
        events = []
        self.bus.on('*', lambda t, d: events.append(t))
        self.bus.emit('anything')
        self.bus.emit('something.else')
        self.assertEqual(events, ['anything', 'something.else'])

    def test_prefix_wildcard(self):
        events = []
        self.bus.on('config.*', lambda t, d: events.append(t))
        self.bus.emit('config.display.scale')
        self.bus.emit('theme.changed')
        self.assertEqual(events, ['config.display.scale'])

    def test_wildcard_and_exact(self):
        """Both wildcard and exact listeners fire."""
        events = []
        self.bus.on('app.installed', lambda t, d: events.append('exact'))
        self.bus.on('app.*', lambda t, d: events.append('wild'))
        self.bus.emit('app.installed')
        self.assertEqual(sorted(events), ['exact', 'wild'])

    def test_off_wildcard(self):
        events = []
        cb = lambda t, d: events.append(1)
        self.bus.on('theme.*', cb)
        self.bus.emit('theme.changed')
        self.assertEqual(len(events), 1)

        self.bus.off('theme.*', cb)
        self.bus.emit('theme.changed')
        self.assertEqual(len(events), 1)


class TestOnce(unittest.TestCase):
    """One-shot subscriptions."""

    def setUp(self):
        self.bus = EventBus()

    def test_once_fires_once(self):
        events = []
        self.bus.once('x', lambda t, d: events.append(1))
        self.bus.emit('x')
        self.bus.emit('x')
        self.assertEqual(len(events), 1)

    def test_once_receives_data(self):
        events = []
        self.bus.once('x', lambda t, d: events.append(d))
        self.bus.emit('x', 'hello')
        self.assertEqual(events, ['hello'])


class TestErrorHandling(unittest.TestCase):
    """Listener errors don't break other listeners."""

    def setUp(self):
        self.bus = EventBus()

    def test_error_in_listener_doesnt_break_others(self):
        good_events = []

        def bad(t, d):
            raise RuntimeError("boom")

        self.bus.on('x', bad)
        self.bus.on('x', lambda t, d: good_events.append(1))
        count = self.bus.emit('x')
        # bad listener counted even though it errored; good one still fires
        self.assertEqual(len(good_events), 1)

    def test_error_in_wildcard_doesnt_break_others(self):
        good_events = []

        def bad(t, d):
            raise RuntimeError("boom")

        self.bus.on('x.*', bad)
        self.bus.on('x.*', lambda t, d: good_events.append(1))
        self.bus.emit('x.y')
        self.assertEqual(len(good_events), 1)


class TestAsyncEmit(unittest.TestCase):
    """Fire-and-forget emission."""

    def test_async_emit_fires(self):
        events = []
        barrier = threading.Event()

        def listener(t, d):
            events.append(t)
            barrier.set()

        bus = EventBus()
        bus.on('async.test', listener)
        bus.emit_async('async.test', {'val': 1})
        barrier.wait(timeout=2)
        self.assertEqual(events, ['async.test'])


class TestIntrospection(unittest.TestCase):
    """has_listeners, topics, health, emit_count."""

    def setUp(self):
        self.bus = EventBus()

    def test_has_listeners_exact(self):
        self.assertFalse(self.bus.has_listeners('x'))
        self.bus.on('x', lambda t, d: None)
        self.assertTrue(self.bus.has_listeners('x'))

    def test_has_listeners_wildcard(self):
        self.bus.on('app.*', lambda t, d: None)
        self.assertTrue(self.bus.has_listeners('app.installed'))

    def test_topics(self):
        self.bus.on('a', lambda t, d: None)
        self.bus.on('b.*', lambda t, d: None)
        topics = self.bus.topics()
        self.assertIn('a', topics)
        self.assertIn('b.*', topics)

    def test_emit_count(self):
        self.assertEqual(self.bus.emit_count, 0)
        self.bus.emit('x')
        self.bus.emit('y')
        self.assertEqual(self.bus.emit_count, 2)

    def test_health(self):
        self.bus.on('a', lambda t, d: None)
        self.bus.on('b.*', lambda t, d: None)
        self.bus.emit('a')
        h = self.bus.health()
        self.assertEqual(h['status'], 'ok')
        self.assertEqual(h['listeners'], 2)
        self.assertEqual(h['topics'], 2)
        self.assertEqual(h['total_emits'], 1)

    def test_clear(self):
        self.bus.on('a', lambda t, d: None)
        self.bus.on('b.*', lambda t, d: None)
        self.bus.clear()
        self.assertEqual(self.bus.topics(), [])


class TestThreadSafety(unittest.TestCase):
    """Concurrent subscribe/emit."""

    def test_concurrent_emit_and_subscribe(self):
        bus = EventBus()
        events = []
        errors = []

        def subscriber():
            for i in range(50):
                bus.on(f'topic.{i}', lambda t, d: events.append(1))

        def emitter():
            for i in range(50):
                try:
                    bus.emit(f'topic.{i}')
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=subscriber),
            threading.Thread(target=emitter),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)


if __name__ == '__main__':
    unittest.main()
