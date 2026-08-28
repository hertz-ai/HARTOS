"""
Tests for the ledger overhaul: thread safety, validated transitions,
atomic writes, dependency auto-unblock, state_history, transition_to(),
and backwards compatibility.

Covers:
  - Thread safety (concurrent task creation / status updates)
  - State transition validation (valid + invalid)
  - Atomic JSON writes (write-to-tmp + os.replace)
  - Dependency auto-unblock (full prerequisite chain walk)
  - state_history recording
  - transition_to() method
  - Backwards compat (old code still works)
  - Bug fixes (case mismatch, LLM removal, lifecycle hooks paths)
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure agent-ledger-opensource is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agent-ledger-opensource'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from agent_ledger.core import (
    Task, TaskType, TaskStatus, ExecutionMode, SmartLedger,
)
from agent_ledger.backends import InMemoryBackend, JSONBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ledger(backend=None):
    """Create a fresh SmartLedger with in-memory backend."""
    if backend is None:
        backend = InMemoryBackend()
    return SmartLedger(agent_id="test", session_id="test_session", backend=backend)


def _make_task(task_id="t1", description="Test task",
               task_type=TaskType.PRE_ASSIGNED,
               status=TaskStatus.PENDING, **kwargs):
    return Task(task_id=task_id, description=description,
                task_type=task_type, status=status, **kwargs)


# ===========================================================================
# 1. transition_to() method
# ===========================================================================

class TestTransitionTo(unittest.TestCase):
    """Tests for Task.transition_to() validated state transitions."""

    def test_valid_transition_pending_to_in_progress(self):
        task = _make_task()
        self.assertTrue(task.transition_to(TaskStatus.IN_PROGRESS, "Starting"))
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)

    def test_valid_transition_in_progress_to_completed(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        self.assertTrue(task.transition_to(TaskStatus.COMPLETED, "Done"))
        self.assertEqual(task.status, TaskStatus.COMPLETED)

    def test_invalid_transition_pending_to_completed(self):
        task = _make_task()
        # PENDING -> COMPLETED is not valid (must go through IN_PROGRESS)
        self.assertFalse(task.transition_to(TaskStatus.COMPLETED, "Nope"))
        self.assertEqual(task.status, TaskStatus.PENDING)

    def test_invalid_transition_from_terminal_state(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        task.transition_to(TaskStatus.COMPLETED, "Done")
        # COMPLETED is terminal, cannot go to IN_PROGRESS
        self.assertFalse(task.transition_to(TaskStatus.IN_PROGRESS, "Retry"))
        self.assertEqual(task.status, TaskStatus.COMPLETED)

    def test_transition_records_state_history(self):
        task = _make_task()
        initial_len = len(task.state_history)
        task.transition_to(TaskStatus.IN_PROGRESS, "Start")
        self.assertEqual(len(task.state_history), initial_len + 1)
        entry = task.state_history[-1]
        self.assertEqual(entry["status"], "in_progress")
        self.assertEqual(entry["previous_status"], "pending")
        self.assertIn("timestamp", entry)
        self.assertIn("reason", entry)

    def test_transition_updates_updated_at(self):
        task = _make_task()
        old_ts = task.updated_at
        time.sleep(0.01)
        task.transition_to(TaskStatus.IN_PROGRESS, "Start")
        self.assertNotEqual(task.updated_at, old_ts)

    def test_transition_with_string_status(self):
        task = _make_task()
        self.assertTrue(task.transition_to("in_progress", "Start"))
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)

    def test_transition_with_invalid_string(self):
        task = _make_task()
        self.assertFalse(task.transition_to("not_a_real_status", "Nope"))
        self.assertEqual(task.status, TaskStatus.PENDING)

    def test_transition_default_reason(self):
        task = _make_task()
        task.transition_to(TaskStatus.IN_PROGRESS)
        entry = task.state_history[-1]
        self.assertIn("in_progress", entry["reason"])

    def test_blocked_to_pending_valid(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        task.transition_to(TaskStatus.BLOCKED, "Deps")
        self.assertTrue(task.transition_to(TaskStatus.PENDING, "Unblocked"))
        self.assertEqual(task.status, TaskStatus.PENDING)

    def test_paused_to_resuming_to_in_progress(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        task.transition_to(TaskStatus.PAUSED, "Pausing")
        self.assertTrue(task.transition_to(TaskStatus.RESUMING, "Resuming"))
        self.assertTrue(task.transition_to(TaskStatus.IN_PROGRESS, "Running"))
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)

    def test_completed_to_rolled_back(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        task.transition_to(TaskStatus.COMPLETED, "Done")
        self.assertTrue(task.transition_to(TaskStatus.ROLLED_BACK, "Undo"))
        self.assertEqual(task.status, TaskStatus.ROLLED_BACK)


# ===========================================================================
# 2. State transition validation (via _validate_transition)
# ===========================================================================

class TestStateTransitionValidation(unittest.TestCase):
    """Test that _validate_transition correctly allows/rejects transitions."""

    def test_all_terminal_states_block_transitions(self):
        terminal = [
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
            TaskStatus.TERMINATED, TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE,
            TaskStatus.ROLLED_BACK,
        ]
        for status in terminal:
            task = _make_task(status=status)
            # Should not be able to go to IN_PROGRESS from any terminal state
            self.assertFalse(task._validate_transition(TaskStatus.IN_PROGRESS),
                             f"Expected {status} to block IN_PROGRESS")

    def test_completed_allows_rolled_back(self):
        task = _make_task(status=TaskStatus.COMPLETED)
        self.assertTrue(task._validate_transition(TaskStatus.ROLLED_BACK))

    def test_pending_valid_targets(self):
        task = _make_task()
        valid = [TaskStatus.IN_PROGRESS, TaskStatus.PAUSED, TaskStatus.CANCELLED,
                 TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE, TaskStatus.DEFERRED,
                 TaskStatus.DELEGATED]
        for target in valid:
            t = _make_task()
            self.assertTrue(t._validate_transition(target), f"PENDING -> {target} should be valid")

    def test_pending_invalid_targets(self):
        task = _make_task()
        invalid = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED,
                   TaskStatus.TERMINATED, TaskStatus.RESUMING, TaskStatus.ROLLED_BACK]
        for target in invalid:
            self.assertFalse(task._validate_transition(target),
                             f"PENDING -> {target} should be invalid")


# ===========================================================================
# 3. Thread safety
# ===========================================================================

class TestThreadSafety(unittest.TestCase):
    """Test concurrent access to SmartLedger."""

    def test_concurrent_task_creation(self):
        ledger = _make_ledger()
        errors = []
        count = 50

        def add_tasks(start):
            for i in range(start, start + count):
                try:
                    task = _make_task(task_id=f"task_{i}", description=f"Task {i}")
                    ledger.add_task(task)
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=add_tasks, args=(i * count,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors during concurrent creation: {errors}")
        self.assertEqual(len(ledger.tasks), 4 * count)

    def test_concurrent_status_updates(self):
        ledger = _make_ledger()
        # Create tasks first
        for i in range(20):
            task = _make_task(task_id=f"t_{i}")
            ledger.add_task(task)

        errors = []

        def update_tasks(task_ids):
            for tid in task_ids:
                try:
                    ledger.update_task_status(tid, TaskStatus.IN_PROGRESS,
                                              reason="Starting")
                except Exception as e:
                    errors.append(str(e))

        half = [f"t_{i}" for i in range(10)]
        other_half = [f"t_{i}" for i in range(10, 20)]

        t1 = threading.Thread(target=update_tasks, args=(half,))
        t2 = threading.Thread(target=update_tasks, args=(other_half,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0)
        for i in range(20):
            self.assertEqual(ledger.tasks[f"t_{i}"].status, TaskStatus.IN_PROGRESS)

    def test_concurrent_read_write(self):
        """Readers and writers don't deadlock (RLock allows reentrant locking)."""
        ledger = _make_ledger()
        for i in range(10):
            ledger.add_task(_make_task(task_id=f"t_{i}"))

        results = []
        errors = []

        def reader():
            for _ in range(50):
                try:
                    ledger.get_task("t_0")
                    ledger.get_tasks_by_status(TaskStatus.PENDING)
                    ledger.get_ready_tasks()
                except Exception as e:
                    errors.append(str(e))

        def writer():
            for i in range(10):
                try:
                    ledger.add_task(_make_task(task_id=f"new_{i}"))
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads.append(threading.Thread(target=writer))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent errors: {errors}")


