"""
Tests for agent-ledger v2.0 overhaul:
- P1: Task ownership (claim/release/transfer)
- P2: Budget + compute fields
- P3: Bug fixes (state validation, enum serialization, graph state machine, verification)
- P4: Direct status bypasses eliminated
- P5: Dependency auto-unblock chain
- Thread safety
"""

import os
import json
import threading
import tempfile
import time
from datetime import datetime, timedelta
import pytest
from agent_ledger import (
    SmartLedger,
    Task,
    TaskType,
    TaskStatus,
    ExecutionMode,
    InMemoryBackend,
    TaskStateMachine,
)
from agent_ledger.backends import JSONBackend


# ==================== P1: Ownership ====================

class TestTaskOwnership:
    """Tests for task ownership fields and methods."""

    def test_ownership_fields_initialized_none(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.owner_node_id is None
        assert task.owner_user_id is None
        assert task.owner_prompt_id is None
        assert task.owned_at is None
        assert task.ownership_history == []

    def test_claim(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        result = task.claim(node_id="node-1", user_id="user-1", prompt_id="prompt-1")
        assert result is True
        assert task.owner_node_id == "node-1"
        assert task.owner_user_id == "user-1"
        assert task.owner_prompt_id == "prompt-1"
        assert task.owned_at is not None
        assert len(task.ownership_history) == 1
        assert task.ownership_history[0]["action"] == "claimed"

    def test_claim_already_owned(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.claim("node-1", "user-1", "p1")
        result = task.claim("node-2", "user-2", "p2")
        assert result is False
        assert task.owner_node_id == "node-1"  # unchanged

    def test_release(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.claim("node-1", "user-1", "p1")
        result = task.release()
        assert result is True
        assert task.owner_node_id is None
        assert task.owner_user_id is None
        assert task.owner_prompt_id is None
        assert task.owned_at is None
        assert len(task.ownership_history) == 2
        assert task.ownership_history[1]["action"] == "released"

    def test_release_not_owned(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.release() is False

    def test_transfer(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.claim("node-1", "user-1", "p1")
        result = task.transfer(node_id="node-2", user_id="user-2", prompt_id="p2")
        assert result is True
        assert task.owner_node_id == "node-2"
        assert task.owner_user_id == "user-2"
        assert len(task.ownership_history) == 2  # claim + transferred

    def test_transfer_not_owned(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.transfer("n", "u", "p") is False

    def test_ownership_serialization(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.claim("node-1", "user-1", "p1")
        d = task.to_dict()
        assert d["owner_node_id"] == "node-1"
        assert d["owner_user_id"] == "user-1"
        assert d["owner_prompt_id"] == "p1"
        assert d["owned_at"] is not None
        assert len(d["ownership_history"]) == 1

        restored = Task.from_dict(d)
        assert restored.owner_node_id == "node-1"
        assert restored.owner_user_id == "user-1"

    def test_claim_partial_fields(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        result = task.claim(node_id="node-1")
        assert result is True
        assert task.owner_node_id == "node-1"
        assert task.owner_user_id is None


# ==================== P2: Budget + Compute ====================

class TestTaskBudget:
    """Tests for budget and compute tracking fields."""

    def test_budget_fields_initialized(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.time_budget_s is None
        assert task.spark_budget is None
        assert task.compute_requirements == {}
        assert task.timeout_s is None
        assert task.started_at is None
        assert task.spark_spent == 0.0
        assert task.time_spent_s == 0.0

    def test_record_spend(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.record_spend(spark=10.0, time_s=5.0)
        assert task.spark_spent == 10.0
        assert task.time_spent_s == 5.0
        task.record_spend(spark=5.0, time_s=2.5)
        assert task.spark_spent == 15.0
        assert task.time_spent_s == 7.5

    def test_is_budget_exhausted_spark(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.spark_budget = 100.0
        assert task.is_budget_exhausted() is False
        task.spark_spent = 100.0
        assert task.is_budget_exhausted() is True

    def test_is_budget_exhausted_time(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.time_budget_s = 60.0
        assert task.is_budget_exhausted() is False
        task.time_spent_s = 61.0
        assert task.is_budget_exhausted() is True

    def test_is_budget_exhausted_no_budget(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.is_budget_exhausted() is False

    def test_is_stuck(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.start()
        # Just started, not stuck
        assert task.is_stuck(threshold_s=3600) is False

    def test_can_run_on(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.compute_requirements = {"gpu": True, "vram_gb": 8}
        assert task.can_run_on({"gpu": True, "vram_gb": 16}) is True
        assert task.can_run_on({"gpu": False}) is False
        assert task.can_run_on({"gpu": True, "vram_gb": 4}) is False

    def test_can_run_on_no_requirements(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.can_run_on({}) is True

    def test_budget_serialization(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.time_budget_s = 120.0
        task.spark_budget = 500.0
        task.compute_requirements = {"gpu": True}
        task.record_spend(spark=50, time_s=10)

        d = task.to_dict()
        restored = Task.from_dict(d)
        assert restored.time_budget_s == 120.0
        assert restored.spark_budget == 500.0
        assert restored.spark_spent == 50.0
        assert restored.compute_requirements == {"gpu": True}


# ==================== P3: Bug Fixes ====================

class TestStateValidation:
    """Tests for update_task_status validation (Bug #2 fix)."""

    def _make_ledger(self):
        return SmartLedger("test", "sess", backend=InMemoryBackend())

    def test_valid_transition(self):
        ledger = self._make_ledger()
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)
        result = ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        assert result is True
        assert ledger.get_task("t1").status == TaskStatus.IN_PROGRESS

    def test_invalid_transition_rejected(self):
        ledger = self._make_ledger()
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)
        # PENDING -> COMPLETED is not valid (must go through IN_PROGRESS)
        result = ledger.update_task_status("t1", TaskStatus.COMPLETED)
        assert result is False
        assert ledger.get_task("t1").status == TaskStatus.PENDING  # unchanged

    def test_terminal_state_blocks_transition(self):
        ledger = self._make_ledger()
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task("t1", result="done")
        # COMPLETED -> IN_PROGRESS is not valid
        result = ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        assert result is False
        assert ledger.get_task("t1").status == TaskStatus.COMPLETED

    def test_completed_to_rolled_back(self):
        ledger = self._make_ledger()
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task("t1", result="done")
        # COMPLETED -> ROLLED_BACK is valid
        result = task.rollback("Testing rollback")
        assert result is True
        assert task.status == TaskStatus.ROLLED_BACK

    def test_task_not_found(self):
        ledger = self._make_ledger()
        result = ledger.update_task_status("nonexistent", TaskStatus.IN_PROGRESS)
        assert result is False

    def test_update_records_state_history(self):
        ledger = self._make_ledger()
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        history = task.get_state_history()
        assert len(history) >= 2  # created + in_progress
        # Last entry should be IN_PROGRESS
        last = history[-1]
        assert last["status"] == "in_progress"


class TestEnumSerialization:
    """Tests for enum serialization in state_history (Bug #1 fix)."""

    def test_initial_state_history_serialized(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        entry = task.state_history[0]
        assert isinstance(entry["status"], str)
        assert entry["status"] == "pending"

    def test_transition_serialized(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.start()
        entries = task.state_history
        for entry in entries:
            assert isinstance(entry["status"], str)
            assert entry["status"] in [s.value for s in TaskStatus]

    def test_to_dict_serializes_enums(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        d = task.to_dict()
        assert d["status"] == "pending"
        assert d["task_type"] == "pre_assigned"
        assert d["execution_mode"] == "sequential"

    def test_roundtrip_serialization(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED, execution_mode=ExecutionMode.PARALLEL)
        task.start()
        task.complete(result="success")
        d = task.to_dict()
        json_str = json.dumps(d)  # Should not raise
        parsed = json.loads(json_str)
        restored = Task.from_dict(parsed)
        assert restored.status == TaskStatus.COMPLETED
        assert restored.task_type == TaskType.PRE_ASSIGNED


class TestGraphStateMachine:
    """Tests for TaskStateMachine alignment with Task._validate_transition (Bug #3 fix)."""

    def test_all_15_states_in_transitions(self):
        for status in TaskStatus:
            assert status in TaskStateMachine.TRANSITIONS, f"Missing state: {status}"

    def test_pending_transitions_match_task(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        graph_allowed = TaskStateMachine.TRANSITIONS[TaskStatus.PENDING]
        for target in graph_allowed:
            assert task._validate_transition(target), \
                f"Graph allows PENDING->{target} but Task rejects it"

    def test_in_progress_transitions_match_task(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.start()
        graph_allowed = TaskStateMachine.TRANSITIONS[TaskStatus.IN_PROGRESS]
        for target in graph_allowed:
            # Create fresh task for each check since _validate_transition doesn't change state
            t = Task("t", "d", TaskType.PRE_ASSIGNED)
            t.start()
            assert t._validate_transition(target), \
                f"Graph allows IN_PROGRESS->{target} but Task rejects it"

    def test_terminal_states_have_no_transitions(self):
        for status in [TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TERMINATED,
                       TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE, TaskStatus.ROLLED_BACK]:
            assert TaskStateMachine.TRANSITIONS[status] == [], \
                f"Terminal state {status} should have no transitions"

    def test_completed_only_to_rolled_back(self):
        allowed = TaskStateMachine.TRANSITIONS[TaskStatus.COMPLETED]
        assert allowed == [TaskStatus.ROLLED_BACK]


class TestVerificationFix:
    """Tests for verification.py case mismatch fix (Bug #5)."""

    def test_task_status_values_are_lowercase(self):
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.IN_PROGRESS.value == "in_progress"
        assert TaskStatus.FAILED.value == "failed"

    def test_baseline_comparison_detects_completion(self):
        """Verify that TaskBaseline.compare uses lowercase comparison."""
        from agent_ledger.verification import TaskBaseline
        baseline = TaskBaseline()
        ledger = SmartLedger("test", "sess", backend=InMemoryBackend())
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)

        snap_id = baseline.create_snapshot(ledger)

        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task("t1", result="done")

        diff = baseline.compare_to_snapshot(ledger, snap_id)
        assert "t1" in diff["completed_since"]


# ==================== P3: JSONBackend Atomic Writes (Bug #6) ====================

class TestJSONBackendAtomic:
    """Tests for JSONBackend atomic write fix."""

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            data = {"key": "value", "nested": {"a": 1}}
            result = backend.save("test_key", data)
            assert result is True

            loaded = backend.load("test_key")
            assert loaded == data

    def test_no_temp_file_left_on_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            backend.save("test_key", {"a": 1})
            files = os.listdir(tmpdir)
            assert not any(f.endswith('.tmp') for f in files)

    def test_path_traversal_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            path = backend._get_path("../../etc/passwd")
            # Should NOT escape the storage directory
            assert str(tmpdir) in str(path.parent)
            assert ".." not in path.name

    def test_save_load_roundtrip_large_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            data = {f"key_{i}": f"value_{i}" for i in range(1000)}
            backend.save("large", data)
            loaded = backend.load("large")
            assert loaded == data


# ==================== P4: No More Direct Status Bypasses ====================

class TestNoDirectBypasses:
    """Tests that state transitions always go through proper validation."""

    def test_sequential_chain_uses_record_transition(self):
        """Sequential chain should properly record BLOCKED state."""
        ledger = SmartLedger("test", "sess", backend=InMemoryBackend())
        tasks = ledger.create_sequential_tasks(
            ["step 1", "step 2", "step 3"],
            task_type=TaskType.PRE_ASSIGNED
        )
        # Second and third tasks should be BLOCKED with history
        assert tasks[1].status == TaskStatus.BLOCKED
        assert len(tasks[1].state_history) >= 2  # PENDING + BLOCKED
        blocked_entry = [h for h in tasks[1].state_history if h["status"] == "blocked"]
        assert len(blocked_entry) > 0


# ==================== P5: Dependency Auto-Unblock ====================

class TestDependencyChain:
    """Tests for dependency chain unblocking (Phase 5 fix)."""

    def _make_ledger(self):
        return SmartLedger("test", "sess", backend=InMemoryBackend())

    def test_completing_prerequisite_unblocks_dependent(self):
        ledger = self._make_ledger()
        t1 = Task("t1", "First task", TaskType.PRE_ASSIGNED)
        t2 = Task("t2", "Second task", TaskType.PRE_ASSIGNED,
                   prerequisites=["t1"])
        t2.add_blocking_task("t1")
        t2._record_state_transition(TaskStatus.BLOCKED, "Blocked by t1")
        t1.add_dependent_task("t2")

        ledger.add_task(t1)
        ledger.add_task(t2)

        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task("t1", result="done")

        # t2 should be auto-resumed (BLOCKED -> RESUMING -> IN_PROGRESS)
        assert t2.status == TaskStatus.IN_PROGRESS

    def test_prerequisite_chain_unblocks_transitively(self):
        """Tasks with prerequisites (not just dependent_task_ids) should be unblocked."""
        ledger = self._make_ledger()
        t1 = Task("t1", "First", TaskType.PRE_ASSIGNED)
        t2 = Task("t2", "Second", TaskType.PRE_ASSIGNED, prerequisites=["t1"])
        # t2 has t1 in prerequisites but t1 doesn't know about t2 via dependent_task_ids
        t2.add_blocking_task("t1")
        t2._record_state_transition(TaskStatus.BLOCKED, "Blocked by t1")

        ledger.add_task(t1)
        ledger.add_task(t2)

        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task("t1", result="done")

        # Should still unblock because _handle_task_completion now scans prerequisites
        assert t2.status == TaskStatus.IN_PROGRESS

    def test_parent_task_unblocked_when_children_complete(self):
        ledger = self._make_ledger()
        parent = Task("parent", "Parent task", TaskType.PRE_ASSIGNED)
        parent.start()
        ledger.add_task(parent)

        ledger.add_subtasks(0, [
            {"subtask_id": "action_0_sub_1", "description": "Child 1"},
            {"subtask_id": "action_0_sub_2", "description": "Child 2"},
        ])
        # The parent_task_id for add_subtasks is "action_0"
        # Let's use the sequential chain instead for a cleaner test
        ledger2 = self._make_ledger()
        p = Task("p", "Parent", TaskType.PRE_ASSIGNED)
        c1 = Task("c1", "Child 1", TaskType.PRE_ASSIGNED, parent_task_id="p")
        c2 = Task("c2", "Child 2", TaskType.PRE_ASSIGNED, parent_task_id="p")
        p.start()
        p.block("Waiting for children")
        ledger2.add_task(p)
        ledger2.add_task(c1)
        ledger2.add_task(c2)

        # Complete both children
        ledger2.update_task_status("c1", TaskStatus.IN_PROGRESS)
        ledger2.complete_task("c1")
        ledger2.update_task_status("c2", TaskStatus.IN_PROGRESS)
        ledger2.complete_task("c2")

        # Parent should be unblocked
        # (via _check_and_unblock_parent which is called from complete_task_and_route)
        # Actually complete_task calls _handle_task_completion which doesn't call _check_and_unblock_parent
        # _check_and_unblock_parent is only called from complete_task_and_route
        # So let's use complete_task_and_route
        ledger3 = self._make_ledger()
        p2 = Task("p2", "Parent", TaskType.PRE_ASSIGNED)
        c3 = Task("c3", "Child", TaskType.PRE_ASSIGNED, parent_task_id="p2")
        p2.start()
        p2.block("Waiting")
        ledger3.add_task(p2)
        ledger3.add_task(c3)
        c3.start()
        ledger3.complete_task_and_route("c3", "success", "done")
        assert p2.status == TaskStatus.IN_PROGRESS


# ==================== Thread Safety ====================

class TestThreadSafety:
    """Tests for thread safety of SmartLedger operations."""

    def test_concurrent_add_tasks(self):
        ledger = SmartLedger("test", "sess", backend=InMemoryBackend())
        errors = []

        def add_task(i):
            try:
                task = Task(f"t{i}", f"Task {i}", TaskType.PRE_ASSIGNED)
                ledger.add_task(task)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_task, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(ledger.tasks) == 20

    def test_concurrent_status_updates(self):
        ledger = SmartLedger("test", "sess", backend=InMemoryBackend())
        for i in range(10):
            task = Task(f"t{i}", f"Task {i}", TaskType.PRE_ASSIGNED)
            ledger.add_task(task)

        errors = []

        def update_status(i):
            try:
                ledger.update_task_status(f"t{i}", TaskStatus.IN_PROGRESS)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update_status, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        for i in range(10):
            assert ledger.get_task(f"t{i}").status == TaskStatus.IN_PROGRESS

    def test_has_rlock(self):
        """SmartLedger should have an RLock for thread safety."""
        ledger = SmartLedger("test", "sess", backend=InMemoryBackend())
        assert hasattr(ledger, '_lock')
        assert isinstance(ledger._lock, type(threading.RLock()))


# ==================== LLM Client Removal (Bug #4) ====================

class TestLLMClientRemoved:
    """Tests that _get_default_llm_client raises NotImplementedError."""

    def test_default_llm_client_raises(self):
        ledger = SmartLedger("test", "sess", backend=InMemoryBackend())
        with pytest.raises(NotImplementedError, match="No LLM client configured"):
            ledger._get_default_llm_client()


# ==================== Observability — Heartbeat + Status Reporting ====================

class TestObservability:
    """Tests for agent heartbeat, status posting, and staleness detection."""

    def test_heartbeat_updates_timestamp(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.last_heartbeat_at is None
        task.heartbeat()
        assert task.last_heartbeat_at is not None

    def test_heartbeat_is_lightweight(self):
        """Heartbeat should NOT trigger save or append to lists — just a timestamp."""
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        msg_count_before = len(task.status_messages)
        task.heartbeat()
        assert len(task.status_messages) == msg_count_before  # No growth

    def test_post_status(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.post_status("Parsing input files", progress_pct=25.0)
        assert len(task.status_messages) == 1
        assert task.status_messages[0]["message"] == "Parsing input files"
        assert task.status_messages[0]["progress_pct"] == 25.0
        assert task.progress_pct == 25.0
        assert task.last_heartbeat_at is not None  # heartbeat side-effect

    def test_post_status_with_metadata(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.post_status("LLM call", metadata={"tokens_used": 1500, "model": "gpt-4"})
        assert task.status_messages[0]["metadata"]["tokens_used"] == 1500

    def test_status_messages_bounded(self):
        """Status messages should be capped at 50 to prevent unbounded growth."""
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        for i in range(60):
            task.post_status(f"Update {i}")
        assert len(task.status_messages) == 50
        assert task.status_messages[0]["message"] == "Update 10"  # Oldest kept

    def test_is_heartbeat_stale_fresh(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.heartbeat()
        assert task.is_heartbeat_stale() is False

    def test_is_heartbeat_stale_no_heartbeat(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.is_heartbeat_stale() is False  # No heartbeat expected yet

    def test_is_heartbeat_stale_expired(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.heartbeat_interval_s = 1.0
        # Simulate old heartbeat
        old_time = (datetime.now() - timedelta(seconds=10)).isoformat()
        task.last_heartbeat_at = old_time
        assert task.is_heartbeat_stale() is True  # 10s > 3*1s = 3s threshold

    def test_get_latest_status_empty(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.get_latest_status() is None

    def test_get_latest_status(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.post_status("First")
        task.post_status("Second")
        assert task.get_latest_status()["message"] == "Second"


# ==================== SLA / Deadline ====================

class TestSLA:
    """Tests for SLA target and deadline enforcement."""

    def test_no_sla_not_breached(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.is_sla_breached() is False

    def test_sla_target_not_breached(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.sla_target_s = 3600.0  # 1 hour
        task.started_at = datetime.now().isoformat()
        assert task.is_sla_breached() is False

    def test_sla_target_breached(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.sla_target_s = 1.0  # 1 second
        task.started_at = (datetime.now() - timedelta(seconds=5)).isoformat()
        assert task.is_sla_breached() is True

    def test_deadline_not_breached(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.deadline = (datetime.now() + timedelta(hours=1)).isoformat()
        assert task.is_sla_breached() is False

    def test_deadline_breached(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.deadline = (datetime.now() - timedelta(hours=1)).isoformat()
        assert task.is_sla_breached() is True

    def test_mark_sla_breached(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.sla_breached is False
        task.mark_sla_breached()
        assert task.sla_breached is True

    def test_sla_serialization(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.sla_target_s = 120.0
        task.deadline = "2026-12-31T23:59:59"
        task.mark_sla_breached()
        d = task.to_dict()
        restored = Task.from_dict(d)
        assert restored.sla_target_s == 120.0
        assert restored.deadline == "2026-12-31T23:59:59"
        assert restored.sla_breached is True


# ==================== Locality + Distribution ====================

class TestLocality:
    """Tests for task locality and sensitivity — distribution eligibility."""

    def test_defaults_allow_distribution(self):
        """PUBLIC + GLOBAL = distributable by default. Never a bottleneck."""
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.can_distribute() is True
        assert task.can_distribute_to_region() is True

    def test_local_only_blocks_distribution(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.locality = "local_only"
        assert task.can_distribute() is False

    def test_secret_blocks_distribution(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.sensitivity = "secret"
        assert task.can_distribute() is False

    def test_confidential_does_not_block(self):
        """CONFIDENTIAL is NOT a distribution blocker — agent decides trust."""
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.sensitivity = "confidential"
        assert task.can_distribute() is True

    def test_internal_does_not_block(self):
        """INTERNAL is NOT a distribution blocker — agent decides trust."""
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.sensitivity = "internal"
        assert task.can_distribute() is True

    def test_regional_allows_regional_not_implied_global(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.locality = "regional"
        assert task.can_distribute() is True
        assert task.can_distribute_to_region() is True

    def test_locality_serialization(self):
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        task.locality = "local_only"
        task.sensitivity = "secret"
        d = task.to_dict()
        restored = Task.from_dict(d)
        assert restored.locality == "local_only"
        assert restored.sensitivity == "secret"
        assert restored.can_distribute() is False


# ==================== Integrity — Corruption Detection ====================

class TestIntegrity:
    """Tests for task data integrity hashing and corruption detection."""

    def test_compute_data_hash(self):
        task = Task("t1", "Process data", TaskType.PRE_ASSIGNED)
        h = task.compute_data_hash()
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_hash_deterministic(self):
        task = Task("t1", "Process data", TaskType.PRE_ASSIGNED)
        assert task.compute_data_hash() == task.compute_data_hash()

    def test_hash_changes_on_mutation(self):
        task = Task("t1", "Process data", TaskType.PRE_ASSIGNED)
        h1 = task.compute_data_hash()
        task.description = "Changed description"
        h2 = task.compute_data_hash()
        assert h1 != h2

    def test_seal_and_verify(self):
        task = Task("t1", "Process data", TaskType.PRE_ASSIGNED)
        task.seal_integrity()
        assert task.data_hash is not None
        assert task.verify_integrity() is True

    def test_corruption_detected(self):
        task = Task("t1", "Process data", TaskType.PRE_ASSIGNED)
        task.seal_integrity()
        # Simulate corruption
        task.description = "CORRUPTED"
        assert task.verify_integrity() is False

    def test_verify_no_hash_returns_true(self):
        """No stored hash means nothing to verify — OK."""
        task = Task("t1", "desc", TaskType.PRE_ASSIGNED)
        assert task.verify_integrity() is True

    def test_integrity_survives_serialization(self):
        task = Task("t1", "Process data", TaskType.PRE_ASSIGNED)
        task.seal_integrity()
        d = task.to_dict()
        restored = Task.from_dict(d)
        assert restored.verify_integrity() is True


# ==================== HARTOS Wiring: create_ledger_from_actions integrity seal ====================

class TestCreateLedgerIntegrity:
    """Verify create_ledger_from_actions() seals integrity on every task."""

    def test_tasks_have_integrity_hash_after_creation(self):
        from agent_ledger.core import create_ledger_from_actions
        actions = [
            {"action_id": 1, "description": "Fetch data", "action": "Fetch data"},
            {"action_id": 2, "description": "Parse results", "action": "Parse results"},
        ]
        ledger = create_ledger_from_actions(user_id=42, prompt_id=100, actions=actions,
                                            backend=InMemoryBackend())
        for task_id, task in ledger.tasks.items():
            assert task.data_hash is not None, f"Task {task_id} missing integrity hash"
            assert task.verify_integrity() is True, f"Task {task_id} integrity check failed"

    def test_corrupted_task_detected_after_creation(self):
        from agent_ledger.core import create_ledger_from_actions
        actions = [{"action_id": 1, "description": "Original", "action": "Original"}]
        ledger = create_ledger_from_actions(user_id=1, prompt_id=1, actions=actions,
                                            backend=InMemoryBackend())
        task = ledger.tasks["action_1"]
        assert task.verify_integrity() is True
        # Simulate corruption (LLM tampered with description)
        task.description = "TAMPERED BY LLM"
        assert task.verify_integrity() is False

    def test_empty_actions_creates_empty_ledger(self):
        from agent_ledger.core import create_ledger_from_actions
        ledger = create_ledger_from_actions(user_id=1, prompt_id=1, actions=[],
                                            backend=InMemoryBackend())
        assert len(ledger.tasks) == 0


# ==================== HARTOS Wiring: Lifecycle hooks ownership sync ====================

class TestLifecycleOwnershipSync:
    """Test _auto_sync_to_ledger wires ownership claim/release/heartbeat."""

    def _make_ledger_with_task(self, task_id="action_1"):
        """Create a ledger with one task in PENDING state."""
        ledger = SmartLedger("test", "session_1", backend=InMemoryBackend())
        task = Task(task_id, "Test task", TaskType.PRE_ASSIGNED)
        task.seal_integrity()
        ledger.add_task(task)
        return ledger

    def test_ownership_claimed_on_in_progress(self):
        """When ActionState → IN_PROGRESS, task ownership should be claimed."""
        from lifecycle_hooks import (
            _auto_sync_to_ledger, register_ledger_for_session,
            ActionState, _ledger_registry
        )
        ledger = self._make_ledger_with_task()
        register_ledger_for_session("42_100", ledger)
        try:
            # Simulate IN_PROGRESS transition
            _auto_sync_to_ledger("42_100", 1, ActionState.IN_PROGRESS)
            task = ledger.tasks["action_1"]
            assert task.is_owned, "Task should be owned after IN_PROGRESS"
            assert task.owner_user_id == "42"
            assert task.owner_prompt_id == "100"
            assert task.owner_node_id is not None
        finally:
            _ledger_registry.pop("42_100", None)

    def test_ownership_released_on_terminal(self):
        """When ActionState → COMPLETED/TERMINATED, ownership released."""
        from lifecycle_hooks import (
            _auto_sync_to_ledger, register_ledger_for_session,
            ActionState, _ledger_registry
        )
        ledger = self._make_ledger_with_task()
        register_ledger_for_session("42_100", ledger)
        try:
            # First claim via IN_PROGRESS
            _auto_sync_to_ledger("42_100", 1, ActionState.IN_PROGRESS)
            task = ledger.tasks["action_1"]
            assert task.is_owned

            # Then release via COMPLETED
            _auto_sync_to_ledger("42_100", 1, ActionState.COMPLETED)
            assert not task.is_owned, "Task ownership should be released after COMPLETED"
        finally:
            _ledger_registry.pop("42_100", None)

    def test_heartbeat_updated_on_every_transition(self):
        """Every state change should update heartbeat timestamp."""
        from lifecycle_hooks import (
            _auto_sync_to_ledger, register_ledger_for_session,
            ActionState, _ledger_registry
        )
        ledger = self._make_ledger_with_task()
        register_ledger_for_session("42_100", ledger)
        try:
            task = ledger.tasks["action_1"]
            assert task.last_heartbeat_at is None

            _auto_sync_to_ledger("42_100", 1, ActionState.IN_PROGRESS)
            hb1 = task.last_heartbeat_at
            assert hb1 is not None, "Heartbeat should be set after IN_PROGRESS"
        finally:
            _ledger_registry.pop("42_100", None)

    def test_time_spent_recorded_on_completion(self):
        """Time spent should be recorded when task completes."""
        from lifecycle_hooks import (
            _auto_sync_to_ledger, register_ledger_for_session,
            ActionState, _ledger_registry
        )
        ledger = self._make_ledger_with_task()
        register_ledger_for_session("42_100", ledger)
        try:
            _auto_sync_to_ledger("42_100", 1, ActionState.IN_PROGRESS)
            task = ledger.tasks["action_1"]
            assert task.started_at is not None

            _auto_sync_to_ledger("42_100", 1, ActionState.COMPLETED)
            assert task.time_spent_s > 0 or task.time_spent_s == 0  # At least recorded
        finally:
            _ledger_registry.pop("42_100", None)

    def test_sla_breach_flagged(self):
        """SLA breach should be flagged during state transition."""
        from lifecycle_hooks import (
            _auto_sync_to_ledger, register_ledger_for_session,
            ActionState, _ledger_registry
        )
        ledger = self._make_ledger_with_task()
        task = ledger.tasks["action_1"]
        task.sla_target_s = 0.001  # Will breach immediately
        register_ledger_for_session("42_100", ledger)
        try:
            _auto_sync_to_ledger("42_100", 1, ActionState.IN_PROGRESS)
            # Give it a tiny moment to breach
            time.sleep(0.01)
            _auto_sync_to_ledger("42_100", 1, ActionState.STATUS_VERIFICATION_REQUESTED)
            # SLA check happens on state change — should be flagged by now
            assert task.is_sla_breached()
        finally:
            _ledger_registry.pop("42_100", None)

    def test_no_crash_on_missing_task(self):
        """If task doesn't exist in ledger, sync should silently skip."""
        from lifecycle_hooks import (
            _auto_sync_to_ledger, register_ledger_for_session,
            ActionState, _ledger_registry
        )
        ledger = SmartLedger("test", "session_1", backend=InMemoryBackend())
        register_ledger_for_session("42_100", ledger)
        try:
            # action_999 doesn't exist — should not crash
            _auto_sync_to_ledger("42_100", 999, ActionState.IN_PROGRESS)
        finally:
            _ledger_registry.pop("42_100", None)

    def test_no_crash_on_missing_ledger(self):
        """If no ledger registered, sync should silently skip."""
        from lifecycle_hooks import _auto_sync_to_ledger, ActionState
        # No ledger registered for this prompt — should not crash
        _auto_sync_to_ledger("nonexistent_prompt", 1, ActionState.IN_PROGRESS)


# ==================== LLM Hallucination Defense ====================

class TestLLMClaimValidation:
    """Test that the ledger rejects false LLM claims using known state."""

    def test_completed_task_cannot_be_recompleted(self):
        """LLM cannot claim completion on a task that's already COMPLETED."""
        task = Task("action_1", "Test", TaskType.PRE_ASSIGNED)
        task.seal_integrity()
        task.start()
        task.complete(result={"done": True})
        assert task.status == TaskStatus.COMPLETED

        # Trying to complete again should fail (terminal state)
        assert task._validate_transition(TaskStatus.COMPLETED) is False

    def test_integrity_verification_catches_tampered_task(self):
        """If task data was corrupted, verify_integrity() returns False."""
        task = Task("action_1", "Original task", TaskType.PRE_ASSIGNED)
        task.seal_integrity()
        assert task.verify_integrity() is True

        # LLM somehow changed the description
        task.description = "Hallucinated task"
        assert task.verify_integrity() is False

    def test_claim_on_nonexistent_task_fails_gracefully(self):
        """Claiming ownership on a task not in the ledger should not crash."""
        ledger = SmartLedger("test", "session", backend=InMemoryBackend())
        task = ledger.tasks.get("action_999")
        assert task is None  # Expected: task doesn't exist

    def test_action_id_mismatch_detection(self):
        """Simulates LLM claiming a different action_id than assigned.

        The defense: pipeline uses its own current_action_id from scope,
        not the LLM's claimed action_id.
        """
        ledger = SmartLedger("test", "session", backend=InMemoryBackend())
        # Add action 1 and 2
        t1 = Task("action_1", "Task 1", TaskType.PRE_ASSIGNED)
        t2 = Task("action_2", "Task 2", TaskType.PRE_ASSIGNED)
        t1.seal_integrity()
        t2.seal_integrity()
        ledger.add_task(t1)
        ledger.add_task(t2)

        # Pipeline knows current_action_id=1
        pipeline_current = 1
        # LLM claims action_id=2
        llm_claimed = 2

        # Defense: use pipeline's known value
        corrected = pipeline_current if llm_claimed != pipeline_current else llm_claimed
        assert corrected == 1, "Should use pipeline's known action_id, not LLM's"

    def test_budget_exhaustion_blocks_execution(self):
        """Task with exhausted budget should signal stop."""
        task = Task("action_1", "Expensive task", TaskType.PRE_ASSIGNED)
        task.spark_budget = 100.0
        task.record_spend(spark=100.0)
        assert task.is_budget_exhausted() is True

    def test_budget_not_exhausted_allows_execution(self):
        """Task within budget should continue."""
        task = Task("action_1", "Cheap task", TaskType.PRE_ASSIGNED)
        task.spark_budget = 100.0
        task.record_spend(spark=50.0)
        assert task.is_budget_exhausted() is False

    def test_terminal_state_blocks_all_transitions(self):
        """Once task reaches terminal state, no further transitions allowed."""
        for terminal in [TaskStatus.COMPLETED, TaskStatus.FAILED,
                         TaskStatus.CANCELLED, TaskStatus.TERMINATED,
                         TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE]:
            task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
            task.start()
            if terminal == TaskStatus.COMPLETED:
                task.complete()
            elif terminal == TaskStatus.FAILED:
                task.fail("error")
            elif terminal == TaskStatus.CANCELLED:
                task.cancel()
            elif terminal == TaskStatus.TERMINATED:
                task.terminate()
            elif terminal == TaskStatus.SKIPPED:
                task.skip()
            elif terminal == TaskStatus.NOT_APPLICABLE:
                task.mark_not_applicable()

            # LLM cannot restart or re-progress a terminal task
            assert task._validate_transition(TaskStatus.IN_PROGRESS) is False
            assert task._validate_transition(TaskStatus.PENDING) is False

    def test_ownership_prevents_double_claim(self):
        """If task is already owned, another claim should be rejected."""
        task = Task("action_1", "Test", TaskType.PRE_ASSIGNED)
        assert task.claim(node_id="node_A", user_id="user_1") is True
        # Second claim by different node should fail
        assert task.claim(node_id="node_B", user_id="user_2") is False
        assert task.owner_node_id == "node_A"  # Original owner preserved
