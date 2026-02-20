"""
Tests for parallel agent dispatch — SmartLedger → ThreadPoolExecutor wiring.

Covers:
1. ParallelDispatchEngine: fan-out, fan-in, mixed execution
2. GoalDecomposition: subtask extraction, ledger creation
3. DaemonParallelIntegration: _tick() parallel path
4. CoordinatorBatchClaim: atomic multi-task claiming
5. LedgerDependencyGraph: sibling/sequential tasks, auto-unblock
6. EndToEndParallelGoal: full pipeline integration
"""
import json
import os
import sys
import threading
import time
import pytest
from unittest.mock import patch, MagicMock

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_ledger import SmartLedger, Task, TaskType, TaskStatus
from agent_ledger.core import ExecutionMode
from agent_ledger.backends import InMemoryBackend


# ─── Helpers ───

_uid = 0


def _unique_id():
    """Generate unique IDs to prevent state leaking between tests."""
    global _uid
    _uid += 1
    return str(_uid)


def _make_ledger_with_parallel_tasks(n=3, parent_id=None):
    """Create a ledger with N parallel sibling tasks under a root."""
    uid = _unique_id()
    parent_id = parent_id or f'goal_root_{uid}'
    ledger = SmartLedger(agent_id='test', session_id=f'par_{uid}',
                         backend=InMemoryBackend())
    root = Task(
        task_id=parent_id,
        description='Root goal',
        task_type=TaskType.AUTONOMOUS,
        execution_mode=ExecutionMode.SEQUENTIAL,
    )
    ledger.add_task(root)
    siblings = ledger.create_sibling_tasks(
        parent_task_id=parent_id,
        sibling_descriptions=[f'Subtask {i+1}' for i in range(n)],
        task_type=TaskType.PRE_ASSIGNED,
    )
    # Mark siblings as PARALLEL + ready + in task_order
    # (create_sibling_tasks defaults to SEQUENTIAL, pending_reason=None,
    #  and doesn't call add_task so task_order is not updated)
    for sib in siblings:
        sib.execution_mode = ExecutionMode.PARALLEL
        sib.pending_reason = 'ready'
        if sib.task_id not in ledger.task_order:
            ledger.task_order.append(sib.task_id)
    return ledger, siblings


def _make_ledger_with_sequential_tasks(n=3):
    """Create a ledger with N sequential tasks."""
    uid = _unique_id()
    ledger = SmartLedger(agent_id='test', session_id=f'seq_{uid}',
                         backend=InMemoryBackend())
    tasks = ledger.create_sequential_tasks(
        [f'Step {i+1}' for i in range(n)],
        task_type=TaskType.PRE_ASSIGNED,
    )
    # Ensure in task_order
    for t in tasks:
        if t.task_id not in ledger.task_order:
            ledger.task_order.append(t.task_id)
    return ledger, tasks


def _success_dispatch(task):
    """Mock dispatch that always succeeds."""
    return {'success': True, 'response': f'Done: {task.description}'}


def _failing_dispatch(task):
    """Mock dispatch that always fails."""
    raise RuntimeError(f'Failed: {task.description}')


# ═══════════════════════════════════════════════════════════════
# 1. TestParallelDispatchEngine
# ═══════════════════════════════════════════════════════════════

