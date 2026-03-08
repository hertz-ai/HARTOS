"""
Tests for the distributed dispatch bridge.

Tests the bridge between the agent daemon and DistributedTaskCoordinator:
- dispatch.py auto-detection (Redis reachable + hive peers → distribute)
- worker_loop.py (claim + execute tasks from Redis)
- gossip idle_compute advertising
- End-to-end: goal → Redis → claim → execute → result

No separate distributed mode flag — distribution is an emergent property
of network state: Redis reachable + active hive peers.
"""
import os
import sys
import json
import time
import pytest
import threading
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime

# ── Environment setup (before any project imports) ──
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('OPENAI_API_KEY', 'test-key')

pytest.importorskip('agent_ledger', reason='agent_ledger not installed')


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_guardrails():
    """Mock guardrails to allow all dispatches."""
    with patch('integrations.agent_engine.dispatch.GuardrailEnforcer',
               create=True) as mock_cls:
        mock_cls.before_dispatch = MagicMock(
            side_effect=lambda p, *a, **kw: (True, '', p))
        mock_cls.after_response = MagicMock(return_value=(True, ''))
        # Patch the import inside dispatch_goal
        with patch('security.hive_guardrails.GuardrailEnforcer', mock_cls):
            yield mock_cls


@pytest.fixture
def mock_coordinator():
    """Create a mock DistributedTaskCoordinator."""
    coord = MagicMock()
    coord.submit_goal.return_value = 'goal_test_123'
    coord.claim_next_task.return_value = None
    coord.submit_result.return_value = {
        'task_id': 'task_1', 'result_hash': 'abc123', 'status': 'completed'}
    coord.verify_result.return_value = True
    coord.get_goal_progress.return_value = {
        'goal_id': 'goal_test_123', 'total_tasks': 1,
        'completed': 0, 'progress_pct': 0.0, 'tasks': []}
    return coord


@pytest.fixture
def mock_task():
    """Create a mock Task object for worker_loop testing."""
    task = MagicMock()
    task.task_id = 'task_abc123'
    task.description = 'Test task: generate marketing content'
    task.context = {
        'prompt': 'Create a social media post about HART',
        'goal_type': 'marketing',
        'user_id': 'test_user',
        'capabilities_required': ['marketing'],
    }
    return task


# ═══════════════════════════════════════════════════════════════════════
# dispatch.py — Auto-Detection Tests (no mode flag)
# ═══════════════════════════════════════════════════════════════════════