# ===========================================================================
# 4. state_history recording
# ===========================================================================

class TestStateHistory(unittest.TestCase):
    """Verify state_history is recorded correctly on transitions."""

    def test_initial_state_recorded(self):
        task = _make_task()
        self.assertEqual(len(task.state_history), 1)
        self.assertEqual(task.state_history[0]["status"], "pending")
        self.assertEqual(task.state_history[0]["reason"], "Task created")

    def test_multiple_transitions_recorded(self):
        task = _make_task()
        task.transition_to(TaskStatus.IN_PROGRESS, "Start")
        task.transition_to(TaskStatus.COMPLETED, "Done")
        # 1 initial + 2 transitions = 3
        self.assertEqual(len(task.state_history), 3)
        self.assertEqual(task.state_history[1]["status"], "in_progress")
        self.assertEqual(task.state_history[2]["status"], "completed")

    def test_failed_transition_not_recorded(self):
        task = _make_task()
        initial_len = len(task.state_history)
        task.transition_to(TaskStatus.COMPLETED, "Invalid")  # Invalid: PENDING -> COMPLETED
        # Should NOT add entry
        self.assertEqual(len(task.state_history), initial_len)

    def test_history_has_from_and_to(self):
        task = _make_task()
        task.transition_to(TaskStatus.IN_PROGRESS, "Go")
        entry = task.state_history[-1]
        self.assertEqual(entry["previous_status"], "pending")
        self.assertEqual(entry["status"], "in_progress")

    def test_history_timestamps_increase(self):
        task = _make_task()
        time.sleep(0.01)
        task.transition_to(TaskStatus.IN_PROGRESS, "Go")
        ts1 = task.state_history[-1]["timestamp"]
        time.sleep(0.01)
        task.transition_to(TaskStatus.COMPLETED, "Done")
        ts2 = task.state_history[-1]["timestamp"]
        self.assertGreater(ts2, ts1)