class TestParallelDispatchEngine:
    """Tests for dispatch_parallel_tasks() and dispatch_goal_with_ledger()."""

    def test_dispatch_parallel_tasks_fan_out(self):
        """3 parallel tasks execute concurrently via ThreadPoolExecutor."""
        from integrations.agent_engine.parallel_dispatch import dispatch_parallel_tasks

        ledger, siblings = _make_ledger_with_parallel_tasks(3)
        executed = []

        def _dispatch(task):
            executed.append(task.task_id)
            return {'success': True}

        result = dispatch_parallel_tasks(ledger, _dispatch, max_concurrent=8)

        assert result['completed'] == 3
        assert result['failed'] == 0
        assert len(executed) == 3
        # All sibling task IDs should be in executed
        for sib in siblings:
            assert sib.task_id in executed

    def test_dispatch_parallel_tasks_empty(self):
        """No parallel tasks returns zero counts."""
        from integrations.agent_engine.parallel_dispatch import dispatch_parallel_tasks

        ledger = SmartLedger(agent_id='test', session_id='empty',
                             backend=InMemoryBackend())
        result = dispatch_parallel_tasks(ledger, _success_dispatch)

        assert result['completed'] == 0
        assert result['failed'] == 0
        assert result['results'] == {}

    def test_dispatch_goal_with_ledger_mixed(self):
        """Parallel tasks run first, then sequential tasks run after."""
        from integrations.agent_engine.parallel_dispatch import dispatch_goal_with_ledger

        ledger, siblings = _make_ledger_with_parallel_tasks(2)
        execution_order = []

        def _dispatch(task):
            execution_order.append(task.task_id)
            return {'success': True}

        result = dispatch_goal_with_ledger(ledger, _dispatch)

        assert result['completed'] >= 2
        assert result['failed'] == 0

    def test_dispatch_goal_with_ledger_all_sequential(self):
        """All sequential tasks execute one-by-one."""
        from integrations.agent_engine.parallel_dispatch import dispatch_goal_with_ledger

        ledger, tasks = _make_ledger_with_sequential_tasks(3)
        execution_order = []

        def _dispatch(task):
            execution_order.append(task.task_id)
            return {'success': True}

        result = dispatch_goal_with_ledger(ledger, _dispatch)

        # First task should execute (second is blocked until first completes)
        assert result['completed'] >= 1
        assert result['failed'] == 0

    def test_failed_task_marks_failure(self):
        """Exception in dispatch_fn marks task as failed."""
        from integrations.agent_engine.parallel_dispatch import dispatch_parallel_tasks

        ledger, siblings = _make_ledger_with_parallel_tasks(2)

        def _partial_fail(task):
            if 'sibling_1' in task.task_id:
                raise RuntimeError('boom')
            return {'success': True}

        result = dispatch_parallel_tasks(ledger, _partial_fail)

        assert result['completed'] == 1
        assert result['failed'] == 1

    def test_max_concurrent_respected(self):
        """Batch size capped at max_concurrent."""
        from integrations.agent_engine.parallel_dispatch import dispatch_parallel_tasks

        ledger, siblings = _make_ledger_with_parallel_tasks(5)
        executed = []

        def _dispatch(task):
            executed.append(task.task_id)
            return {'success': True}

        # Only allow 2 concurrent
        result = dispatch_parallel_tasks(ledger, _dispatch, max_concurrent=2)

        assert result['completed'] == 2
        assert len(executed) == 2


# ═══════════════════════════════════════════════════════════════
# 2. TestGoalDecomposition
# ═══════════════════════════════════════════════════════════════