class TestDistributedDispatch:
    """Tests for dispatch.py distributed auto-detection."""

    def test_has_hive_peers_false_when_no_peers(self):
        """No active peers → local dispatch."""
        from integrations.agent_engine.dispatch import _has_hive_peers
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.count.return_value = 0
        mock_db.query.return_value = mock_query
        with patch('integrations.social.models.get_db', return_value=mock_db):
            assert _has_hive_peers() is False

    def test_has_hive_peers_false_when_only_self(self):
        """Only self in peer table (count=1) → local dispatch."""
        from integrations.agent_engine.dispatch import _has_hive_peers
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.count.return_value = 1
        mock_db.query.return_value = mock_query
        with patch('integrations.social.models.get_db', return_value=mock_db):
            assert _has_hive_peers() is False

    def test_has_hive_peers_true_when_multiple_active(self):
        """Multiple active peers → distribute."""
        from integrations.agent_engine.dispatch import _has_hive_peers
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.count.return_value = 3
        mock_db.query.return_value = mock_query
        with patch('integrations.social.models.get_db', return_value=mock_db):
            assert _has_hive_peers() is True

    def test_has_hive_peers_handles_db_error(self):
        """DB error → safe fallback to False (local dispatch)."""
        from integrations.agent_engine.dispatch import _has_hive_peers
        with patch('integrations.social.models.get_db',
                   side_effect=Exception("DB unavailable")):
            assert _has_hive_peers() is False

    def test_decompose_goal_returns_single_task(self):
        """Default decomposition: 1 goal → 1 task."""
        from integrations.agent_engine.dispatch import _decompose_goal
        tasks = _decompose_goal('Test prompt', 'goal_123', 'marketing', 'user_1')
        assert len(tasks) == 1
        assert tasks[0]['task_id'] == 'goal_123_task_0'
        assert tasks[0]['capabilities'] == ['marketing']

    def test_dispatch_goal_distributed_success(self, mock_coordinator, mock_guardrails):
        """When coordinator is available, goal is submitted to Redis."""
        from integrations.agent_engine.dispatch import dispatch_goal_distributed
        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator):
            result = dispatch_goal_distributed(
                'Test prompt', 'user_1', 'goal_abc', 'marketing')
            assert result == 'goal_test_123'
            mock_coordinator.submit_goal.assert_called_once()

    def test_dispatch_goal_distributed_no_coordinator(self, mock_guardrails):
        """When coordinator is unavailable, returns None (fallback to local)."""
        from integrations.agent_engine.dispatch import dispatch_goal_distributed
        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=None):
            result = dispatch_goal_distributed(
                'Test prompt', 'user_1', 'goal_abc', 'marketing')
            assert result is None

    def test_dispatch_goal_auto_distributes_with_coordinator_and_peers(
            self, mock_coordinator, mock_guardrails):
        """dispatch_goal() auto-distributes when coordinator reachable + peers exist."""
        from integrations.agent_engine.dispatch import dispatch_goal
        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator):
            with patch('integrations.agent_engine.dispatch._has_hive_peers',
                       return_value=True):
                result = dispatch_goal(
                    'Test prompt', 'user_1', 'goal_abc', 'marketing')
                assert result == 'goal_test_123'
                mock_coordinator.submit_goal.assert_called_once()

    def test_dispatch_goal_falls_back_to_local_when_no_coordinator(
            self, mock_guardrails):
        """No coordinator → local /chat dispatch."""
        from integrations.agent_engine.dispatch import dispatch_goal
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'local result'}

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=None):
            with patch('integrations.agent_engine.dispatch.requests.post',
                       return_value=mock_resp):
                result = dispatch_goal(
                    'Test prompt', 'user_1', 'goal_abc', 'marketing')
                assert result == 'local result'

    def test_dispatch_goal_falls_back_to_local_when_no_peers(
            self, mock_coordinator, mock_guardrails):
        """Coordinator reachable but no peers → local dispatch (single-node)."""
        from integrations.agent_engine.dispatch import dispatch_goal
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'local result'}

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator):
            with patch('integrations.agent_engine.dispatch._has_hive_peers',
                       return_value=False):
                with patch('integrations.agent_engine.dispatch.requests.post',
                           return_value=mock_resp):
                    result = dispatch_goal(
                        'Test prompt', 'user_1', 'goal_abc', 'marketing')
                    assert result == 'local result'
                    # Coordinator should NOT have been called
                    mock_coordinator.submit_goal.assert_not_called()

    def test_dispatch_goal_falls_back_on_distributed_failure(
            self, mock_guardrails):
        """When distributed dispatch fails, falls back to local /chat."""
        from integrations.agent_engine.dispatch import dispatch_goal
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'local fallback'}

        # Coordinator reachable + peers exist, but submit_goal fails
        failing_coord = MagicMock()
        failing_coord.submit_goal.side_effect = Exception("Redis write failed")

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=failing_coord):
            with patch('integrations.agent_engine.dispatch._has_hive_peers',
                       return_value=True):
                with patch('integrations.agent_engine.dispatch.requests.post',
                           return_value=mock_resp):
                    result = dispatch_goal(
                        'Test prompt', 'user_1', 'goal_abc', 'marketing')
                    assert result == 'local fallback'

    def test_distributed_goal_context_includes_source_node(
            self, mock_coordinator, mock_guardrails):
        """Distributed goals carry source_node metadata."""
        from integrations.agent_engine.dispatch import dispatch_goal_distributed
        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator):
            with patch.dict(os.environ, {'HEVOLVE_NODE_ID': 'node_central'}):
                dispatch_goal_distributed(
                    'Test prompt', 'user_1', 'goal_abc', 'marketing')
                call_args = mock_coordinator.submit_goal.call_args
                context = call_args[1].get('context') or call_args[0][2]
                assert context['source_node'] == 'node_central'
                assert context['goal_type'] == 'marketing'