# ===========================================================================
# 5. Dependency auto-unblock (full chain walk)
# ===========================================================================

class TestDependencyAutoUnblock(unittest.TestCase):
    """Test that _handle_task_completion walks the full dependency chain."""

    def test_direct_dependent_unblocked(self):
        ledger = _make_ledger()
        t1 = _make_task(task_id="t1")
        ledger.add_task(t1)

        t2 = _make_task(task_id="t2", prerequisites=["t1"])
        t2.add_blocking_task("t1")
        ledger.add_task(t2)

        # Start and complete t1
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        # Block t2
        ledger.update_task_status("t2", TaskStatus.IN_PROGRESS)
        ledger.update_task_status("t2", TaskStatus.BLOCKED, error_message="Waiting for t1")

        ledger.update_task_status("t1", TaskStatus.COMPLETED, result="done")

        # t2 should be auto-resumed (BLOCKED -> RESUMING -> IN_PROGRESS)
        t2_task = ledger.tasks["t2"]
        self.assertEqual(t2_task.status, TaskStatus.IN_PROGRESS)

    def test_chain_unblock_three_levels(self):
        """t1 -> t2 -> t3: completing t1 should cascade unblock."""
        ledger = _make_ledger()

        t1 = _make_task(task_id="t1")
        ledger.add_task(t1)

        t2 = _make_task(task_id="t2", prerequisites=["t1"])
        t2.add_blocking_task("t1")
        ledger.add_task(t2)

        t3 = _make_task(task_id="t3", prerequisites=["t2"])
        t3.add_blocking_task("t2")
        ledger.add_task(t3)

        # Wire dependent_task_ids
        ledger.tasks["t1"].add_dependent_task("t2")
        ledger.tasks["t2"].add_dependent_task("t3")

        # Start and block t2 and t3
        ledger.update_task_status("t2", TaskStatus.IN_PROGRESS)
        ledger.update_task_status("t2", TaskStatus.BLOCKED, error_message="Waiting t1")
        ledger.update_task_status("t3", TaskStatus.IN_PROGRESS)
        ledger.update_task_status("t3", TaskStatus.BLOCKED, error_message="Waiting t2")

        # Complete t1
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.update_task_status("t1", TaskStatus.COMPLETED, result="done")

        # t2 should be auto-resumed
        self.assertEqual(ledger.tasks["t2"].status, TaskStatus.IN_PROGRESS)
        # t3 should still be BLOCKED (blocked_by t2, which is not yet completed)
        # The BFS checks blocked_by — t3 is blocked by t2 which is now IN_PROGRESS, not completed
        self.assertEqual(ledger.tasks["t3"].status, TaskStatus.BLOCKED)

    def test_parent_unblocked_when_all_children_complete(self):
        ledger = _make_ledger()
        parent = _make_task(task_id="parent")
        ledger.add_task(parent)

        # Start parent and block it
        ledger.update_task_status("parent", TaskStatus.IN_PROGRESS)
        ledger.update_task_status("parent", TaskStatus.BLOCKED, error_message="children")

        # Add children
        for i in range(3):
            child = _make_task(task_id=f"child_{i}", parent_task_id="parent")
            ledger.add_task(child)
            ledger.tasks["parent"].add_child_task(f"child_{i}")

        # Complete all children via complete_task_and_route
        for i in range(3):
            ledger.update_task_status(f"child_{i}", TaskStatus.IN_PROGRESS)
            ledger.complete_task_and_route(f"child_{i}", "success", "done")

        # Parent should now be IN_PROGRESS
        self.assertEqual(ledger.tasks["parent"].status, TaskStatus.IN_PROGRESS)


