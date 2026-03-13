"""
Tests for core.platform.registry — ServiceRegistry.

Covers: register, get, unregister, singleton/factory, lifecycle,
dependency ordering, health, thread safety, error handling.
"""

import threading
import time
import unittest

from core.platform.registry import (
    ServiceRegistry, Lifecycle, get_registry, reset_registry,
)


class DummyService:
    """Plain service with no lifecycle."""
    def __init__(self):
        self.value = 42


class CounterService:
    """Tracks how many times it's instantiated."""
    _count = 0

    def __init__(self):
        CounterService._count += 1
        self.instance_num = CounterService._count


class LifecycleService(Lifecycle):
    """Service with full lifecycle hooks."""
    def __init__(self):
        self.started = False
        self.stopped = False
        self._healthy = True

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def health(self):
        if self._healthy:
            return {'status': 'ok', 'detail': 'all good'}
        return {'status': 'degraded'}


class FailingFactory:
    """Factory that raises on instantiation."""
    def __init__(self):
        raise RuntimeError("factory boom")


class FailingStartService(Lifecycle):
    """Service whose start() raises."""
    def start(self):
        raise RuntimeError("start boom")


class TestServiceRegistryBasic(unittest.TestCase):
    """Basic register/get/has/unregister."""

    def setUp(self):
        self.reg = ServiceRegistry()

    def test_register_and_get(self):
        self.reg.register('dummy', DummyService)
        svc = self.reg.get('dummy')
        self.assertIsInstance(svc, DummyService)
        self.assertEqual(svc.value, 42)

    def test_has(self):
        self.assertFalse(self.reg.has('dummy'))
        self.reg.register('dummy', DummyService)
        self.assertTrue(self.reg.has('dummy'))

    def test_names(self):
        self.reg.register('a', DummyService)
        self.reg.register('b', DummyService)
        self.assertEqual(sorted(self.reg.names()), ['a', 'b'])

    def test_get_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.reg.get('nonexistent')

    def test_duplicate_register_raises(self):
        self.reg.register('x', DummyService)
        with self.assertRaises(ValueError):
            self.reg.register('x', DummyService)

    def test_unregister(self):
        self.reg.register('dummy', DummyService)
        self.reg.unregister('dummy')
        self.assertFalse(self.reg.has('dummy'))

    def test_unregister_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.reg.unregister('ghost')

    def test_reset_clears_all(self):
        self.reg.register('a', DummyService)
        self.reg.register('b', DummyService)
        self.reg.reset()
        self.assertEqual(self.reg.names(), [])


class TestSingletonBehavior(unittest.TestCase):
    """Singleton vs factory mode."""

    def setUp(self):
        self.reg = ServiceRegistry()
        CounterService._count = 0

    def test_singleton_returns_same_instance(self):
        self.reg.register('svc', DummyService, singleton=True)
        a = self.reg.get('svc')
        b = self.reg.get('svc')
        self.assertIs(a, b)

    def test_factory_returns_new_instance(self):
        self.reg.register('svc', CounterService, singleton=False)
        a = self.reg.get('svc')
        b = self.reg.get('svc')
        self.assertIsNot(a, b)
        self.assertEqual(a.instance_num, 1)
        self.assertEqual(b.instance_num, 2)

    def test_singleton_is_default(self):
        self.reg.register('svc', CounterService)
        self.reg.get('svc')
        self.reg.get('svc')
        self.assertEqual(CounterService._count, 1)


