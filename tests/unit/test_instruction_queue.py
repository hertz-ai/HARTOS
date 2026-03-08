"""Tests for instruction queue and shard engine."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestInstruction(unittest.TestCase):
    """Test Instruction data class."""

    def test_create_instruction(self):
        from integrations.agent_engine.instruction_queue import Instruction
        inst = Instruction('user1', 'Add WiFi API', priority=3, tags=['os', 'network'])
        self.assertEqual(inst.user_id, 'user1')
        self.assertEqual(inst.text, 'Add WiFi API')
        self.assertEqual(inst.priority, 3)
        self.assertEqual(inst.tags, ['os', 'network'])
        self.assertEqual(inst.status.value, 'queued')
        self.assertEqual(len(inst.id), 16)

    def test_serialization_roundtrip(self):
        from integrations.agent_engine.instruction_queue import Instruction
        inst = Instruction('u1', 'Fix bug', priority=1, context={'file': 'main.py'})
        d = inst.to_dict()
        restored = Instruction.from_dict(d)
        self.assertEqual(restored.id, inst.id)
        self.assertEqual(restored.text, inst.text)
        self.assertEqual(restored.priority, inst.priority)
        self.assertEqual(restored.context, inst.context)

    def test_default_priority(self):
        from integrations.agent_engine.instruction_queue import Instruction
        inst = Instruction('u1', 'Task')
        self.assertEqual(inst.priority, 5)


class TestInstructionQueue(unittest.TestCase):
    """Test persistent instruction queue."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Patch queue directory
        import integrations.agent_engine.instruction_queue as iq
        self._orig_dir = iq._QUEUE_DIR
        iq._QUEUE_DIR = self.tmpdir

    def tearDown(self):
        import integrations.agent_engine.instruction_queue as iq
        iq._QUEUE_DIR = self._orig_dir
        iq._queues.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enqueue_and_get_pending(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('test_user')
        q.enqueue('Task 1')
        q.enqueue('Task 2')
        pending = q.get_pending()
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0].text, 'Task 1')
        self.assertEqual(pending[1].text, 'Task 2')

    def test_deduplication(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('test_user')
        i1 = q.enqueue('Same task')
        i2 = q.enqueue('Same task')
        self.assertEqual(i1.id, i2.id)  # Same instruction returned
        self.assertEqual(len(q.get_pending()), 1)

    def test_priority_ordering(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('test_user')
        q.enqueue('Low priority', priority=8)
        q.enqueue('High priority', priority=1)
        q.enqueue('Medium priority', priority=5)
        pending = q.get_pending()
        self.assertEqual(pending[0].text, 'High priority')
        self.assertEqual(pending[1].text, 'Medium priority')
        self.assertEqual(pending[2].text, 'Low priority')

    def test_persistence(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q1 = InstructionQueue('persist_user')
        q1.enqueue('Persistent task')
        # Create new queue from same file
        q2 = InstructionQueue('persist_user')
        self.assertEqual(len(q2.get_pending()), 1)
        self.assertEqual(q2.get_pending()[0].text, 'Persistent task')

    def test_mark_done(self):
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('test_user')
        inst = q.enqueue('Complete me')
        q.mark_status(inst.id, InstructionStatus.DONE, result='OK')
        self.assertEqual(len(q.get_pending()), 0)

    def test_cancel(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('test_user')
        inst = q.enqueue('Cancel me')
        q.cancel(inst.id)
        self.assertEqual(len(q.get_pending()), 0)

    def test_stats(self):
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('test_user')
        q.enqueue('A')
        q.enqueue('B')
        inst = q.enqueue('C')
        q.mark_status(inst.id, InstructionStatus.DONE)
        stats = q.stats()
        self.assertEqual(stats['total'], 3)
        self.assertEqual(stats['pending'], 2)
        self.assertEqual(stats['by_status']['done'], 1)

    def test_clear_done(self):
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('test_user')
        i1 = q.enqueue('Stay')
        i2 = q.enqueue('Remove')
        q.mark_status(i2.id, InstructionStatus.DONE)
        removed = q.clear_done()
        self.assertEqual(removed, 1)
        self.assertEqual(len(q.get_all()), 1)


class TestBatchConsolidation(unittest.TestCase):
    """Test batch pulling and consolidation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import integrations.agent_engine.instruction_queue as iq
        self._orig_dir = iq._QUEUE_DIR
        iq._QUEUE_DIR = self.tmpdir

    def tearDown(self):
        import integrations.agent_engine.instruction_queue as iq
        iq._QUEUE_DIR = self._orig_dir
        iq._queues.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pull_single_instruction(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('batch_user')
        q.enqueue('Fix the WiFi API')
        batch = q.pull_batch()
        self.assertIsNotNone(batch)
        self.assertEqual(len(batch.instructions), 1)
        self.assertIn('Fix the WiFi API', batch.consolidated_prompt)

    def test_pull_multiple_instructions(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('batch_user')
        q.enqueue('Add battery monitor', tags=['os'])
        q.enqueue('Fix Bluetooth timeout', tags=['os'])
        q.enqueue('Update README', tags=['docs'])
        batch = q.pull_batch()
        self.assertIsNotNone(batch)
        self.assertEqual(len(batch.instructions), 3)
        self.assertIn('3 queued instructions', batch.consolidated_prompt)
        self.assertIn('battery monitor', batch.consolidated_prompt)

    def test_batch_respects_token_limit(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('batch_user')
        # Add instructions with large context
        for i in range(10):
            q.enqueue(f'Task {i}', context={'data': 'x' * 1000})
        batch = q.pull_batch(max_tokens=500)
        # Should only include as many as fit
        self.assertLess(len(batch.instructions), 10)
        self.assertGreater(len(batch.instructions), 0)

    def test_batch_marks_instructions_as_batched(self):
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('batch_user')
        q.enqueue('Task A')
        q.enqueue('Task B')
        batch = q.pull_batch()
        # Pending should now be empty (batched, not queued)
        self.assertEqual(len(q.get_pending()), 0)
        # But instructions still exist
        all_insts = q.get_all()
        for inst in all_insts:
            self.assertEqual(inst.status, InstructionStatus.BATCHED)
            self.assertEqual(inst.batch_id, batch.batch_id)

    def test_complete_batch(self):
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('batch_user')
        q.enqueue('Task A')
        q.enqueue('Task B')
        batch = q.pull_batch()
        q.complete_batch(batch.batch_id, result='All done')
        for inst in q.get_all():
            self.assertEqual(inst.status, InstructionStatus.DONE)
            self.assertEqual(inst.result, 'All done')

    def test_fail_batch_returns_to_queue(self):
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('batch_user')
        q.enqueue('Retry me')
        batch = q.pull_batch()
        q.fail_batch(batch.batch_id, 'Compute failed')
        # Should be back in queue
        pending = q.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, InstructionStatus.QUEUED)
        self.assertIsNone(pending[0].batch_id)

    def test_empty_queue_returns_none(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('empty_user')
        batch = q.pull_batch()
        self.assertIsNone(batch)

    def test_tag_grouping_in_consolidation(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('batch_user')
        q.enqueue('Add WiFi', tags=['network'])
        q.enqueue('Add VPN', tags=['network'])
        q.enqueue('Fix theme', tags=['ui'])
        batch = q.pull_batch()
        self.assertIn('Group: network', batch.consolidated_prompt)
        self.assertIn('Group: ui', batch.consolidated_prompt)

    def test_batch_token_estimate(self):
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('batch_user')
        q.enqueue('Short task')
        batch = q.pull_batch()
        self.assertGreater(batch.token_estimate, 0)


class TestConvenienceFunctions(unittest.TestCase):
    """Test module-level convenience functions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import integrations.agent_engine.instruction_queue as iq
        self._orig_dir = iq._QUEUE_DIR
        iq._QUEUE_DIR = self.tmpdir

    def tearDown(self):
        import integrations.agent_engine.instruction_queue as iq
        iq._QUEUE_DIR = self._orig_dir
        iq._queues.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enqueue_instruction(self):
        from integrations.agent_engine.instruction_queue import (
            enqueue_instruction, get_queue,
        )
        inst = enqueue_instruction('conv_user', 'Do something')
        q = get_queue('conv_user')
        self.assertEqual(len(q.get_pending()), 1)

    def test_pull_user_batch(self):
        from integrations.agent_engine.instruction_queue import (
            enqueue_instruction, pull_user_batch,
        )
        enqueue_instruction('conv_user', 'Task 1')
        enqueue_instruction('conv_user', 'Task 2')
        batch = pull_user_batch('conv_user')
        self.assertIsNotNone(batch)
        self.assertEqual(len(batch.instructions), 2)


class TestLedgerIntegration(unittest.TestCase):
    """Test SmartLedger integration for dependency-aware ordering."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import integrations.agent_engine.instruction_queue as iq
        self._orig_dir = iq._QUEUE_DIR
        iq._QUEUE_DIR = self.tmpdir

    def tearDown(self):
        import integrations.agent_engine.instruction_queue as iq
        iq._QUEUE_DIR = self._orig_dir
        iq._queues.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enqueue_registers_with_ledger(self):
        """enqueue() should call ledger.add_dynamic_task() when available."""
        from unittest.mock import MagicMock, patch
        from integrations.agent_engine.instruction_queue import InstructionQueue

        mock_task = MagicMock()
        mock_task.task_id = 'dynamic_1'
        mock_ledger = MagicMock()
        mock_ledger.add_dynamic_task.return_value = mock_task
        mock_ledger.tasks = {}

        q = InstructionQueue('ledger_user')
        q._ledger = mock_ledger  # Inject mock

        inst = q.enqueue('Build battery API')
        mock_ledger.add_dynamic_task.assert_called_once()
        call_args = mock_ledger.add_dynamic_task.call_args
        self.assertEqual(call_args[1]['task_description']
                         if 'task_description' in call_args[1]
                         else call_args[0][0],
                         'Build battery API')
        # Instruction should have ledger task ID in context
        self.assertEqual(inst.context.get('ledger_task_id'), 'dynamic_1')
        # Task map should be populated
        self.assertEqual(q._task_map[inst.id], 'dynamic_1')

    def test_enqueue_graceful_without_ledger(self):
        """enqueue() works fine when SmartLedger is unavailable."""
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('no_ledger_user')
        q._ledger = None  # Explicitly no ledger

        inst = q.enqueue('Still works')
        self.assertEqual(inst.text, 'Still works')
        self.assertEqual(len(q.get_pending()), 1)
        self.assertNotIn('ledger_task_id', inst.context)

    def test_pull_batch_uses_ledger_ordering(self):
        """pull_batch() should use ledger's dependency graph for ordering."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('order_user')

        # Enqueue without ledger first
        i1 = q.enqueue('Step 1: Create API', priority=5)
        i2 = q.enqueue('Step 2: Write tests', priority=5)
        i3 = q.enqueue('Step 3: Deploy', priority=5)

        # Now set up a mock ledger that says i3 depends on i2 depends on i1
        mock_ledger = MagicMock()
        mock_ledger.tasks = {}

        # Create mock tasks with correct ordering
        mock_task1 = MagicMock()
        mock_task1.task_id = 'task_1'
        mock_task1.status = MagicMock()
        mock_task1.execution_mode = MagicMock()

        mock_task2 = MagicMock()
        mock_task2.task_id = 'task_2'

        mock_task3 = MagicMock()
        mock_task3.task_id = 'task_3'

        # Ledger returns tasks in dependency order (1 first, then 2, then 3)
        mock_ledger.get_parallel_executable_tasks.return_value = []
        call_count = [0]
        def next_task():
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_task1
            elif call_count[0] == 2:
                return mock_task2
            elif call_count[0] == 3:
                return mock_task3
            return None
        mock_ledger.get_next_executable_task.side_effect = next_task
        mock_ledger.tasks = {
            'task_1': mock_task1,
            'task_2': mock_task2,
            'task_3': mock_task3,
        }

        q._ledger = mock_ledger
        q._task_map = {
            i1.id: 'task_1',
            i2.id: 'task_2',
            i3.id: 'task_3',
        }

        batch = q.pull_batch()
        self.assertIsNotNone(batch)
        # Should respect ledger ordering
        self.assertEqual(len(batch.instructions), 3)
        texts = [i.text for i in batch.instructions]
        self.assertEqual(texts, ['Step 1: Create API', 'Step 2: Write tests', 'Step 3: Deploy'])

    def test_complete_batch_notifies_ledger(self):
        """complete_batch() should call ledger.complete_task_and_route()."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('complete_user')
        inst = q.enqueue('Task to complete')

        mock_ledger = MagicMock()
        q._ledger = mock_ledger
        q._task_map = {inst.id: 'dynamic_1'}

        batch = q.pull_batch()
        q.complete_batch(batch.batch_id, result='Success')

        mock_ledger.complete_task_and_route.assert_called_once_with(
            'dynamic_1', outcome='success', result='Success',
        )

    def test_fail_batch_notifies_ledger(self):
        """fail_batch() should notify ledger of failure."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('fail_user')
        inst = q.enqueue('Task that fails')

        mock_ledger = MagicMock()
        q._ledger = mock_ledger
        q._task_map = {inst.id: 'dynamic_1'}

        batch = q.pull_batch()
        q.fail_batch(batch.batch_id, 'Compute failed')

        mock_ledger.complete_task_and_route.assert_called_once_with(
            'dynamic_1', outcome='failure', result='Compute failed',
        )

    def test_task_map_persists_via_context(self):
        """ledger_task_id stored in context survives reload."""
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q1 = InstructionQueue('persist_map_user')
        inst = q1.enqueue('Persistent mapping')
        # Simulate ledger registration
        q1._task_map[inst.id] = 'dynamic_42'
        inst.context['ledger_task_id'] = 'dynamic_42'
        q1._save()

        # Reload
        q2 = InstructionQueue('persist_map_user')
        self.assertEqual(q2._task_map.get(inst.id), 'dynamic_42')

    def test_ledger_ordering_fallback_on_error(self):
        """If ledger queries fail, falls back to priority ordering."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('fallback_user')
        q.enqueue('Low', priority=8)
        q.enqueue('High', priority=1)

        mock_ledger = MagicMock()
        mock_ledger.get_parallel_executable_tasks.side_effect = Exception('LLM down')
        mock_ledger.get_next_executable_task.side_effect = Exception('LLM down')
        mock_ledger.tasks = {}

        q._ledger = mock_ledger
        # No task map → should fall through to simple ordering
        batch = q.pull_batch()
        self.assertIsNotNone(batch)
        texts = [i.text for i in batch.instructions]
        self.assertEqual(texts, ['High', 'Low'])


class TestExecutionPlan(unittest.TestCase):
    """Test dependency-aware execution plan with parallel waves."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import integrations.agent_engine.instruction_queue as iq
        self._orig_dir = iq._QUEUE_DIR
        iq._QUEUE_DIR = self.tmpdir

    def tearDown(self):
        import integrations.agent_engine.instruction_queue as iq
        iq._QUEUE_DIR = self._orig_dir
        iq._queues.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_plan_without_ledger_single_wave(self):
        """Without ledger, all instructions go into one wave."""
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('plan_user')
        q.enqueue('Task A')
        q.enqueue('Task B')
        q.enqueue('Task C')

        plan = q.pull_execution_plan()
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.waves), 1)
        self.assertEqual(plan.total_instructions, 3)

    def test_plan_with_independent_tasks_one_wave(self):
        """Independent tasks (no prerequisites) → single parallel wave."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('parallel_user')
        i1 = q.enqueue('Add WiFi API')
        i2 = q.enqueue('Add Bluetooth API')
        i3 = q.enqueue('Add VPN API')

        # Mock ledger with all-independent tasks
        mock_ledger = MagicMock()
        t1, t2, t3 = MagicMock(), MagicMock(), MagicMock()
        t1.task_id, t1.prerequisites, t1.blocked_by, t1.parent_task_id = 'dt1', [], [], None
        t2.task_id, t2.prerequisites, t2.blocked_by, t2.parent_task_id = 'dt2', [], [], None
        t3.task_id, t3.prerequisites, t3.blocked_by, t3.parent_task_id = 'dt3', [], [], None
        mock_ledger.tasks = {'dt1': t1, 'dt2': t2, 'dt3': t3}

        q._ledger = mock_ledger
        q._task_map = {i1.id: 'dt1', i2.id: 'dt2', i3.id: 'dt3'}

        plan = q.pull_execution_plan()
        self.assertIsNotNone(plan)
        # All independent → single wave with all 3
        self.assertEqual(len(plan.waves), 1)
        self.assertEqual(len(plan.waves[0]), 3)

    def test_plan_with_sequential_deps_multiple_waves(self):
        """Sequential dependencies → multiple waves in order."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('seq_user')
        i1 = q.enqueue('Create database schema')
        i2 = q.enqueue('Write API routes')
        i3 = q.enqueue('Write tests')

        # i2 depends on i1, i3 depends on i2
        t1, t2, t3 = MagicMock(), MagicMock(), MagicMock()
        t1.task_id = 'dt1'
        t1.prerequisites, t1.blocked_by, t1.parent_task_id = [], [], None
        t2.task_id = 'dt2'
        t2.prerequisites, t2.blocked_by, t2.parent_task_id = ['dt1'], [], None
        t3.task_id = 'dt3'
        t3.prerequisites, t3.blocked_by, t3.parent_task_id = ['dt2'], [], None

        mock_ledger = MagicMock()
        mock_ledger.tasks = {'dt1': t1, 'dt2': t2, 'dt3': t3}

        q._ledger = mock_ledger
        q._task_map = {i1.id: 'dt1', i2.id: 'dt2', i3.id: 'dt3'}

        plan = q.pull_execution_plan()
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.waves), 3)
        # Wave 0: schema (no deps)
        self.assertEqual(len(plan.waves[0]), 1)
        self.assertEqual(plan.waves[0][0].text, 'Create database schema')
        # Wave 1: routes (depends on schema)
        self.assertEqual(len(plan.waves[1]), 1)
        self.assertEqual(plan.waves[1][0].text, 'Write API routes')
        # Wave 2: tests (depends on routes)
        self.assertEqual(len(plan.waves[2]), 1)
        self.assertEqual(plan.waves[2][0].text, 'Write tests')

    def test_plan_mixed_parallel_and_sequential(self):
        """Mix of independent and dependent tasks → proper wave grouping."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('mixed_user')
        i1 = q.enqueue('Build WiFi module')
        i2 = q.enqueue('Build Bluetooth module')
        i3 = q.enqueue('Integration tests for both')

        # i1, i2 independent; i3 depends on both
        t1, t2, t3 = MagicMock(), MagicMock(), MagicMock()
        t1.task_id = 'dt1'
        t1.prerequisites, t1.blocked_by, t1.parent_task_id = [], [], None
        t2.task_id = 'dt2'
        t2.prerequisites, t2.blocked_by, t2.parent_task_id = [], [], None
        t3.task_id = 'dt3'
        t3.prerequisites = ['dt1', 'dt2']
        t3.blocked_by, t3.parent_task_id = [], None

        mock_ledger = MagicMock()
        mock_ledger.tasks = {'dt1': t1, 'dt2': t2, 'dt3': t3}

        q._ledger = mock_ledger
        q._task_map = {i1.id: 'dt1', i2.id: 'dt2', i3.id: 'dt3'}

        plan = q.pull_execution_plan()
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.waves), 2)
        # Wave 0: WiFi + Bluetooth in parallel
        self.assertEqual(len(plan.waves[0]), 2)
        wave0_texts = {i.text for i in plan.waves[0]}
        self.assertEqual(wave0_texts, {'Build WiFi module', 'Build Bluetooth module'})
        # Wave 1: Integration tests (after both complete)
        self.assertEqual(len(plan.waves[1]), 1)
        self.assertEqual(plan.waves[1][0].text, 'Integration tests for both')

    def test_plan_empty_queue(self):
        """Empty queue returns None."""
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('empty_plan_user')
        plan = q.pull_execution_plan()
        self.assertIsNone(plan)

    def test_plan_marks_instructions_batched(self):
        """All instructions in plan are marked as BATCHED."""
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('batched_plan_user')
        q.enqueue('Task A')
        q.enqueue('Task B')
        plan = q.pull_execution_plan()
        self.assertIsNotNone(plan)
        for inst in q.get_all():
            self.assertEqual(inst.status, InstructionStatus.BATCHED)

    def test_complete_individual_instruction(self):
        """complete_instruction() marks one instruction done."""
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('single_complete_user')
        i1 = q.enqueue('Task A')
        i2 = q.enqueue('Task B')
        plan = q.pull_execution_plan()
        q.complete_instruction(i1.id, result='Done A')
        # i1 done, i2 still batched
        self.assertEqual(q._instructions[i1.id].status, InstructionStatus.DONE)
        self.assertEqual(q._instructions[i2.id].status, InstructionStatus.BATCHED)

    def test_fail_individual_instruction(self):
        """fail_instruction() returns one instruction to queue."""
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )
        q = InstructionQueue('single_fail_user')
        i1 = q.enqueue('Task A')
        plan = q.pull_execution_plan()
        q.fail_instruction(i1.id, 'timeout')
        self.assertEqual(q._instructions[i1.id].status, InstructionStatus.QUEUED)
        self.assertEqual(q._instructions[i1.id].error, 'timeout')

    def test_plan_to_dict(self):
        """ExecutionPlan serialization."""
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('dict_plan_user')
        q.enqueue('Task A')
        q.enqueue('Task B')
        plan = q.pull_execution_plan()
        d = plan.to_dict()
        self.assertEqual(d['wave_count'], 1)
        self.assertEqual(d['total_instructions'], 2)
        self.assertIn('batch_id', d)

    def test_plan_with_blocked_by(self):
        """blocked_by field creates dependency between waves."""
        from unittest.mock import MagicMock
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('blocked_user')
        i1 = q.enqueue('Deploy server')
        i2 = q.enqueue('Run smoke tests')

        t1, t2 = MagicMock(), MagicMock()
        t1.task_id = 'dt1'
        t1.prerequisites, t1.blocked_by, t1.parent_task_id = [], [], None
        t2.task_id = 'dt2'
        t2.prerequisites = []
        t2.blocked_by = ['dt1']  # Blocked by deploy
        t2.parent_task_id = None

        mock_ledger = MagicMock()
        mock_ledger.tasks = {'dt1': t1, 'dt2': t2}
        q._ledger = mock_ledger
        q._task_map = {i1.id: 'dt1', i2.id: 'dt2'}

        plan = q.pull_execution_plan()
        self.assertEqual(len(plan.waves), 2)
        self.assertEqual(plan.waves[0][0].text, 'Deploy server')
        self.assertEqual(plan.waves[1][0].text, 'Run smoke tests')


class TestDrainWithWaves(unittest.TestCase):
    """Test drain_instruction_queue with parallel wave dispatch."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import integrations.agent_engine.instruction_queue as iq
        self._orig_dir = iq._QUEUE_DIR
        iq._QUEUE_DIR = self.tmpdir

    def tearDown(self):
        import integrations.agent_engine.instruction_queue as iq
        iq._QUEUE_DIR = self._orig_dir
        iq._queues.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drain_dispatches_parallel_wave(self):
        """Independent instructions dispatched concurrently."""
        from unittest.mock import patch, MagicMock
        from integrations.agent_engine.instruction_queue import enqueue_instruction

        enqueue_instruction('drain_user', 'Task A')
        enqueue_instruction('drain_user', 'Task B')
        enqueue_instruction('drain_user', 'Task C')

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'OK'}

        call_count = {'n': 0}
        def mock_post(*args, **kwargs):
            call_count['n'] += 1
            return mock_resp

        with patch('integrations.agent_engine.dispatch.requests.post',
                   side_effect=mock_post):
            from integrations.agent_engine.dispatch import drain_instruction_queue
            result = drain_instruction_queue('drain_user')

        self.assertIsNotNone(result)
        # 3 instructions → 3 separate /chat calls (not 1 batch)
        self.assertEqual(call_count['n'], 3)

    def test_drain_sequential_waves_in_order(self):
        """Dependent instructions dispatch in dependency order."""
        from unittest.mock import patch, MagicMock
        from integrations.agent_engine.instruction_queue import (
            get_queue, InstructionQueue,
        )

        q = InstructionQueue('wave_user')
        i1 = q.enqueue('Step 1: Create schema')
        i2 = q.enqueue('Step 2: Write routes')

        # Set up dependency: i2 depends on i1
        t1, t2 = MagicMock(), MagicMock()
        t1.task_id = 'dt1'
        t1.prerequisites, t1.blocked_by, t1.parent_task_id = [], [], None
        t2.task_id = 'dt2'
        t2.prerequisites, t2.blocked_by, t2.parent_task_id = ['dt1'], [], None

        mock_ledger = MagicMock()
        mock_ledger.tasks = {'dt1': t1, 'dt2': t2}
        q._ledger = mock_ledger
        q._task_map = {i1.id: 'dt1', i2.id: 'dt2'}

        # Register the queue in the singleton registry
        import integrations.agent_engine.instruction_queue as iq
        iq._queues['wave_user'] = q

        dispatch_order = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'OK'}

        def mock_post(url, **kwargs):
            prompt = kwargs.get('json', {}).get('prompt', '')
            dispatch_order.append(prompt)
            return mock_resp

        with patch('integrations.agent_engine.dispatch.requests.post',
                   side_effect=mock_post):
            from integrations.agent_engine.dispatch import drain_instruction_queue
            result = drain_instruction_queue('wave_user')

        self.assertIsNotNone(result)
        self.assertEqual(len(dispatch_order), 2)
        # First dispatched must be Step 1 (no deps)
        self.assertIn('Step 1', dispatch_order[0])
        # Second must be Step 2 (depends on Step 1)
        self.assertIn('Step 2', dispatch_order[1])

    def test_drain_empty_queue(self):
        """Empty queue returns None without dispatch."""
        from unittest.mock import patch
        from integrations.agent_engine.dispatch import drain_instruction_queue
        result = drain_instruction_queue('nonexistent_user')
        self.assertIsNone(result)

    def test_drain_partial_failure(self):
        """Some instructions fail, others succeed — returns partial results."""
        from unittest.mock import patch, MagicMock
        from integrations.agent_engine.instruction_queue import enqueue_instruction

        enqueue_instruction('partial_user', 'Good task')
        enqueue_instruction('partial_user', 'Bad task')

        call_n = {'n': 0}
        def mock_post(url, **kwargs):
            call_n['n'] += 1
            resp = MagicMock()
            prompt = kwargs.get('json', {}).get('prompt', '')
            if 'Bad' in prompt:
                resp.status_code = 500
            else:
                resp.status_code = 200
                resp.json.return_value = {'response': 'Success'}
            return resp

        with patch('integrations.agent_engine.dispatch.requests.post',
                   side_effect=mock_post):
            from integrations.agent_engine.dispatch import drain_instruction_queue
            result = drain_instruction_queue('partial_user')

        # Should return partial success
        self.assertIsNotNone(result)
        self.assertIn('Success', result)