# ═══════════════════════════════════════════════════════════════════════
# worker_loop.py — Worker Claim Loop Tests
# ═══════════════════════════════════════════════════════════════════════

class TestDistributedWorkerLoop:
    """Tests for the distributed worker claim loop."""

    def test_worker_disabled_when_no_coordinator(self):
        """Worker loop doesn't start when Redis coordinator is not reachable."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        with patch('integrations.distributed_agent.api._get_coordinator',
                   return_value=None):
            assert DistributedWorkerLoop._is_enabled() is False

    def test_worker_enabled_when_coordinator_reachable(self):
        """Worker loop starts when shared Redis coordinator is reachable."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        mock_coord = MagicMock()
        with patch('integrations.distributed_agent.api._get_coordinator',
                   return_value=mock_coord):
            assert DistributedWorkerLoop._is_enabled() is True

    def test_worker_disabled_when_coordinator_returns_none(self):
        """Worker loop disabled when _get_coordinator returns None."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        with patch('integrations.distributed_agent.api._get_coordinator',
                   return_value=None):
            assert DistributedWorkerLoop._is_enabled() is False

    def test_worker_detects_capabilities(self):
        """Worker detects system capabilities for task matching."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        wl = DistributedWorkerLoop()
        # Base capabilities always present
        assert 'marketing' in wl._capabilities
        assert 'news' in wl._capabilities
        assert 'finance' in wl._capabilities

    def test_worker_tick_no_coordinator(self):
        """Tick does nothing when coordinator is unavailable."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        wl = DistributedWorkerLoop()
        with patch.object(wl, '_get_coordinator', return_value=None):
            wl._tick()  # Should not raise

    def test_worker_tick_no_tasks(self, mock_coordinator):
        """Tick does nothing when no tasks are available."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        mock_coordinator.claim_next_task.return_value = None
        wl = DistributedWorkerLoop()
        with patch.object(wl, '_get_coordinator', return_value=mock_coordinator):
            wl._tick()
            mock_coordinator.claim_next_task.assert_called_once()

    def test_worker_tick_claims_and_executes(self, mock_coordinator, mock_task,
                                              mock_guardrails):
        """Worker claims a task, executes via /chat, submits result."""
        wl_mod = sys.modules['integrations.distributed_agent.worker_loop']
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        mock_coordinator.claim_next_task.return_value = mock_task

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'Generated content'}

        wl = DistributedWorkerLoop()
        original_requests = wl_mod.requests
        with patch.object(wl, '_get_coordinator', return_value=mock_coordinator):
            try:
                mock_req = MagicMock()
                mock_req.post.return_value = mock_resp
                mock_req.RequestException = original_requests.RequestException
                wl_mod.requests = mock_req
                wl._tick()
                # Task was claimed
                mock_coordinator.claim_next_task.assert_called_once()
                # Result was submitted
                mock_coordinator.submit_result.assert_called_once_with(
                    'task_abc123', wl._node_id, 'Generated content')
            finally:
                wl_mod.requests = original_requests

    def test_worker_tick_handles_chat_failure(self, mock_coordinator, mock_task,
                                               mock_guardrails):
        """Worker handles /chat failure gracefully."""
        import requests as _requests
        wl_mod = sys.modules['integrations.distributed_agent.worker_loop']
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        mock_coordinator.claim_next_task.return_value = mock_task

        wl = DistributedWorkerLoop()
        original_requests = wl_mod.requests
        with patch.object(wl, '_get_coordinator', return_value=mock_coordinator):
            try:
                mock_req = MagicMock()
                mock_req.post.side_effect = _requests.RequestException("Connection refused")
                mock_req.RequestException = _requests.RequestException
                wl_mod.requests = mock_req
                wl._tick()
                # Task was claimed but result NOT submitted (failure)
                mock_coordinator.claim_next_task.assert_called_once()
                mock_coordinator.submit_result.assert_not_called()
            finally:
                wl_mod.requests = original_requests

    def test_worker_guardrail_blocks_task(self, mock_coordinator, mock_task):
        """Worker respects guardrail blocks."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        mock_coordinator.claim_next_task.return_value = mock_task

        wl = DistributedWorkerLoop()
        with patch.object(wl, '_get_coordinator', return_value=mock_coordinator):
            with patch('security.hive_guardrails.GuardrailEnforcer') as mock_guard:
                mock_guard.before_dispatch.return_value = (False, 'blocked', '')
                wl._tick()
                mock_coordinator.submit_result.assert_not_called()

    def test_worker_start_stop(self):
        """Worker can start and stop cleanly."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        wl = DistributedWorkerLoop()
        wl._interval = 1
        # Mock _is_enabled to return True so start() proceeds
        with patch.object(DistributedWorkerLoop, '_is_enabled', return_value=True):
            wl.start()
            assert wl._running is True
            time.sleep(0.1)
            wl.stop()
            assert wl._running is False