class TestLifecycle(unittest.TestCase):
    """start_all, stop_all, health."""

    def setUp(self):
        self.reg = ServiceRegistry()

    def test_start_all_calls_start(self):
        self.reg.register('svc', LifecycleService)
        self.reg.start_all()
        svc = self.reg.get('svc')
        self.assertTrue(svc.started)

    def test_stop_all_calls_stop(self):
        self.reg.register('svc', LifecycleService)
        self.reg.start_all()
        self.reg.stop_all()
        svc = self.reg.get('svc')
        self.assertTrue(svc.stopped)

    def test_stop_reverse_order(self):
        order = []

        class A(Lifecycle):
            def stop(self_inner):
                order.append('a')

        class B(Lifecycle):
            def stop(self_inner):
                order.append('b')

        class C(Lifecycle):
            def stop(self_inner):
                order.append('c')

        self.reg.register('a', A)
        self.reg.register('b', B)
        self.reg.register('c', C)
        self.reg.start_all()
        self.reg.stop_all()
        self.assertEqual(order, ['c', 'b', 'a'])

    def test_plain_service_not_started(self):
        self.reg.register('plain', DummyService)
        self.reg.start_all()
        # Should not raise — plain services are just marked started

    def test_health_reports_all(self):
        self.reg.register('lifecycle', LifecycleService)
        self.reg.register('plain', DummyService)
        self.reg.start_all()
        h = self.reg.health()
        self.assertIn('lifecycle', h)
        self.assertEqual(h['lifecycle']['status'], 'ok')
        self.assertIn('uptime_seconds', h['lifecycle'])
        self.assertIn('plain', h)
        self.assertEqual(h['plain']['status'], 'running')

    def test_health_not_instantiated(self):
        self.reg.register('lazy', DummyService)
        h = self.reg.health()
        self.assertEqual(h['lazy']['status'], 'not_instantiated')

    def test_unregister_stops_service(self):
        self.reg.register('svc', LifecycleService)
        self.reg.start_all()
        svc = self.reg.get('svc')
        self.reg.unregister('svc')
        self.assertTrue(svc.stopped)


class TestDependencyOrdering(unittest.TestCase):
    """depends_on and topological sort."""

    def setUp(self):
        self.reg = ServiceRegistry()

    def test_dependency_order(self):
        order = []

        def make_tracker(label):
            class Tracker(Lifecycle):
                def start(self_inner):
                    order.append(label)
            return Tracker

        self.reg.register('c', make_tracker('c'), depends_on=['b'])
        self.reg.register('b', make_tracker('b'), depends_on=['a'])
        self.reg.register('a', make_tracker('a'))
        self.reg.start_all()
        self.assertEqual(order, ['a', 'b', 'c'])

    def test_circular_dependency_raises(self):
        self.reg.register('a', DummyService, depends_on=['b'])
        self.reg.register('b', DummyService, depends_on=['a'])
        with self.assertRaises(ValueError) as ctx:
            self.reg.start_all()
        self.assertIn('Circular', str(ctx.exception))

    def test_missing_dependency_ignored(self):
        # depends_on a service not registered — just skip that dep
        self.reg.register('a', DummyService, depends_on=['missing'])
        self.reg.start_all()  # should not raise


class TestErrorHandling(unittest.TestCase):
    """Factory failures, start failures."""

    def setUp(self):
        self.reg = ServiceRegistry()

    def test_failing_factory(self):
        self.reg.register('bad', FailingFactory)
        with self.assertRaises(RuntimeError) as ctx:
            self.reg.get('bad')
        self.assertIn('factory boom', str(ctx.exception))

    def test_failing_start_records_error(self):
        self.reg.register('bad', FailingStartService)
        self.reg.start_all()
        h = self.reg.health()
        self.assertEqual(h['bad']['status'], 'error')
        self.assertIn('start boom', h['bad']['error'])

    def test_factory_function(self):
        """Factory can be a plain function, not just a class."""
        def make_service():
            return {'type': 'dict_service', 'value': 99}

        self.reg.register('func_svc', make_service)
        svc = self.reg.get('func_svc')
        self.assertEqual(svc['value'], 99)

    def test_factory_lambda(self):
        """Factory can be a lambda."""
        self.reg.register('lambda_svc', lambda: [1, 2, 3])
        svc = self.reg.get('lambda_svc')
        self.assertEqual(svc, [1, 2, 3])


class TestThreadSafety(unittest.TestCase):
    """Concurrent access."""

    def test_concurrent_get(self):
        reg = ServiceRegistry()
        CounterService._count = 0
        reg.register('svc', CounterService)

        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            svc = reg.get('svc')
            results.append(svc.instance_num)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same singleton instance
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(CounterService._count, 1)


class TestGlobalRegistry(unittest.TestCase):
    """get_registry() / reset_registry() global singleton."""

    def setUp(self):
        reset_registry()

    def tearDown(self):
        reset_registry()

    def test_get_registry_returns_same(self):
        a = get_registry()
        b = get_registry()
        self.assertIs(a, b)

    def test_reset_creates_new(self):
        a = get_registry()
        a.register('test', DummyService)
        reset_registry()
        b = get_registry()
        self.assertIsNot(a, b)
        self.assertFalse(b.has('test'))


if __name__ == '__main__':
    unittest.main()