# ===========================================================================
# 6. Atomic JSON writes
# ===========================================================================

class TestAtomicJSONWrites(unittest.TestCase):
    """Verify JSONBackend uses write-to-tmp + os.replace."""

    def test_atomic_write_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            data = {"test": "data", "tasks": {}}
            result = backend.save("test_key", data)
            self.assertTrue(result)

            loaded = backend.load("test_key")
            self.assertEqual(loaded["test"], "data")

    def test_atomic_write_no_tmp_left_behind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            backend.save("key1", {"x": 1})
            # No .tmp files should exist
            tmp_files = list(Path(tmpdir).glob("*.tmp"))
            self.assertEqual(len(tmp_files), 0)

    def test_atomic_write_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            backend.save("key1", {"version": 1})
            backend.save("key1", {"version": 2})
            loaded = backend.load("key1")
            self.assertEqual(loaded["version"], 2)

    def test_save_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            backend.save("key1", {"tasks": {"t1": {"status": "pending"}}})
            path = backend._get_path("key1")
            with open(path, 'r') as f:
                data = json.load(f)
            self.assertIn("tasks", data)


# ===========================================================================
# 7. update_task_status validates transitions (Bug #2)
# ===========================================================================

class TestUpdateTaskStatusValidation(unittest.TestCase):
    """Ensure update_task_status goes through _validate_transition."""

    def test_valid_update(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        result = ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.IN_PROGRESS)

    def test_invalid_update_rejected(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        # PENDING -> COMPLETED is invalid
        result = ledger.update_task_status("t1", TaskStatus.COMPLETED)
        self.assertFalse(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.PENDING)

    def test_update_from_terminal_rejected(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.update_task_status("t1", TaskStatus.COMPLETED)
        # COMPLETED -> IN_PROGRESS is invalid
        result = ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        self.assertFalse(result)

    def test_update_records_state_history(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS, reason="Starting work")
        task = ledger.tasks["t1"]
        self.assertTrue(any(e["reason"] == "Starting work" for e in task.state_history))

    def test_string_status_coercion(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        result = ledger.update_task_status("t1", "in_progress", reason="String status")
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.IN_PROGRESS)

    def test_unknown_string_status_rejected(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        result = ledger.update_task_status("t1", "bogus_status")
        self.assertFalse(result)


# ===========================================================================
# 8. Backwards compatibility
# ===========================================================================

class TestBackwardsCompatibility(unittest.TestCase):
    """Verify old public APIs still work."""

    def test_add_task_and_get_task(self):
        ledger = _make_ledger()
        task = _make_task(task_id="t1")
        self.assertTrue(ledger.add_task(task))
        retrieved = ledger.get_task("t1")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.task_id, "t1")

    def test_complete_task_method(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        result = ledger.complete_task("t1", result="success")
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.COMPLETED)

    def test_fail_task_method(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        result = ledger.fail_task("t1", "something broke")
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.FAILED)

    def test_pause_and_resume_task(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.pause_task("t1", "Taking a break")
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.PAUSED)
        ledger.resume_task("t1", "Back at it")
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.IN_PROGRESS)

    def test_get_ready_tasks(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1", priority=90))
        ledger.add_task(_make_task(task_id="t2", priority=50))
        ready = ledger.get_ready_tasks()
        self.assertEqual(len(ready), 2)
        self.assertEqual(ready[0].task_id, "t1")  # Higher priority first

    def test_get_progress_summary(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        summary = ledger.get_progress_summary()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["pending"], 1)

    def test_task_to_dict_from_dict_roundtrip(self):
        task = _make_task(task_id="rt1")
        task.transition_to(TaskStatus.IN_PROGRESS, "Start")
        d = task.to_dict()
        restored = Task.from_dict(d)
        self.assertEqual(restored.task_id, "rt1")
        self.assertEqual(restored.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(len(restored.state_history), len(task.state_history))

    def test_complete_task_and_route(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        next_task = ledger.complete_task_and_route("t1", "success", "result")
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.COMPLETED)

    def test_cancel_task(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        result = ledger.cancel_task("t1")
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.CANCELLED)


# ===========================================================================
# 9. Bug fix: pubsub uses TaskStatus.value (lowercase)
# ===========================================================================

class TestPubSubCaseMismatch(unittest.TestCase):
    """Verify the pubsub call uses lowercase status values."""

    def test_task_status_completed_is_lowercase(self):
        self.assertEqual(TaskStatus.COMPLETED.value, "completed")
        self.assertEqual(TaskStatus.IN_PROGRESS.value, "in_progress")

    def test_generate_event_uses_lowercase(self):
        """The _generate_event pubsub call should use .value (lowercase)."""
        ledger = _make_ledger()
        mock_pubsub = MagicMock()
        ledger._pubsub = mock_pubsub

        task = _make_task(task_id="t1")
        ledger.add_task(task)
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.update_task_status("t1", TaskStatus.COMPLETED, result="ok")

        # Check the pubsub was called with lowercase values
        if mock_pubsub.publish_task_update.called:
            call_args = mock_pubsub.publish_task_update.call_args
            # new_status should be "completed" (lowercase)
            self.assertEqual(call_args[0][2], "completed")
            self.assertEqual(call_args[0][1], "in_progress")


# ===========================================================================
# 10. Bug fix: LLM client removed from helper_ledger
# ===========================================================================

class TestLLMClientRemoved(unittest.TestCase):
    """Verify get_default_llm_client raises NotImplementedError."""

    def test_helper_ledger_llm_client_raises(self):
        from helper_ledger import get_default_llm_client
        with self.assertRaises(NotImplementedError):
            get_default_llm_client()

    def test_core_llm_client_raises(self):
        ledger = _make_ledger()
        with self.assertRaises(NotImplementedError):
            ledger._get_default_llm_client()


# ===========================================================================
# 11. Sequential tasks use metadata instead of invalid transitions
# ===========================================================================

class TestSequentialTasks(unittest.TestCase):
    """Sequential task creation should NOT use invalid PENDING -> BLOCKED."""

    def test_sequential_tasks_stay_pending(self):
        ledger = _make_ledger()
        tasks = ledger.create_sequential_tasks(
            ["Step 1", "Step 2", "Step 3"],
            task_type=TaskType.PRE_ASSIGNED,
        )
        self.assertEqual(len(tasks), 3)
        # First task: PENDING (no prereqs)
        self.assertEqual(tasks[0].status, TaskStatus.PENDING)
        # Second task: PENDING (has prereqs but stays PENDING with metadata)
        self.assertEqual(tasks[1].status, TaskStatus.PENDING)
        self.assertIn(tasks[0].task_id, tasks[1].blocked_by)
        self.assertEqual(tasks[1].pending_reason, "awaiting_prerequisites")
        # Third task
        self.assertEqual(tasks[2].status, TaskStatus.PENDING)
        self.assertIn(tasks[1].task_id, tasks[2].blocked_by)

    def test_sequential_tasks_have_prerequisites(self):
        ledger = _make_ledger()
        tasks = ledger.create_sequential_tasks(["A", "B", "C"])
        self.assertEqual(len(tasks[0].prerequisites), 0)
        self.assertIn(tasks[0].task_id, tasks[1].prerequisites)
        self.assertIn(tasks[1].task_id, tasks[2].prerequisites)


# ===========================================================================
# 12. Deferred, delegated, rollback
# ===========================================================================

class TestAdvancedTransitions(unittest.TestCase):
    """Test deferred, delegated, rollback transitions."""

    def test_defer_task(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        result = ledger.defer_task("t1", "Later", until="2026-12-31")
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.DEFERRED)

    def test_delegate_task(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        result = ledger.delegate_task("t1", "agent_B")
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.DELEGATED)

    def test_rollback_task(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task("t1", result="v1")
        result = ledger.rollback_task("t1", "Oops")
        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.ROLLED_BACK)
        self.assertEqual(ledger.tasks["t1"].original_result, "v1")


# ===========================================================================
# 13. Retry via complete_task_and_route
# ===========================================================================

class TestRetryMechanism(unittest.TestCase):
    """Test retry mechanism in complete_task_and_route."""

    def test_retry_resets_to_pending(self):
        ledger = _make_ledger()
        task = _make_task(task_id="t1")
        task.max_retries = 3
        ledger.add_task(task)
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)

        # First failure should retry (not go to FAILED)
        ledger.complete_task_and_route("t1", "failure", "error1")
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.PENDING)
        self.assertEqual(ledger.tasks["t1"].retry_count, 1)

    def test_max_retries_goes_to_failed(self):
        ledger = _make_ledger()
        task = _make_task(task_id="t1")
        task.max_retries = 1
        ledger.add_task(task)

        # First attempt
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task_and_route("t1", "failure", "error1")
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.PENDING)

        # Second attempt — exceeds max_retries
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task_and_route("t1", "failure", "error2")
        self.assertEqual(ledger.tasks["t1"].status, TaskStatus.FAILED)