# ═══════════════════════════════════════════════════════════════════════
# peer_discovery.py — Idle Compute Advertising Tests
# ═══════════════════════════════════════════════════════════════════════

class TestGossipIdleCompute:
    """Tests for idle compute advertising in gossip beacons."""

    def test_self_info_includes_idle_compute_when_available(self):
        """Gossip self_info includes idle_compute stats when agents are idle."""
        from integrations.social.peer_discovery import GossipProtocol
        gp = GossipProtocol()
        mock_stats = {'currently_idle': 2, 'total_opted_in': 5}
        with patch('integrations.coding_agent.idle_detection.IdleDetectionService.get_idle_stats',
                   return_value=mock_stats):
            with patch('integrations.social.models.get_db') as mock_db:
                mock_db.return_value = MagicMock()
                info = gp._self_info()
                if 'idle_compute' in info:
                    assert info['idle_compute']['available'] is True
                    assert info['idle_compute']['idle_agents'] == 2
                    assert info['idle_compute']['opted_in'] == 5

    def test_self_info_no_distributed_mode_flag(self):
        """Gossip self_info does NOT include a distributed_mode flag.

        Distribution is emergent from network state, not a node property.
        """
        from integrations.social.peer_discovery import GossipProtocol
        gp = GossipProtocol()
        info = gp._self_info()
        assert 'distributed_mode' not in info

    def test_self_info_idle_compute_graceful_on_error(self):
        """If idle detection fails, self_info still works without idle_compute."""
        from integrations.social.peer_discovery import GossipProtocol
        gp = GossipProtocol()
        with patch('integrations.coding_agent.idle_detection.IdleDetectionService.get_idle_stats',
                   side_effect=Exception("idle detection unavailable")):
            info = gp._self_info()
            # Should not crash — idle_compute is optional
            assert 'node_id' in info


# ═══════════════════════════════════════════════════════════════════════
# End-to-End Distributed Flow
# ═══════════════════════════════════════════════════════════════════════

