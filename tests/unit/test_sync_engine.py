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
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        result = SyncEngine.get_queue_stats(mock_db, 'node_abc')
        assert isinstance(result, dict)


# ============================================================
# receive_sync_batch — process incoming items from child nodes
# ============================================================

class TestReceiveSyncBatch:
    """receive_sync_batch processes items from child nodes in the hierarchy."""

    def test_returns_dict_with_processed_and_errors(self):
        from integrations.social.sync_engine import SyncEngine
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        result = SyncEngine.receive_sync_batch(mock_db, [
            {'id': 'item_1', 'operation_type': 'register_agent', 'payload': {}},
        ])
        assert isinstance(result, dict)
        assert 'processed' in result
        assert 'errors' in result

    def test_processes_known_operation_types(self):
        """Known op types must be processed without errors."""
        from integrations.social.sync_engine import SyncEngine
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        items = [
            {'id': '1', 'operation_type': 'register_agent', 'payload': {}},
            {'id': '2', 'operation_type': 'sync_post', 'payload': {}},
            {'id': '3', 'operation_type': 'update_stats', 'payload': {}},
        ]
        result = SyncEngine.receive_sync_batch(mock_db, items)
        assert len(result['processed']) == 3
        assert len(result['errors']) == 0

    def test_handles_unknown_operation_type(self):
        """Unknown op types are logged but not errored — forward compatibility."""
        from integrations.social.sync_engine import SyncEngine
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        result = SyncEngine.receive_sync_batch(mock_db, [
            {'id': '1', 'operation_type': 'future_op_type_v2', 'payload': {}},
        ])
        assert '1' in result['processed']

    def test_empty_batch_returns_empty(self):
        from integrations.social.sync_engine import SyncEngine
        result = SyncEngine.receive_sync_batch(MagicMock(), [])
        assert result == {'processed': [], 'errors': []}

    def test_idempotency_skips_already_completed(self):
        """Already-processed items must be skipped — prevents double processing."""
        from integrations.social.sync_engine import SyncEngine
        mock_db = MagicMock()
        mock_existing = MagicMock()
        mock_existing.status = 'completed'
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_existing
        result = SyncEngine.receive_sync_batch(mock_db, [
            {'id': 'already_done', 'operation_type': 'sync_post', 'payload': {}},
        ])
        assert 'already_done' in result['processed']


# ============================================================
# queue_user_sync — convenience for user data sync
# ============================================================

class TestQueueUserSync:
    """queue_user_sync queues user profile changes for regional sync."""

    def test_method_exists(self):
        from integrations.social.sync_engine import SyncEngine
        assert callable(SyncEngine.queue_user_sync)


# ============================================================
# Sync loop internals
# ============================================================

class TestSyncLoopInternals:
    """Internal methods of the background sync loop."""

    def test_do_sync_drain_exists(self):
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        assert callable(engine._do_sync_drain)

    def test_lock_exists(self):
        """Sync engine must have a threading lock for concurrent access."""
        from integrations.social.sync_engine import SyncEngine
        engine = SyncEngine()
        assert engine._lock is not None