# ===========================================================================
# 14. Graph state machine consistency (Bug #3)
# ===========================================================================

class TestGraphStateMachineConsistency(unittest.TestCase):
    """Verify TaskStateMachine.TRANSITIONS matches Task._validate_transition."""

    def test_transitions_match(self):
        from agent_ledger.graph import TaskStateMachine
        for from_status, allowed_list in TaskStateMachine.TRANSITIONS.items():
            for target in allowed_list:
                task = _make_task(status=from_status)
                is_valid = task._validate_transition(target)
                self.assertTrue(is_valid,
                    f"Graph says {from_status} -> {target} is valid, "
                    f"but _validate_transition disagrees")


# ===========================================================================
# 15. Lifecycle hooks: BLOCKED -> PENDING -> IN_PROGRESS path
# ===========================================================================

class TestLifecycleHooksPaths(unittest.TestCase):
    """Verify lifecycle_hooks uses proper transition paths."""

    def test_blocked_to_in_progress_path(self):
        """BLOCKED should go through PENDING before IN_PROGRESS."""
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        task.transition_to(TaskStatus.BLOCKED, "Deps")
        self.assertEqual(task.status, TaskStatus.BLOCKED)

        # Simulate the lifecycle_hooks path: BLOCKED -> PENDING -> IN_PROGRESS
        self.assertTrue(task.transition_to(TaskStatus.PENDING, "Unblocked"))
        self.assertTrue(task.transition_to(TaskStatus.IN_PROGRESS, "Resumed"))
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)

    def test_paused_to_in_progress_via_resume(self):
        """PAUSED should go through RESUMING -> IN_PROGRESS via resume()."""
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        task.transition_to(TaskStatus.PAUSED, "Pausing")
        self.assertEqual(task.status, TaskStatus.PAUSED)
        success = task.resume("Back")
        self.assertTrue(success)
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)


