"""
Thread-safety tests for singleton patterns and executor lifecycle.

Validates:
- get_engine() returns same engine from concurrent threads (double-checked locking)
- get_session_factory() returns same factory from concurrent threads
- get_executor() returns same executor from concurrent threads
- ThreadPoolExecutor atexit handlers are registered
"""
import atexit
import os
import threading
from unittest.mock import patch, MagicMock

import pytest

# Force in-memory DB for tests
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')


# ─── models.py thread-safe singletons ───

class TestGetEngineThreadSafety:
    """Verify get_engine() returns the same engine from multiple threads."""

    def test_concurrent_get_engine_returns_same_object(self):
        """10 threads calling get_engine() should all get the same Engine."""
        import integrations.social.models as models_mod
        old_engine = models_mod._engine
        models_mod._engine = None

        results = []
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=5)
                engine = models_mod.get_engine()
                results.append(id(engine))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        models_mod._engine = old_engine

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10
        assert len(set(results)) == 1, \
            f"Expected 1 unique engine, got {len(set(results))}"

    def test_get_engine_idempotent(self):
        """Calling get_engine() twice returns the same object."""
        from integrations.social.models import get_engine
        e1 = get_engine()
        e2 = get_engine()
        assert e1 is e2


class TestGetSessionFactoryThreadSafety:
    """Verify get_session_factory() returns the same factory from multiple threads."""

    def test_concurrent_get_session_factory_returns_same_object(self):
        """10 threads calling get_session_factory() should all get the same factory."""
        import integrations.social.models as models_mod
        old_factory = models_mod._SessionLocal
        models_mod._SessionLocal = None

        results = []
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=5)
                factory = models_mod.get_session_factory()
                results.append(id(factory))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        models_mod._SessionLocal = old_factory

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10
        assert len(set(results)) == 1, \
            f"Expected 1 unique factory, got {len(set(results))}"

    def test_get_session_factory_idempotent(self):
        """Calling get_session_factory() twice returns the same object."""
        from integrations.social.models import get_session_factory
        f1 = get_session_factory()
        f2 = get_session_factory()
        assert f1 is f2


# ─── parallel_dispatch.py thread-safe executor ───

class TestGetExecutorThreadSafety:
    """Verify get_executor() returns the same executor from multiple threads."""

    def test_concurrent_get_executor_returns_same_object(self):
        """10 threads calling get_executor() should all get the same executor."""
        import integrations.agent_engine.parallel_dispatch as pd_mod
        old_executor = pd_mod._executor
        pd_mod._executor = None

        results = []
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=5)
                executor = pd_mod.get_executor()
                results.append(id(executor))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        pd_mod._executor = old_executor

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10
        assert len(set(results)) == 1, \
            f"Expected 1 unique executor, got {len(set(results))}"

    def test_get_executor_idempotent(self):
        """Calling get_executor() twice returns the same object."""
        from integrations.agent_engine.parallel_dispatch import get_executor
        e1 = get_executor()
        e2 = get_executor()
        assert e1 is e2


# ─── Atexit shutdown registration ───

class TestAtexitShutdownRegistration:
    """Verify ThreadPoolExecutors register atexit shutdown handlers."""

    def test_parallel_dispatch_registers_atexit(self):
        """parallel_dispatch.get_executor() registers atexit shutdown."""
        import integrations.agent_engine.parallel_dispatch as pd_mod
        old_executor = pd_mod._executor
        pd_mod._executor = None

        registered = []
        original_register = atexit.register

        def mock_register(func, *args, **kwargs):
            registered.append(func)
            return original_register(func, *args, **kwargs)

        with patch('atexit.register', side_effect=mock_register):
            pd_mod.get_executor()

        pd_mod._executor = old_executor
        assert len(registered) >= 1, "Expected atexit.register to be called"

    def test_speculative_dispatcher_registers_atexit(self):
        """SpeculativeDispatcher.__init__ registers atexit for expert pool."""
        registered = []
        original_register = atexit.register

        def mock_register(func, *args, **kwargs):
            registered.append(func)
            return original_register(func, *args, **kwargs)

        with patch('atexit.register', side_effect=mock_register):
            mock_registry = MagicMock()
            from integrations.agent_engine.speculative_dispatcher import \
                SpeculativeDispatcher
            sd = SpeculativeDispatcher(model_registry=mock_registry)

        assert len(registered) >= 1, "Expected atexit.register to be called"

    def test_world_model_bridge_registers_atexit(self):
        """WorldModelBridge.__init__ registers atexit for flush executor."""
        registered = []
        original_register = atexit.register

        def mock_register(func, *args, **kwargs):
            registered.append(func)
            return original_register(func, *args, **kwargs)

        with patch('atexit.register', side_effect=mock_register):
            from integrations.agent_engine.world_model_bridge import \
                WorldModelBridge
            bridge = WorldModelBridge()

        assert len(registered) >= 1, "Expected atexit.register to be called"


# ─── Lock existence verification ───

class TestLockExistence:
    """Verify that lock objects exist on the modules."""

    def test_models_has_engine_lock(self):
        """models.py should have _engine_lock."""
        import integrations.social.models as models_mod
        assert hasattr(models_mod, '_engine_lock')
        assert isinstance(models_mod._engine_lock, type(threading.Lock()))

    def test_models_has_session_lock(self):
        """models.py should have _session_lock."""
        import integrations.social.models as models_mod
        assert hasattr(models_mod, '_session_lock')
        assert isinstance(models_mod._session_lock, type(threading.Lock()))

    def test_parallel_dispatch_has_executor_lock(self):
        """parallel_dispatch.py should have _executor_lock."""
        import integrations.agent_engine.parallel_dispatch as pd_mod
        assert hasattr(pd_mod, '_executor_lock')
        assert isinstance(pd_mod._executor_lock, type(threading.Lock()))