class TestGoalDecomposition:
    """Tests for decompose_goal_to_ledger() and extract_subtasks_from_context()."""

    def test_single_task_decomposition_backward_compat(self):
        """No subtasks → single-task list, no ledger."""
        from integrations.agent_engine.parallel_dispatch import decompose_goal_to_ledger

        tasks, ledger = decompose_goal_to_ledger(
            'Do something', 'goal_1', 'marketing', 'user_1', None)

        assert len(tasks) == 1
        assert tasks[0]['task_id'] == 'goal_1_task_0'
        assert ledger is None

    def test_parallel_subtask_decomposition(self):
        """Goal with parallel subtasks creates sibling tasks."""
        from integrations.agent_engine.parallel_dispatch import decompose_goal_to_ledger

        subtask_defs = {
            'tasks': [
                {'description': 'Research A'},
                {'description': 'Research B'},
                {'description': 'Research C'},
            ],
            'parallel': True,
        }

        tasks, ledger = decompose_goal_to_ledger(
            'Research project', 'goal_2', 'coding', 'user_1', subtask_defs)

        assert ledger is not None
        # Root + 3 siblings = 4 tasks
        assert len(tasks) == 4
        # Check parallel siblings exist
        parallel_tasks = [t for t in tasks if t.get('execution_mode') == 'parallel']
        assert len(parallel_tasks) == 3

    def test_sequential_subtask_decomposition(self):
        """Goal with sequential subtasks creates chained tasks."""
        from integrations.agent_engine.parallel_dispatch import decompose_goal_to_ledger

        subtask_defs = {
            'tasks': [
                {'description': 'Extract'},
                {'description': 'Transform'},
                {'description': 'Load'},
            ],
            'parallel': False,
        }

        tasks, ledger = decompose_goal_to_ledger(
            'ETL pipeline', 'goal_3', 'coding', 'user_1', subtask_defs)

        assert ledger is not None
        assert len(tasks) >= 4  # root + 3 sequential
        # Sequential tasks should have prerequisites
        seq_tasks = [t for t in tasks if t.get('prerequisites')]
        assert len(seq_tasks) >= 2  # At least 2 tasks with prerequisites

    @patch('integrations.social.models.get_db')
    def test_extract_subtasks_from_context(self, mock_get_db):
        """Reads AgentGoal.context for subtask definitions."""
        from integrations.agent_engine.parallel_dispatch import extract_subtasks_from_context

        mock_goal = MagicMock()
        mock_goal.context = json.dumps({
            'tasks': [{'description': 'A'}, {'description': 'B'}],
            'parallel': True,
        })

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_goal
        mock_get_db.return_value = mock_db

        result = extract_subtasks_from_context('goal_1')

        assert result is not None
        assert len(result['tasks']) == 2
        assert result['parallel'] is True

    def test_import_error_fallback(self):
        """Graceful degradation when agent_ledger unavailable."""
        from integrations.agent_engine.parallel_dispatch import decompose_goal_to_ledger

        # Even with valid subtasks, if SmartLedger breaks, fallback to single task
        # SmartLedger is imported inside the function, so patch the module source
        with patch.dict('sys.modules', {'agent_ledger': None}):
            tasks, ledger = decompose_goal_to_ledger(
                'Test', 'g1', 'marketing', 'u1',
                {'tasks': [{'description': 'A'}, {'description': 'B'}], 'parallel': True})

        assert len(tasks) == 1
        assert ledger is None


# ═══════════════════════════════════════════════════════════════
# 3. TestDaemonParallelIntegration
# ═══════════════════════════════════════════════════════════════

class TestDaemonParallelIntegration:
    """Tests for AgentDaemon parallel dispatch wiring."""

    def test_try_parallel_dispatch_with_parallel_tasks(self):
        """_try_parallel_dispatch returns count > 0 when parallel tasks exist."""
        from integrations.agent_engine.agent_daemon import AgentDaemon

        daemon = AgentDaemon()
        ledger, siblings = _make_ledger_with_parallel_tasks(2)

        mock_goal = MagicMock()
        mock_goal.id = 'test_goal'
        mock_goal.goal_type = 'marketing'
        mock_goal.user_id = 'user_1'

        idle_agents = [{'user_id': 'a1'}, {'user_id': 'a2'}, {'user_id': 'a3'}]

        with patch.object(daemon, '_get_goal_ledger', return_value=ledger), \
             patch('integrations.agent_engine.parallel_dispatch.dispatch_parallel_tasks',
                   return_value={'completed': 2, 'failed': 0, 'results': {}}):
            count = daemon._try_parallel_dispatch(mock_goal, idle_agents, 0, 10)

        assert count == 2

    def test_try_parallel_dispatch_no_ledger(self):
        """_try_parallel_dispatch returns 0 when no ledger exists."""
        from integrations.agent_engine.agent_daemon import AgentDaemon

        daemon = AgentDaemon()
        mock_goal = MagicMock()
        mock_goal.id = 'test_goal'

        with patch.object(daemon, '_get_goal_ledger', return_value=None):
            count = daemon._try_parallel_dispatch(mock_goal, [], 0, 10)

        assert count == 0

    def test_get_goal_ledger_returns_none_single_task(self):
        """_get_goal_ledger returns None for single-task ledger."""
        from integrations.agent_engine.agent_daemon import AgentDaemon

        daemon = AgentDaemon()
        mock_goal = MagicMock()
        mock_goal.id = 'g1'
        mock_goal.user_id = 'u1'

        # No ledger file exists
        with patch('os.path.isfile', return_value=False):
            result = daemon._get_goal_ledger(mock_goal)

        assert result is None

    def test_get_goal_ledger_loads_existing(self):
        """_get_goal_ledger loads persisted ledger with multiple tasks."""
        from integrations.agent_engine.agent_daemon import AgentDaemon

        daemon = AgentDaemon()
        mock_goal = MagicMock()
        mock_goal.id = 'g1'
        mock_goal.user_id = 'u1'

        mock_ledger = MagicMock()
        mock_ledger.tasks = {'t1': MagicMock(), 't2': MagicMock()}

        with patch('os.path.isfile', return_value=True), \
             patch('agent_ledger.SmartLedger', return_value=mock_ledger):
            result = daemon._get_goal_ledger(mock_goal)

        assert result is not None
        mock_ledger.load.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# 4. TestCoordinatorBatchClaim
