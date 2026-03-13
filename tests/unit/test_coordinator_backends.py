"""
Tests for coordinator backend abstraction.

Verifies that:
1. InMemoryTaskLock provides thread-safe atomic claim/release
2. InMemoryHostRegistry provides host registration and lookup
3. GossipTaskBridge propagates tasks to peers via HTTP
4. create_coordinator() falls back to in-memory when Redis unavailable
5. Backend-agnostic DistributedTaskCoordinator works with in-memory
"""

import os
import sys
import time
import threading
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

pytest.importorskip('agent_ledger', reason='agent_ledger not installed')


class TestInMemoryTaskLock:
    """Test InMemoryTaskLock — thread-safe, no Redis."""

    def test_claim_unclaimed_task(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        assert lock.try_claim_task('task_1', 'agent_A') is True

    def test_cannot_double_claim(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        lock.try_claim_task('task_1', 'agent_A')
        assert lock.try_claim_task('task_1', 'agent_B') is False

    def test_release_then_reclaim(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        lock.try_claim_task('task_1', 'agent_A')
        lock.release_task('task_1', 'agent_A')
        assert lock.try_claim_task('task_1', 'agent_B') is True

    def test_cannot_release_other_agents_lock(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        lock.try_claim_task('task_1', 'agent_A')
        assert lock.release_task('task_1', 'agent_B') is False

    def test_get_task_owner(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        lock.try_claim_task('task_1', 'agent_A')
        assert lock.get_task_owner('task_1') == 'agent_A'
        assert lock.get_task_owner('task_99') is None

    def test_is_task_locked(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        assert lock.is_task_locked('task_1') is False
        lock.try_claim_task('task_1', 'agent_A')
        assert lock.is_task_locked('task_1') is True

    def test_expired_lock_allows_reclaim(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        # Claim with 1ms TTL → expires almost immediately
        lock.try_claim_task('task_1', 'agent_A', ttl=0)
        time.sleep(0.05)
        assert lock.try_claim_task('task_1', 'agent_B') is True

    def test_reclaim_stale_tasks(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        lock.try_claim_task('task_1', 'agent_A', ttl=0)
        lock.try_claim_task('task_2', 'agent_B', ttl=300)
        time.sleep(0.05)
        reclaimed = lock.reclaim_stale_tasks()
        assert 'task_1' in reclaimed
        assert 'task_2' not in reclaimed

    def test_thread_safety_concurrent_claims(self):
        """Multiple threads trying to claim the same task — only one wins."""
        from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
        lock = InMemoryTaskLock()
        results = []

        def try_claim(agent_id):
            result = lock.try_claim_task('contested_task', agent_id)
            results.append((agent_id, result))

        threads = [threading.Thread(target=try_claim, args=(f'agent_{i}',))
                    for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r[1] is True]
        assert len(winners) == 1  # Exactly one agent wins


class TestInMemoryHostRegistry:
    """Test InMemoryHostRegistry — thread-safe, no Redis."""

    def test_register_and_list(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryHostRegistry
        reg = InMemoryHostRegistry(host_id='host_1', host_url='http://localhost:6777')
        reg.register_host(['coding', 'marketing'])
        hosts = reg.get_all_hosts()
        assert len(hosts) == 1
        assert hosts[0]['host_id'] == 'host_1'
        assert 'coding' in hosts[0]['capabilities']

    def test_deregister(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryHostRegistry
        reg = InMemoryHostRegistry(host_id='host_1')
        reg.register_host(['coding'])
        reg.deregister_host()
        assert len(reg.get_all_hosts()) == 0

    def test_get_hosts_with_capability(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryHostRegistry
        reg = InMemoryHostRegistry(host_id='host_1')
        reg.register_host(['coding', 'marketing'])
        # Add another host manually to test filtering
        reg._hosts['host_2'] = {
            'host_id': 'host_2', 'capabilities': ['marketing'],
        }
        coding_hosts = reg.get_hosts_with_capability('coding')
        assert len(coding_hosts) == 1
        assert coding_hosts[0]['host_id'] == 'host_1'

    def test_update_compute_usage(self):
        from integrations.distributed_agent.coordinator_backends import InMemoryHostRegistry
        reg = InMemoryHostRegistry(host_id='host_1')
        reg.register_host(['coding'])
        reg.update_compute_usage({'cpu_pct': 50, 'ram_pct': 70})
        info = reg.get_host_info('host_1')
        assert info['compute_usage']['cpu_pct'] == 50


class TestGossipTaskBridge:
    """Test GossipTaskBridge — HTTP-based peer task propagation."""

    def test_announce_goal_to_peers(self):
        from integrations.distributed_agent.coordinator_backends import GossipTaskBridge
        bridge = GossipTaskBridge()

        mock_peers = [
            MagicMock(url='http://peer1:6777', node_id='n1', status='active'),
            MagicMock(url='http://peer2:6777', node_id='n2', status='active'),
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(bridge, '_get_active_peers', return_value=[
            {'host_url': 'http://peer1:6777', 'node_id': 'n1'},
            {'host_url': 'http://peer2:6777', 'node_id': 'n2'},
        ]):
            with patch('requests.post', return_value=mock_response) as mock_post:
                notified = bridge.announce_goal(
                    'goal_1', 'test objective',
                    [{'task_id': 't1', 'description': 'do stuff'}],
                    {'user_id': 'u1'},
                )

        assert notified == 2
        assert mock_post.call_count == 2

    def test_announce_handles_unreachable_peers(self):
        from integrations.distributed_agent.coordinator_backends import GossipTaskBridge
        import requests as req_lib

        bridge = GossipTaskBridge()

        with patch.object(bridge, '_get_active_peers', return_value=[
            {'host_url': 'http://dead-peer:6777', 'node_id': 'n1'},
        ]):
            with patch('requests.post', side_effect=req_lib.RequestException("timeout")):
                notified = bridge.announce_goal('goal_2', 'test', [], {})

        assert notified == 0  # Graceful degradation

    def test_pull_tasks_from_peers(self):
        from integrations.distributed_agent.coordinator_backends import GossipTaskBridge
        bridge = GossipTaskBridge()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'tasks': [{'task_id': 't1', 'description': 'from peer'}]
        }

        with patch.object(bridge, '_get_active_peers', return_value=[
            {'host_url': 'http://peer1:6777', 'node_id': 'n1'},
        ]):
            with patch('requests.get', return_value=mock_response):
                tasks = bridge.pull_tasks_from_peers()

        assert len(tasks) == 1
        assert tasks[0]['task_id'] == 't1'


class TestCreateCoordinator:
    """Test create_coordinator() factory — Redis → in-memory fallback."""

    def test_fallback_to_inmemory_when_no_redis(self):
        """Without Redis, coordinator still works with in-memory backend."""
        from integrations.distributed_agent.coordinator_backends import create_coordinator

        with patch('integrations.distributed_agent.coordinator_backends._try_redis_backend',
                   return_value=None):
            coordinator, backend_type = create_coordinator(agent_id='test')

        assert coordinator is not None
        assert backend_type == 'inmemory'

    def test_prefers_redis_when_available(self):
        from integrations.distributed_agent.coordinator_backends import create_coordinator

        mock_coordinator = MagicMock()
        with patch('integrations.distributed_agent.coordinator_backends._try_redis_backend',
                   return_value=mock_coordinator):
            coordinator, backend_type = create_coordinator()

        assert coordinator is mock_coordinator
        assert backend_type == 'redis'

    def test_inmemory_coordinator_can_submit_and_claim(self, tmp_path):
        """Full workflow: submit goal → claim task → submit result."""
        import uuid
        from integrations.distributed_agent.coordinator_backends import create_coordinator

        # Use unique temp dir so JSONBackend doesn't collide with prior runs
        with patch.dict(os.environ, {'HEVOLVE_DB_PATH': str(tmp_path / 'test.db')}):
            with patch('integrations.distributed_agent.coordinator_backends._try_redis_backend',
                       return_value=None):
                coordinator, backend_type = create_coordinator(agent_id='test_worker')

        assert backend_type == 'inmemory'

        uid = uuid.uuid4().hex[:8]

        # Submit a goal with one task
        goal_id = coordinator.submit_goal(
            objective='Test distributed work',
            decomposed_tasks=[{
                'task_id': f'task_{uid}',
                'description': 'Do something useful',
                'capabilities': ['marketing'],
            }],
            context={'user_id': 'test_user'},
        )
        assert goal_id is not None

        # Claim the task
        task = coordinator.claim_next_task('worker_1', ['marketing'])
        assert task is not None
        assert task.task_id == f'task_{uid}'

        # Submit result
        result = coordinator.submit_result(f'task_{uid}', 'worker_1', 'done!')
        assert result['status'] == 'completed'

        # Check progress
        progress = coordinator.get_goal_progress(goal_id)
        assert progress['completed'] == 1
        assert progress['progress_pct'] == 100.0

    def test_inmemory_lock_prevents_double_claim(self, tmp_path):
        """Two workers cannot claim the same task."""
        import uuid
        from integrations.distributed_agent.coordinator_backends import create_coordinator

        with patch.dict(os.environ, {'HEVOLVE_DB_PATH': str(tmp_path / 'test.db')}):
            with patch('integrations.distributed_agent.coordinator_backends._try_redis_backend',
                       return_value=None):
                coordinator, _ = create_coordinator(agent_id='test')

        uid = uuid.uuid4().hex[:8]

        coordinator.submit_goal(
            objective='Test',
            decomposed_tasks=[{
                'task_id': f'contested_{uid}',
                'description': 'Only one can claim',
                'capabilities': ['coding'],
            }],
        )

        task1 = coordinator.claim_next_task('worker_A', ['coding'])
        task2 = coordinator.claim_next_task('worker_B', ['coding'])

        assert task1 is not None
        assert task2 is None  # Already claimed by worker_A


class TestApiCoordinatorSingleton:
    """Test that api._get_coordinator() uses the new backend factory."""

    def test_get_coordinator_returns_inmemory_without_redis(self):
        """api._get_coordinator() works without Redis."""
        # Reset singleton
        if hasattr(_get_coordinator_fn, '_instance'):
            delattr(_get_coordinator_fn, '_instance')

        with patch('integrations.distributed_agent.coordinator_backends._try_redis_backend',
                   return_value=None):
            from integrations.distributed_agent.api import _get_coordinator
            coordinator = _get_coordinator()

        # Clean up singleton for other tests
        if hasattr(_get_coordinator, '_instance'):
            delattr(_get_coordinator, '_instance')

        assert coordinator is not None


# Helper reference for singleton cleanup
def _get_coordinator_fn():
    pass


try:
    from integrations.distributed_agent.api import _get_coordinator as _get_coordinator_fn
except ImportError:
    pass
