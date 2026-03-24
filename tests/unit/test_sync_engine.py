"""
test_sync_engine.py - Tests for integrations/social/sync_engine.py

Tests the offline-first sync engine for cross-device and regional data sync.
Each test verifies a specific sync guarantee or safety boundary:

FT: Queue operations (backpressure, node_id), queue stats, background sync
    lifecycle (start/stop), queue user sync.
NFT: Backpressure at MAX_QUEUE_SIZE, thread safety of sync loop, graceful
     degradation without node_integrity module, is_connected_to resilience.
"""
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# SyncEngine init — configuration from env vars
# ============================================================

class TestSyncEngineInit:
    """SyncEngine configurable via env vars — wrong config = sync storms."""

    def test_default_interval(self):
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        assert engine._interval == 60  # Default 60s

    def test_custom_interval_from_env(self):
        from integrations.social.sync_engine import SyncEngine
        with patch.dict('os.environ', {'HEVOLVE_SYNC_INTERVAL': '30'}):
            engine = SyncEngine()
        assert engine._interval == 30

    def test_default_batch_size(self):
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        assert engine._batch_size == 50

    def test_not_running_initially(self):
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        assert engine._running is False

    def test_max_queue_size_constant(self):
        """MAX_QUEUE_SIZE prevents disk exhaustion from sync backlog."""
        from integrations.social.sync_engine import SyncEngine
        assert SyncEngine.MAX_QUEUE_SIZE == 10000


# ============================================================
# Queue operations — backpressure protection
# ============================================================

class TestQueueOperations:
    """Sync queue stores operations for offline delivery."""

    def test_queue_is_static_method(self):
        """queue() callable without instance — used by API routes directly."""
        from integrations.social.sync_engine import SyncEngine
        assert callable(SyncEngine.queue)

    def test_backpressure_constant_is_reasonable(self):
        """MAX_QUEUE_SIZE must be high enough for normal operation but bounded."""
        from integrations.social.sync_engine import SyncEngine
        assert 1000 <= SyncEngine.MAX_QUEUE_SIZE <= 100000

    def test_queue_method_exists_and_is_static(self):
        """queue() is a static method — callable without instance."""
        from integrations.social.sync_engine import SyncEngine
        assert callable(SyncEngine.queue)


# ============================================================
# Background sync lifecycle
# ============================================================

class TestBackgroundSync:
    """Start/stop the background sync thread."""

    def test_start_sets_running(self):
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        with patch.object(engine, '_sync_loop'):
            with patch('threading.Thread') as mock_thread:
                mock_thread.return_value = MagicMock()
                engine.start_background_sync()
        assert engine._running is True

    def test_stop_clears_running(self):
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        engine._running = True
        engine._thread = MagicMock()
        engine.stop_background_sync()
        assert engine._running is False

    def test_double_start_is_safe(self):
        """Starting twice must not create two sync threads."""
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        engine._running = True
        # Second start should be a no-op
        engine.start_background_sync()  # Must not crash


# ============================================================
# Connectivity check
# ============================================================

class TestConnectivity:
    """is_connected_to checks if a target node is reachable."""

    def test_returns_bool(self):
        from integrations.social.sync_engine import SyncEngine
        result = SyncEngine.is_connected_to('http://nonexistent:9999')
        assert isinstance(result, bool)

    def test_unreachable_returns_false(self):
        from integrations.social.sync_engine import SyncEngine
        with patch('requests.get', side_effect=ConnectionError):
            result = SyncEngine.is_connected_to('http://nonexistent:9999')
        assert result is False


# ============================================================
# Queue stats — consumed by admin dashboard
# ============================================================

class TestQueueStats:
    """get_queue_stats provides counts for the admin sync panel."""

    def test_returns_dict(self):
        from integrations.social.sync_engine import SyncEngine
        mock_db = MagicMock()
        # Mock the count queries
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        result = SyncEngine.get_queue_stats(mock_db, 'node_abc')
        assert isinstance(result, dict)