# ═══════════════════════════════════════════════════════════════

class TestCoordinatorBatchClaim:
    """Tests for DistributedTaskCoordinator.claim_parallel_batch()."""

    def _make_coordinator(self):
        from integrations.distributed_agent.task_coordinator import DistributedTaskCoordinator
        from agent_ledger.distributed import DistributedTaskLock

        uid = _unique_id()
        ledger = SmartLedger(agent_id='test', session_id=f'coord_{uid}',
                             backend=InMemoryBackend())
        mock_lock = MagicMock(spec=DistributedTaskLock)
        mock_lock.try_claim_task.return_value = True
        return DistributedTaskCoordinator(ledger, mock_lock), ledger

    def test_claim_parallel_batch(self):
        """Claims multiple parallel tasks atomically."""
        coord, ledger = self._make_coordinator()

        for i in range(3):
            task = Task(
                task_id=f'par_{_unique_id()}',
                description=f'Parallel task {i}',
                task_type=TaskType.AUTONOMOUS,
                execution_mode=ExecutionMode.PARALLEL,
            )
            ledger.add_task(task)

        claimed = coord.claim_parallel_batch('agent_1', max_tasks=4)
        assert len(claimed) == 3

    def test_claim_batch_respects_capabilities(self):
        """Only claims tasks matching agent capabilities."""
        coord, ledger = self._make_coordinator()
        uid = _unique_id()

        t1 = Task(task_id=f'py_{uid}', description='Python task',
                   task_type=TaskType.AUTONOMOUS,
                   execution_mode=ExecutionMode.PARALLEL)
        t1.context['capabilities_required'] = ['python']
        ledger.add_task(t1)

        t2 = Task(task_id=f'go_{uid}', description='Go task',
                   task_type=TaskType.AUTONOMOUS,
                   execution_mode=ExecutionMode.PARALLEL)
        t2.context['capabilities_required'] = ['golang']
        ledger.add_task(t2)

        claimed = coord.claim_parallel_batch(
            'agent_1', max_tasks=4, capabilities=['python'])

        assert len(claimed) == 1
        assert claimed[0].task_id == f'py_{uid}'

    def test_claim_batch_skips_sequential(self):
        """Only claims parallel-mode tasks, skips sequential."""
        coord, ledger = self._make_coordinator()
        uid = _unique_id()

        seq = Task(task_id=f'seq_{uid}', description='Sequential',
                   task_type=TaskType.AUTONOMOUS,
                   execution_mode=ExecutionMode.SEQUENTIAL)
        ledger.add_task(seq)

        par = Task(task_id=f'par_{uid}', description='Parallel',
                   task_type=TaskType.AUTONOMOUS,
                   execution_mode=ExecutionMode.PARALLEL)
        ledger.add_task(par)

        claimed = coord.claim_parallel_batch('agent_1', max_tasks=4)
        assert len(claimed) == 1
        assert claimed[0].task_id == f'par_{uid}'


# ═══════════════════════════════════════════════════════════════
# 5. TestLedgerDependencyGraph
# ═══════════════════════════════════════════════════════════════