class TestDistributedEndToEnd:
    """End-to-end test: goal submission → Redis → claim → execute → result."""

    @staticmethod
    def _make_coordinator():
        """Create a fresh coordinator with isolated in-memory ledger."""
        import uuid
        from agent_ledger.core import SmartLedger
        from agent_ledger.distributed import DistributedTaskLock
        from integrations.distributed_agent.task_coordinator import DistributedTaskCoordinator

        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        mock_redis.eval.return_value = 1

        # Unique session_id ensures isolated task namespace
        ledger = SmartLedger(agent_id='test', session_id=f'e2e_{uuid.uuid4().hex[:8]}')
        lock = DistributedTaskLock(mock_redis)
        return DistributedTaskCoordinator(ledger=ledger, task_lock=lock), mock_redis

    def test_full_distributed_flow(self, mock_guardrails):
        """Submit a goal, claim it, execute it, submit result, verify."""
        coordinator, _ = self._make_coordinator()

        # 1. Submit goal (what the daemon does)
        goal_id = coordinator.submit_goal(
            objective='Create marketing post for HART',
            decomposed_tasks=[{
                'task_id': 'mkt_task_1',
                'description': 'Write a Twitter post about HART AI platform',
                'capabilities': ['marketing'],
            }],
            context={'goal_type': 'marketing', 'user_id': 'sys_agent'},
        )
        assert goal_id is not None
        assert goal_id.startswith('goal_')

        # 2. Claim task (what the worker does)
        task = coordinator.claim_next_task('worker_node_1', ['marketing'])
        assert task is not None
        assert task.task_id == 'mkt_task_1'
        assert task.context['capabilities_required'] == ['marketing']

        # 3. Submit result (worker completed the task)
        result_info = coordinator.submit_result(
            'mkt_task_1', 'worker_node_1',
            'Check out HART — crowdsourced AI for everyone! #AI #HevolveAI')
        assert result_info['status'] == 'completed'
        assert 'result_hash' in result_info

        # 4. Verify result (another node verifies)
        verified = coordinator.verify_result('mkt_task_1', 'verifier_node')
        assert verified is True

        # 5. Check progress
        progress = coordinator.get_goal_progress(goal_id)
        assert progress['completed'] == 1
        assert progress['progress_pct'] == 100.0

    def test_no_double_claim(self, mock_guardrails):
        """Two workers cannot claim the same task."""
        coordinator, mock_redis = self._make_coordinator()
        # First claim succeeds, second fails
        mock_redis.set.side_effect = [True, True, False]  # goal parent + child create + claim

        coordinator.submit_goal(
            objective='Test',
            decomposed_tasks=[{
                'task_id': 'task_1',
                'description': 'Task 1',
                'capabilities': ['coding'],
            }],
        )

        # Worker 1 claims
        task1 = coordinator.claim_next_task('worker_1', ['coding'])
        assert task1 is not None

        # Worker 2 tries to claim — nothing available (task_1 already in progress)
        task2 = coordinator.claim_next_task('worker_2', ['coding'])
        assert task2 is None

    def test_capability_matching(self):
        """Workers only claim tasks matching their capabilities."""
        coordinator, _ = self._make_coordinator()

        coordinator.submit_goal(
            objective='Vision task',
            decomposed_tasks=[{
                'task_id': 'vis_task_1',
                'description': 'Analyze image',
                'capabilities': ['vision'],
            }],
        )

        # Worker without vision capability can't claim
        task = coordinator.claim_next_task('basic_worker', ['marketing', 'coding'])
        assert task is None

        # Worker with vision can claim
        task = coordinator.claim_next_task('gpu_worker', ['vision', 'coding'])
        assert task is not None
        assert task.task_id == 'vis_task_1'


# ═══════════════════════════════════════════════════════════════════════
# Module-level __init__.py export test
# ═══════════════════════════════════════════════════════════════════════

class TestModuleExports:
    """Verify all distributed modules are importable."""

    def test_import_worker_loop(self):
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
        assert DistributedWorkerLoop is not None

    def test_import_coordinator(self):
        from integrations.distributed_agent.task_coordinator import DistributedTaskCoordinator
        assert DistributedTaskCoordinator is not None

    def test_import_dispatch_functions(self):
        from integrations.agent_engine.dispatch import (
            dispatch_goal, dispatch_goal_distributed,
            _has_hive_peers, _decompose_goal)
        assert dispatch_goal is not None
        assert dispatch_goal_distributed is not None
        assert _has_hive_peers is not None

    def test_init_exports_worker_loop(self):
        from integrations.distributed_agent import worker_loop, DistributedWorkerLoop
        assert worker_loop is not None
        assert DistributedWorkerLoop is not None

    def test_no_distributed_mode_function(self):
        """_is_distributed_mode no longer exists — replaced by auto-detection."""
        import integrations.agent_engine.dispatch as dispatch_mod
        assert not hasattr(dispatch_mod, '_is_distributed_mode')