# ===========================================================================
# 16. Miscellaneous edge cases
# ===========================================================================

class TestEdgeCases(unittest.TestCase):
    """Additional edge case tests."""

    def test_add_duplicate_task_rejected(self):
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1"))
        result = ledger.add_task(_make_task(task_id="t1"))
        self.assertFalse(result)

    def test_add_task_over_terminal_existing_replaces_it(self):
        """2026-08-28: a deterministic ledger key (agent_id, session_id) is
        reused across a process restart -- add_task's collision guard used to
        block ANY re-add unconditionally, so a long-dead terminal task (e.g.
        one abandoned via ActionState.GAVE_UP, see test_gave_up_state.py)
        silently kept its stale description forever and the caller's real,
        brand-new task for that id was discarded. This is the mechanism
        behind weeks of "agent 8888 has weird pre-existing state" reports --
        a genuinely NEW message must win over a dead old task, while a
        still-active (non-terminal) one must still be protected."""
        ledger = _make_ledger()
        old = _make_task(task_id="t1", description="stale goal from a dead session",
                          status=TaskStatus.IN_PROGRESS)
        ledger.add_task(old)
        old.transition_to(TaskStatus.FAILED, "GAVE_UP: abandoned")

        new = _make_task(task_id="t1", description="brand new message")
        result = ledger.add_task(new)

        self.assertTrue(result)
        self.assertEqual(ledger.tasks["t1"].description, "brand new message")

    def test_add_task_over_non_terminal_existing_still_rejected(self):
        """The protective half of the same change: a genuinely in-flight
        task (e.g. mid-way through a real multi-action recipe) must still
        block a collision -- only a TERMINAL existing task is safe to
        replace."""
        ledger = _make_ledger()
        ledger.add_task(_make_task(task_id="t1", status=TaskStatus.IN_PROGRESS))

        result = ledger.add_task(_make_task(task_id="t1", description="different"))

        self.assertFalse(result)
        self.assertEqual(ledger.tasks["t1"].description, "Test task")

    def test_update_nonexistent_task(self):
        ledger = _make_ledger()
        result = ledger.update_task_status("ghost", TaskStatus.IN_PROGRESS)
        self.assertFalse(result)

    def test_empty_ledger_progress(self):
        ledger = _make_ledger()
        summary = ledger.get_progress_summary()
        self.assertEqual(summary["total"], 0)

    def test_task_is_terminal(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        self.assertFalse(task.is_terminal())
        task.transition_to(TaskStatus.COMPLETED, "Done")
        self.assertTrue(task.is_terminal())

    def test_task_is_resumable(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        self.assertFalse(task.is_resumable())
        task.transition_to(TaskStatus.PAUSED, "Break")
        self.assertTrue(task.is_resumable())


if __name__ == "__main__":
    unittest.main()