class TestLedgerDependencyGraph:
    """Tests for SmartLedger dependency graph primitives."""

    def test_sibling_tasks_parallel_execution(self):
        """create_sibling_tasks + PARALLEL mode → get_parallel_executable_tasks finds them."""
        ledger, siblings = _make_ledger_with_parallel_tasks(3)

        parallel = ledger.get_parallel_executable_tasks()
        assert len(parallel) == 3

    def test_sequential_tasks_respect_order(self):
        """Sequential tasks block correctly — only first is executable."""
        ledger, tasks = _make_ledger_with_sequential_tasks(3)

        # Only first task should be ready (others blocked by prerequisites)
        next_task = ledger.get_next_executable_task()
        assert next_task is not None
        assert next_task.task_id == tasks[0].task_id

        # No parallel tasks (all sequential)
        parallel = ledger.get_parallel_executable_tasks()
        assert len(parallel) == 0

    def test_complete_and_route_unblocks(self):
        """Completing a task via update_task_status auto-unblocks dependents."""
        ledger, tasks = _make_ledger_with_sequential_tasks(3)

        first_id = tasks[0].task_id
        second_id = tasks[1].task_id

        # Complete first task (update_task_status triggers _handle_task_completion
        # which calls remove_blocking_task and auto-resume on dependents)
        ledger.update_task_status(first_id, TaskStatus.IN_PROGRESS)
        ledger.update_task_status(first_id, TaskStatus.COMPLETED,
                                  result={'data': 'result'})

        # Second task should now be executable (unblocked by completion)
        next_available = ledger.get_next_executable_task()
        assert next_available is not None
        assert next_available.task_id == second_id

    def test_hierarchical_task_tree(self):
        """Parent-child with tree traversal."""
        ledger = SmartLedger(agent_id='test', session_id='tree_test',
                             backend=InMemoryBackend())

        root = Task(task_id='root', description='Root',
                    task_type=TaskType.AUTONOMOUS)
        ledger.add_task(root)

        children = ledger.create_sibling_tasks(
            parent_task_id='root',
            sibling_descriptions=['Child A', 'Child B', 'Child C'],
        )

        # Root should have 3 children
        root_task = ledger.get_task('root')
        assert len(root_task.child_task_ids) == 3

        # Each child should reference root as parent
        for child in children:
            assert child.parent_task_id == 'root'


# ═══════════════════════════════════════════════════════════════
# 6. TestEndToEndParallelGoal
# ═══════════════════════════════════════════════════════════════

class TestEndToEndParallelGoal:
    """End-to-end: goal → decompose → parallel dispatch → aggregate."""

    def test_goal_with_parallel_subtasks_e2e(self):
        """Full flow: decompose with parallel subtasks → dispatch all → collect results."""
        from integrations.agent_engine.parallel_dispatch import (
            decompose_goal_to_ledger, dispatch_goal_with_ledger)

        uid = _unique_id()
        subtask_defs = {
            'tasks': [
                {'description': 'Research competitors'},
                {'description': 'Analyze market'},
                {'description': 'Draft report'},
            ],
            'parallel': True,
        }

        tasks, ledger = decompose_goal_to_ledger(
            'Market analysis', f'goal_e2e_{uid}', 'marketing', 'user_1',
            subtask_defs)

        assert ledger is not None
        assert len(tasks) == 4  # root + 3 parallel

        executed_tasks = []

        def _dispatch(task):
            executed_tasks.append(task.task_id)
            return {'success': True, 'output': f'Result for {task.description}'}

        result = dispatch_goal_with_ledger(ledger, _dispatch)

        # All 3 parallel tasks should execute
        assert result['completed'] >= 3
        assert result['failed'] == 0
        assert len(executed_tasks) >= 3

    def test_goal_with_mixed_parallel_sequential(self):
        """Parallel fan-out then sequential merge."""
        from integrations.agent_engine.parallel_dispatch import dispatch_goal_with_ledger

        uid = _unique_id()
        ledger = SmartLedger(agent_id='test', session_id=f'mixed_{uid}',
                             backend=InMemoryBackend())

        # Create root
        root = Task(task_id=f'root_{uid}', description='Project',
                    task_type=TaskType.AUTONOMOUS,
                    execution_mode=ExecutionMode.SEQUENTIAL)
        ledger.add_task(root)

        # Create 2 parallel research tasks
        siblings = ledger.create_sibling_tasks(
            parent_task_id=f'root_{uid}',
            sibling_descriptions=['Research A', 'Research B'],
        )
        for sib in siblings:
            sib.execution_mode = ExecutionMode.PARALLEL
            sib.pending_reason = 'ready'
            if sib.task_id not in ledger.task_order:
                ledger.task_order.append(sib.task_id)

        execution_order = []

        def _dispatch(task):
            execution_order.append(task.task_id)
            return {'success': True}

        result = dispatch_goal_with_ledger(ledger, _dispatch)

        # Both parallel tasks should execute
        assert result['completed'] >= 2
        assert result['failed'] == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