class TestConcurrencySafety(unittest.TestCase):
    """Test thread safety and race condition prevention."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import integrations.agent_engine.instruction_queue as iq
        self._orig_dir = iq._QUEUE_DIR
        iq._QUEUE_DIR = self.tmpdir

    def tearDown(self):
        import integrations.agent_engine.instruction_queue as iq
        iq._QUEUE_DIR = self._orig_dir
        iq._queues.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drain_lock_prevents_concurrent_drain(self):
        """Only one drain can run at a time for the same user."""
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('lock_user')

        # First acquire succeeds
        self.assertTrue(q.acquire_drain_lock())
        # Second acquire fails (non-blocking)
        self.assertFalse(q.acquire_drain_lock())
        # Release
        q.release_drain_lock()
        # Now acquire works again
        self.assertTrue(q.acquire_drain_lock())
        q.release_drain_lock()

    def test_drain_lock_file_created(self):
        """Drain lock creates a file for cross-process visibility."""
        from integrations.agent_engine.instruction_queue import InstructionQueue
        q = InstructionQueue('filelock_user')

        self.assertTrue(q.acquire_drain_lock())
        self.assertTrue(os.path.exists(q._drain_lock_path))

        import json as json_mod
        with open(q._drain_lock_path) as f:
            data = json_mod.load(f)
        self.assertEqual(data['pid'], os.getpid())
        self.assertEqual(data['user_id'], 'filelock_user')

        q.release_drain_lock()
        self.assertFalse(os.path.exists(q._drain_lock_path))

    def test_stale_lock_overridden(self):
        """Stale lock from dead process is overridden."""
        from integrations.agent_engine.instruction_queue import InstructionQueue
        import json as json_mod

        q = InstructionQueue('stale_user')
        os.makedirs(os.path.dirname(q._drain_lock_path), exist_ok=True)

        # Write a lock file with a fake PID that doesn't exist
        with open(q._drain_lock_path, 'w') as f:
            json_mod.dump({'pid': 99999999, 'time': time.time(),
                           'user_id': 'stale_user'}, f)

        # Should override dead process lock
        self.assertTrue(q.acquire_drain_lock())
        q.release_drain_lock()

    def test_concurrent_enqueue_thread_safe(self):
        """Multiple threads can enqueue simultaneously without corruption."""
        import threading
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('concurrent_user')
        errors = []

        def enqueue_task(n):
            try:
                q.enqueue(f'Task {n}', priority=n % 5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=enqueue_task, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(q.get_all()), 20)

    def test_atomic_save_survives_read_after(self):
        """Atomic save produces valid JSON readable by new queue instance."""
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q1 = InstructionQueue('atomic_user')
        for i in range(10):
            q1.enqueue(f'Task {i}')

        # Load from same file — should be valid JSON
        q2 = InstructionQueue('atomic_user')
        self.assertEqual(len(q2.get_all()), 10)

    def test_no_temp_file_left_after_save(self):
        """Atomic save cleans up temp file."""
        from integrations.agent_engine.instruction_queue import InstructionQueue

        q = InstructionQueue('temp_user')
        q.enqueue('Task')

        tmp_path = q._queue_path + '.tmp'
        self.assertFalse(os.path.exists(tmp_path))

    def test_pull_plan_and_enqueue_no_double_dispatch(self):
        """Instruction enqueued during drain is not included in current plan."""
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, InstructionStatus,
        )

        q = InstructionQueue('nodupe_user')
        q.enqueue('Original task')

        # Pull plan — marks Original as BATCHED
        plan = q.pull_execution_plan()
        self.assertIsNotNone(plan)
        self.assertEqual(plan.total_instructions, 1)

        # Enqueue new task AFTER plan was pulled
        q.enqueue('Late arrival')

        # Late arrival is QUEUED, not in current plan
        pending = q.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].text, 'Late arrival')

        # Second pull_execution_plan gets only the late arrival
        plan2 = q.pull_execution_plan()
        self.assertIsNotNone(plan2)
        self.assertEqual(plan2.total_instructions, 1)
        self.assertEqual(plan2.waves[0][0].text, 'Late arrival')

    def test_dispatch_drain_skips_when_locked(self):
        """drain_instruction_queue returns None when drain lock is held."""
        from unittest.mock import patch, MagicMock
        from integrations.agent_engine.instruction_queue import (
            InstructionQueue, enqueue_instruction, get_queue,
        )
        import integrations.agent_engine.instruction_queue as iq

        q = InstructionQueue('drain_lock_user')
        q.enqueue('Task')
        iq._queues['drain_lock_user'] = q

        # Hold the drain lock
        self.assertTrue(q.acquire_drain_lock())
        try:
            from integrations.agent_engine.dispatch import drain_instruction_queue
            # Second drain should skip
            result = drain_instruction_queue('drain_lock_user')
            self.assertIsNone(result)

            # Instruction should still be QUEUED (not lost)
            self.assertEqual(len(q.get_pending()), 1)
        finally:
            q.release_drain_lock()


class TestInterfaceExtractor(unittest.TestCase):
    """Test Python interface extraction for shard engine."""

    def test_extract_functions(self):
        from integrations.agent_engine.shard_engine import InterfaceExtractor
        # Extract from this test file itself
        spec = InterfaceExtractor.extract_from_file(__file__)
        self.assertIsNotNone(spec)
        self.assertTrue(len(spec.classes) > 0)

    def test_extract_from_known_file(self):
        from integrations.agent_engine.shard_engine import InterfaceExtractor
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        target = os.path.join(code_root, 'security', 'origin_attestation.py')
        spec = InterfaceExtractor.extract_from_file(target)
        func_names = [f['name'] for f in spec.functions]
        self.assertIn('verify_origin', func_names)
        self.assertIn('compute_origin_fingerprint', func_names)
        # Should NOT include private functions (starting with _)
        for name in func_names:
            self.assertFalse(name.startswith('_') and not name.startswith('__'),
                             f"Private function leaked: {name}")

    def test_extract_preserves_types(self):
        from integrations.agent_engine.shard_engine import InterfaceExtractor
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        target = os.path.join(code_root, 'security', 'origin_attestation.py')
        spec = InterfaceExtractor.extract_from_file(target)
        # verify_origin has return type Dict
        vo = next((f for f in spec.functions if f['name'] == 'verify_origin'), None)
        self.assertIsNotNone(vo)

    def test_extract_nonexistent_file(self):
        from integrations.agent_engine.shard_engine import InterfaceExtractor
        spec = InterfaceExtractor.extract_from_file('/nonexistent/file.py')
        self.assertEqual(spec.functions, [])
        self.assertEqual(spec.classes, [])


class TestShardEngine(unittest.TestCase):
    """Test shard engine task decomposition."""

    def test_create_shard_interfaces_scope(self):
        from integrations.agent_engine.shard_engine import (
            ShardEngine, ShardScope,
        )
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        engine = ShardEngine(code_root)
        shard = engine.create_shard(
            task='Add battery monitoring API',
            target_files=['security/origin_attestation.py'],
            scope=ShardScope.INTERFACES,
        )
        self.assertIsNotNone(shard.shard_id)
        self.assertEqual(len(shard.target_files), 1)
        self.assertEqual(shard.scope, ShardScope.INTERFACES)
        # Should have interface specs, NOT full content
        self.assertTrue(len(shard.interface_specs) > 0)
        self.assertEqual(len(shard.full_content), 0)

    def test_create_shard_full_scope(self):
        from integrations.agent_engine.shard_engine import (
            ShardEngine, ShardScope,
        )
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        engine = ShardEngine(code_root)
        shard = engine.create_shard(
            task='Review code',
            target_files=['security/origin_attestation.py'],
            scope=ShardScope.FULL_FILE,
        )
        self.assertTrue(len(shard.full_content) > 0)
        content = list(shard.full_content.values())[0]
        self.assertIn('verify_origin', content)

    def test_shard_expiry(self):
        from integrations.agent_engine.shard_engine import (
            ShardEngine, ShardScope,
        )
        engine = ShardEngine(shard_ttl=1)
        shard = engine.create_shard(
            task='Quick task',
            target_files=[],
            scope=ShardScope.MINIMAL,
        )
        # Force expiry by backdating
        shard.expires_at = time.time() - 10
        self.assertTrue(shard.is_expired())
        cleaned = engine.cleanup_expired()
        self.assertEqual(cleaned, 1)

    def test_obfuscated_paths(self):
        from integrations.agent_engine.shard_engine import (
            ShardEngine, ShardScope,
        )
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        engine = ShardEngine(code_root)
        shard = engine.create_shard(
            task='Secret task',
            target_files=['security/origin_attestation.py'],
            scope=ShardScope.INTERFACES,
            obfuscate_paths=True,
        )
        self.assertTrue(shard.obfuscated)
        # Paths should be obfuscated
        for tf in shard.target_files:
            self.assertTrue(tf.startswith('module_'), f"Path not obfuscated: {tf}")

    def test_validate_result_unauthorized_file(self):
        from integrations.agent_engine.shard_engine import (
            ShardEngine, ShardScope, ShardResult,
        )
        engine = ShardEngine()
        shard = engine.create_shard(
            task='Modify only one file',
            target_files=['security/origin_attestation.py'],
            scope=ShardScope.INTERFACES,
        )
        # Try to submit a result that modifies an unauthorized file
        result = ShardResult(
            shard_id=shard.shard_id,
            diffs={'security/master_key.py': '+hacked'},
            test_results=None,
            success=True,
        )
        ok, msg = engine.validate_result(shard.shard_id, result)
        self.assertFalse(ok)
        self.assertIn('unauthorized', msg)

    def test_validate_result_success(self):
        from integrations.agent_engine.shard_engine import (
            ShardEngine, ShardScope, ShardResult,
        )
        engine = ShardEngine()
        shard = engine.create_shard(
            task='Modify file',
            target_files=['security/origin_attestation.py'],
            scope=ShardScope.INTERFACES,
        )
        result = ShardResult(
            shard_id=shard.shard_id,
            diffs={'security/origin_attestation.py': '+new line'},
            test_results='OK',
            success=True,
        )
        ok, msg = engine.validate_result(shard.shard_id, result)
        self.assertTrue(ok, msg)

    def test_get_stats(self):
        from integrations.agent_engine.shard_engine import ShardEngine
        engine = ShardEngine()
        stats = engine.get_stats()
        self.assertIn('active_shards', stats)
        self.assertIn('interface_cache_size', stats)

    def test_singleton(self):
        from integrations.agent_engine.shard_engine import get_shard_engine
        e1 = get_shard_engine()
        e2 = get_shard_engine()
        self.assertIs(e1, e2)


if __name__ == '__main__':
    unittest.main()
