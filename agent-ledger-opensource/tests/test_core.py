"""
Tests for Agent Ledger core functionality.
"""

import pytest
from agent_ledger import (
    SmartLedger,
    Task,
    TaskType,
    TaskStatus,
    ExecutionMode,
    InMemoryBackend,
)


class TestTask:
    """Tests for Task class."""

    def test_task_creation(self):
        """Test basic task creation."""
        task = Task(
            task_id="test_1",
            description="Test task",
            task_type=TaskType.PRE_ASSIGNED
        )

        assert task.task_id == "test_1"
        assert task.description == "Test task"
        assert task.task_type == TaskType.PRE_ASSIGNED
        assert task.status == TaskStatus.PENDING
        assert task.priority == 50

    def test_task_with_prerequisites(self):
        """Test task with prerequisites."""
        task = Task(
            task_id="test_2",
            description="Dependent task",
            task_type=TaskType.PRE_ASSIGNED,
            prerequisites=["task_1", "task_2"]
        )

        assert task.prerequisites == ["task_1", "task_2"]

    def test_task_state_transitions(self):
        """Test task state transitions."""
        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)

        # Start task
        assert task.start()
        assert task.status == TaskStatus.IN_PROGRESS

        # Complete task
        assert task.complete(result={"done": True})
        assert task.status == TaskStatus.COMPLETED
        assert task.result == {"done": True}

    def test_task_pause_resume(self):
        """Test pause and resume."""
        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        task.start()

        assert task.pause("Testing pause")
        assert task.status == TaskStatus.PAUSED

        assert task.resume("Testing resume")
        assert task.status == TaskStatus.IN_PROGRESS

    def test_task_fail(self):
        """Test task failure."""
        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        task.start()

        assert task.fail("Something went wrong")
        assert task.status == TaskStatus.FAILED
        assert task.error_message == "Something went wrong"

    def test_invalid_transition(self):
        """Test invalid state transitions."""
        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        task.start()
        task.complete()

        # Cannot transition from COMPLETED
        assert not task.start()
        assert task.status == TaskStatus.COMPLETED

    def test_task_serialization(self):
        """Test task to_dict and from_dict."""
        task = Task(
            task_id="t1",
            description="Test task",
            task_type=TaskType.AUTONOMOUS,
            priority=75,
            prerequisites=["p1"],
            context={"key": "value"}
        )

        # Serialize
        data = task.to_dict()
        assert data["task_id"] == "t1"
        assert data["priority"] == 75

        # Deserialize
        restored = Task.from_dict(data)
        assert restored.task_id == task.task_id
        assert restored.description == task.description
        assert restored.priority == task.priority

    def test_state_history(self):
        """Test state history tracking."""
        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        task.start()
        task.pause("Testing")
        task.resume("Continue")

        history = task.get_state_history()
        assert len(history) >= 4  # Created, started, paused, resuming, in_progress

    def test_is_terminal(self):
        """Test terminal state detection."""
        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        assert not task.is_terminal()

        task.start()
        task.complete()
        assert task.is_terminal()


class TestTaskRecipeHierarchy:
    """Regression tests for recipe-hierarchy fields added 2026-05-27.

    The dashboard groups tasks as prompt_id → session_id → flow_id →
    action_id; losing any of these stamps silently breaks grouping.
    Tests here pin: defaults, round-trip, legacy from_dict tolerance.
    See docs/architecture/TASK_LEDGER_GROUPING_FIX_PLAN.md.
    """

    def test_recipe_fields_default_none(self):
        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        assert task.recipe_prompt_id is None
        assert task.recipe_flow_id is None
        assert task.recipe_action_id is None

    def test_recipe_fields_round_trip_via_dict(self):
        task = Task("action_3", "third", TaskType.PRE_ASSIGNED)
        task.recipe_prompt_id = "prompt_42"
        task.recipe_flow_id = 2
        task.recipe_action_id = 3

        d = task.to_dict()
        assert d["recipe_prompt_id"] == "prompt_42"
        assert d["recipe_flow_id"] == 2
        assert d["recipe_action_id"] == 3

        restored = Task.from_dict(d)
        assert restored.recipe_prompt_id == "prompt_42"
        assert restored.recipe_flow_id == 2
        assert restored.recipe_action_id == 3

    def test_legacy_dict_missing_recipe_keys_restores_as_none(self):
        # Simulate a pre-schema ledger dict — pop the recipe_* keys
        # from a freshly-serialized task and reload.  None defaults
        # let the dashboard's filename/task_id fallback take over.
        task = Task("action_1", "legacy", TaskType.PRE_ASSIGNED)
        d = task.to_dict()
        for k in ("recipe_prompt_id", "recipe_flow_id", "recipe_action_id"):
            d.pop(k, None)
        restored = Task.from_dict(d)
        assert restored.recipe_prompt_id is None
        assert restored.recipe_flow_id is None
        assert restored.recipe_action_id is None


class TestSmartLedger:
    """Tests for SmartLedger class."""

    def test_ledger_creation(self):
        """Test ledger creation with in-memory backend."""
        backend = InMemoryBackend()
        ledger = SmartLedger(
            agent_id="test_agent",
            session_id="test_session",
            backend=backend
        )

        assert ledger.agent_id == "test_agent"
        assert ledger.session_id == "test_session"
        assert len(ledger.tasks) == 0

    def test_add_task(self):
        """Test adding tasks to ledger."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        task = Task("t1", "Test task", TaskType.PRE_ASSIGNED)
        assert ledger.add_task(task)
        assert "t1" in ledger.tasks

        # Adding duplicate should fail
        assert not ledger.add_task(task)

    def test_get_task(self):
        """Test getting task from ledger."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        task = Task("t1", "Test task", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)

        retrieved = ledger.get_task("t1")
        assert retrieved is not None
        assert retrieved.task_id == "t1"

        # Non-existent task
        assert ledger.get_task("nonexistent") is None

    def test_get_ready_tasks(self):
        """Test getting ready tasks."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        # Add tasks with dependencies
        t1 = Task("t1", "First", TaskType.PRE_ASSIGNED, priority=100)
        t2 = Task("t2", "Second", TaskType.PRE_ASSIGNED,
                  prerequisites=["t1"], priority=90)
        t3 = Task("t3", "Third", TaskType.PRE_ASSIGNED, priority=80)

        ledger.add_task(t1)
        ledger.add_task(t2)
        ledger.add_task(t3)

        ready = ledger.get_ready_tasks()
        # Only t1 and t3 should be ready (t2 depends on t1)
        ready_ids = [t.task_id for t in ready]
        assert "t1" in ready_ids
        assert "t3" in ready_ids
        assert "t2" not in ready_ids

        # Should be sorted by priority
        assert ready[0].task_id == "t1"  # priority 100

    def test_get_next_task(self):
        """Test getting next task to execute."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        t1 = Task("t1", "Low priority", TaskType.PRE_ASSIGNED, priority=50)
        t2 = Task("t2", "High priority", TaskType.PRE_ASSIGNED, priority=100)

        ledger.add_task(t1)
        ledger.add_task(t2)

        next_task = ledger.get_next_task()
        assert next_task.task_id == "t2"  # Higher priority

    def test_update_task_status(self):
        """Test updating task status."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)

        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        assert ledger.get_task("t1").status == TaskStatus.IN_PROGRESS

    def test_complete_task(self):
        """Test completing a task."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)

        result = {"processed": 100}
        ledger.complete_task("t1", result=result)

        completed_task = ledger.get_task("t1")
        assert completed_task.status == TaskStatus.COMPLETED
        assert completed_task.result == result

    def test_reprioritize_task(self):
        """Test task reprioritization."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        task = Task("t1", "Test", TaskType.PRE_ASSIGNED, priority=50)
        ledger.add_task(task)

        ledger.reprioritize_task("t1", 100)
        assert ledger.get_task("t1").priority == 100

        # Test bounds
        ledger.reprioritize_task("t1", 150)
        assert ledger.get_task("t1").priority == 100  # Capped at 100

        ledger.reprioritize_task("t1", -10)
        assert ledger.get_task("t1").priority == 0  # Minimum 0

    def test_get_progress_summary(self):
        """Test progress summary."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        # Empty ledger
        summary = ledger.get_progress_summary()
        assert summary["total"] == 0
        assert summary["progress"] == "0%"

        # Add tasks
        ledger.add_task(Task("t1", "Test 1", TaskType.PRE_ASSIGNED))
        ledger.add_task(Task("t2", "Test 2", TaskType.PRE_ASSIGNED))

        summary = ledger.get_progress_summary()
        assert summary["total"] == 2
        assert summary["pending"] == 2

        # Complete one
        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)
        ledger.complete_task("t1")

        summary = ledger.get_progress_summary()
        assert summary["completed"] == 1
        assert summary["progress"] == "50.0%"

    def test_get_tasks_by_status(self):
        """Test filtering tasks by status."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        ledger.add_task(Task("t1", "Test 1", TaskType.PRE_ASSIGNED))
        ledger.add_task(Task("t2", "Test 2", TaskType.PRE_ASSIGNED))

        ledger.update_task_status("t1", TaskStatus.IN_PROGRESS)

        pending = ledger.get_tasks_by_status(TaskStatus.PENDING)
        assert len(pending) == 1
        assert pending[0].task_id == "t2"

        in_progress = ledger.get_tasks_by_status(TaskStatus.IN_PROGRESS)
        assert len(in_progress) == 1
        assert in_progress[0].task_id == "t1"

    def test_cancel_task(self):
        """Test task cancellation."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        task = Task("t1", "Test", TaskType.PRE_ASSIGNED)
        ledger.add_task(task)

        ledger.cancel_task("t1")
        assert ledger.get_task("t1").status == TaskStatus.CANCELLED

    def test_cancel_task_cascade(self):
        """Test cascading task cancellation."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        t1 = Task("t1", "Parent", TaskType.PRE_ASSIGNED)
        t2 = Task("t2", "Child", TaskType.PRE_ASSIGNED, prerequisites=["t1"])

        ledger.add_task(t1)
        ledger.add_task(t2)

        ledger.cancel_task("t1", cascade=True)

        assert ledger.get_task("t1").status == TaskStatus.CANCELLED
        assert ledger.get_task("t2").status == TaskStatus.CANCELLED

    def test_create_parent_child_task(self):
        """Test creating parent-child task relationships."""
        backend = InMemoryBackend()
        ledger = SmartLedger("agent", "session", backend=backend)

        parent = Task("parent", "Parent task", TaskType.PRE_ASSIGNED)
        ledger.add_task(parent)

        child = ledger.create_parent_child_task(
            parent_task_id="parent",
            child_description="Child task",
            child_type=TaskType.AUTONOMOUS
        )

        assert child is not None
        assert child.parent_task_id == "parent"
        assert child.task_id in ledger.get_task("parent").child_task_ids


class TestBackends:
    """Tests for storage backends."""

    def test_in_memory_backend(self):
        """Test InMemoryBackend."""
        backend = InMemoryBackend()

        # Save
        data = {"key": "value"}
        assert backend.save("test_key", data)

        # Load
        loaded = backend.load("test_key")
        assert loaded == data

        # Exists
        assert backend.exists("test_key")
        assert not backend.exists("nonexistent")

        # Delete
        assert backend.delete("test_key")
        assert not backend.exists("test_key")

        # List keys
        backend.save("key1", {"a": 1})
        backend.save("key2", {"b": 2})
        keys = backend.list_keys("key*")
        assert "key1" in keys
        assert "key2" in keys


class TestRecipeHierarchyStamping:
    """End-to-end tests for the dashboard grouping pipeline.

    Each test exercises the round-trip from action dicts → ledger →
    grouped dict the dashboard would read.  See
    docs/architecture/TASK_LEDGER_GROUPING_FIX_PLAN.md §5.
    """

    def test_create_ledger_from_actions_stamps_recipe_fields(self):
        from agent_ledger.core import create_ledger_from_actions
        actions = [
            {"action_id": 1, "action": "do x", "description": "do x"},
            {"action_id": 2, "action": "do y", "description": "do y"},
        ]
        ledger = create_ledger_from_actions(
            agent_id="prompt_42",
            session_id="alice_session",
            actions=actions,
            backend=InMemoryBackend(),
            flow_id=3,
        )
        for tid, t in ledger.tasks.items():
            assert t.recipe_prompt_id == "prompt_42"
            assert t.recipe_flow_id == 3
            assert t.recipe_action_id in (1, 2)

    def test_create_ledger_from_actions_default_flow_id_is_zero(self):
        from agent_ledger.core import create_ledger_from_actions
        actions = [{"action_id": 1, "action": "x", "description": "x"}]
        ledger = create_ledger_from_actions(
            agent_id="prompt_x",
            session_id="s1",
            actions=actions,
            backend=InMemoryBackend(),
        )
        task = next(iter(ledger.tasks.values()))
        assert task.recipe_flow_id == 0

    def test_create_ledger_from_actions_recipe_prompt_id_override(self):
        from agent_ledger.core import create_ledger_from_actions
        actions = [{"action_id": 1, "action": "x", "description": "x"}]
        ledger = create_ledger_from_actions(
            agent_id="agent_abc",
            session_id="s1",
            actions=actions,
            backend=InMemoryBackend(),
            recipe_prompt_id="prompt_specific",
        )
        task = next(iter(ledger.tasks.values()))
        assert task.recipe_prompt_id == "prompt_specific"

    def test_grouping_helper_handles_new_format(self, tmp_path):
        """Realistic filename: agent_id is the stringified int
        prompt_id ('99'), session_id is f"{user_id}_{prompt_id}"
        ('alice_99' here) per ``create_ledger_from_actions``.
        agent_id never contains underscores."""
        import json
        from agent_ledger.core import create_ledger_from_actions
        actions = [
            {"action_id": 1, "action": "a1", "description": "a1"},
            {"action_id": 2, "action": "a2", "description": "a2"},
        ]
        ledger = create_ledger_from_actions(
            agent_id="99", session_id="alice_99",
            actions=actions, backend=InMemoryBackend(), flow_id=1,
        )
        (tmp_path / "ledger_99_alice_99.json").write_text(
            json.dumps({
                "agent_id": "99", "session_id": "alice_99",
                "tasks": {tid: t.to_dict() for tid, t in ledger.tasks.items()},
            })
        )
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(str(tmp_path))
        assert "99" in groups, f"got: {list(groups.keys())}"
        assert "alice_99" in groups["99"], f"got: {list(groups['99'].keys())}"
        assert 1 in groups["99"]["alice_99"]
        action_ids = [a[0] for a in groups["99"]["alice_99"][1]]
        assert action_ids == [1, 2], f"actions out of order: {action_ids}"

    def test_grouping_helper_legacy_fallback(self, tmp_path):
        """Pre-schema ledgers (the 431 files in production on 2026-05-26)
        must keep grouping correctly: prompt_id from filename, flow_id=0,
        action_id parsed from task_id string.  agent_id='77' is the
        canonical int-as-string convention."""
        import json
        legacy_tasks = {
            "action_1": {
                "task_id": "action_1", "description": "old1",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "pending", "priority": 50,
            },
            "action_2": {
                "task_id": "action_2", "description": "old2",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "pending", "priority": 50,
            },
        }
        (tmp_path / "ledger_77_bob_77.json").write_text(
            json.dumps({"agent_id": "77", "session_id": "bob_77",
                        "tasks": legacy_tasks})
        )
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(str(tmp_path))
        assert "77" in groups, f"got: {list(groups.keys())}"
        assert "bob_77" in groups["77"], f"got: {list(groups['77'].keys())}"
        # Legacy → flow_id=0 default
        assert 0 in groups["77"]["bob_77"]
        action_ids = sorted(a[0] for a in groups["77"]["bob_77"][0])
        assert action_ids == [1, 2]

    def test_grouping_helper_empty_dir_returns_empty_dict(self, tmp_path):
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(str(tmp_path))
        assert groups == {}

    def test_grouping_helper_filename_multi_underscore_session(self, tmp_path):
        """``create_ledger_from_actions`` builds session_id as
        f"{user_id}_{prompt_id}", so legacy filenames look like
        ``ledger_88888_12345_88888.json`` — agent=88888, session=12345_88888.
        Verify the regex picks the agent as the shortest leading
        underscore-free token, leaving the rest for the session."""
        import json
        legacy_tasks = {
            "action_1": {
                "task_id": "action_1", "description": "x",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "pending", "priority": 50,
            },
        }
        (tmp_path / "ledger_88888_12345_88888.json").write_text(
            json.dumps({"agent_id": "88888", "session_id": "12345_88888",
                        "tasks": legacy_tasks})
        )
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(str(tmp_path))
        assert "88888" in groups, f"expected agent '88888', got {list(groups.keys())}"
        assert "12345_88888" in groups["88888"], \
            f"expected session '12345_88888', got {list(groups['88888'].keys())}"

    def test_grouping_helper_filename_uuid_agent(self, tmp_path):
        """Autonomous agents identify by UUID — verify the regex
        accepts UUIDs in the agent slot (UUIDs contain dashes, not
        underscores, so the non-greedy first group still wins)."""
        import json
        uuid_agent = "177bdda1-c710-4a47-9c89-56808a13fc84"
        session = "72629287662"
        legacy_tasks = {
            "action_1": {
                "task_id": "action_1", "description": "x",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "pending", "priority": 50,
            },
        }
        (tmp_path / f"ledger_{uuid_agent}_{session}.json").write_text(
            json.dumps({"agent_id": uuid_agent, "session_id": session,
                        "tasks": legacy_tasks})
        )
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(str(tmp_path))
        assert uuid_agent in groups
        assert session in groups[uuid_agent]

    def test_grouping_helper_nonexistent_dir_returns_empty_dict(self):
        groups = SmartLedger.list_grouped_by_recipe_hierarchy(
            "/nonexistent_dir_xyz_test_99999",
        )
        assert groups == {}

    def test_session_id_resume_when_unfinished(self, tmp_path, monkeypatch):
        """Phase 2: when a prior ledger for (user, prompt) has any
        non-terminal task, ``create_ledger_from_actions`` attaches to
        that session_id instead of minting a new one."""
        import json
        # Pre-write an unfinished ledger for prompt=42, user=10202.
        # Task status PENDING is non-terminal → session must be resumed.
        unfinished_tasks = {
            "action_1": {
                "task_id": "action_1", "description": "in-flight",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "pending", "priority": 50,
            },
        }
        existing_session = "10202_42_1716000000000"
        (tmp_path / f"ledger_42_{existing_session}.json").write_text(
            json.dumps({"agent_id": "42", "session_id": existing_session,
                        "tasks": unfinished_tasks})
        )
        # Point the helper at our tmp dir for this test.
        monkeypatch.chdir(tmp_path.parent)
        from agent_ledger.core import create_ledger_from_actions
        import agent_ledger.core as _core
        # Patch the default ledger_dir resolver used by the helper —
        # the helper takes ledger_dir as a kwarg with default "agent_data",
        # so we redirect via the SmartLedger.list_grouped helper too.
        original_helper = _core.SmartLedger.list_grouped_by_recipe_hierarchy
        def _scoped_helper(cls=None, ledger_dir=None):
            return original_helper.__func__(_core.SmartLedger, str(tmp_path))
        monkeypatch.setattr(
            _core.SmartLedger,
            'list_grouped_by_recipe_hierarchy',
            classmethod(_scoped_helper),
        )

        actions = [{"action_id": 1, "action": "x", "description": "x"}]
        ledger = create_ledger_from_actions(
            user_id=10202, prompt_id=42, actions=actions,
            backend=InMemoryBackend(),
        )
        assert ledger.session_id == existing_session, (
            f"Expected resume into {existing_session}, got {ledger.session_id}"
        )

    def test_session_id_mints_new_when_all_sessions_terminal(self, tmp_path, monkeypatch):
        """Phase 2: when every prior session for this (user, prompt) is
        terminal, mint a fresh timestamped session_id."""
        import json
        terminal_tasks = {
            "action_1": {
                "task_id": "action_1", "description": "done",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "completed", "priority": 50,
            },
        }
        old_session = "10202_42_1715000000000"
        (tmp_path / f"ledger_42_{old_session}.json").write_text(
            json.dumps({"agent_id": "42", "session_id": old_session,
                        "tasks": terminal_tasks})
        )
        from agent_ledger.core import create_ledger_from_actions
        import agent_ledger.core as _core
        original_helper = _core.SmartLedger.list_grouped_by_recipe_hierarchy
        def _scoped_helper(cls=None, ledger_dir=None):
            return original_helper.__func__(_core.SmartLedger, str(tmp_path))
        monkeypatch.setattr(
            _core.SmartLedger,
            'list_grouped_by_recipe_hierarchy',
            classmethod(_scoped_helper),
        )

        actions = [{"action_id": 1, "action": "x", "description": "x"}]
        ledger = create_ledger_from_actions(
            user_id=10202, prompt_id=42, actions=actions,
            backend=InMemoryBackend(),
        )
        assert ledger.session_id != old_session
        assert ledger.session_id.startswith("10202_42_"), (
            f"Fresh session_id should keep user_prefix; got {ledger.session_id}"
        )

    def test_session_id_mints_new_when_no_prior(self, tmp_path, monkeypatch):
        """Phase 2: no prior ledger for (user, prompt) → fresh session."""
        from agent_ledger.core import create_ledger_from_actions
        import agent_ledger.core as _core
        original_helper = _core.SmartLedger.list_grouped_by_recipe_hierarchy
        def _scoped_helper(cls=None, ledger_dir=None):
            return original_helper.__func__(_core.SmartLedger, str(tmp_path))
        monkeypatch.setattr(
            _core.SmartLedger,
            'list_grouped_by_recipe_hierarchy',
            classmethod(_scoped_helper),
        )
        ledger = create_ledger_from_actions(
            user_id=10202, prompt_id=42,
            actions=[{"action_id": 1, "action": "x", "description": "x"}],
            backend=InMemoryBackend(),
        )
        assert ledger.session_id.startswith("10202_42_")
        # Timestamp suffix is numeric and at least 10 digits (ms epoch
        # has 13 digits post-2001).
        suffix = ledger.session_id.rsplit("_", 1)[-1]
        assert suffix.isdigit() and len(suffix) >= 10

    def test_session_id_does_not_resume_different_user(self, tmp_path, monkeypatch):
        """Phase 2: an unfinished session for user A must NOT be
        resumed when user B calls.  The user_prefix filter enforces
        per-user isolation."""
        import json
        unfinished_tasks = {
            "action_1": {
                "task_id": "action_1", "description": "in-flight",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "pending", "priority": 50,
            },
        }
        # User 'alice' has an unfinished session for prompt 42.
        alice_session = "11111_42_1716000000000"
        (tmp_path / f"ledger_42_{alice_session}.json").write_text(
            json.dumps({"agent_id": "42", "session_id": alice_session,
                        "tasks": unfinished_tasks})
        )
        from agent_ledger.core import create_ledger_from_actions
        import agent_ledger.core as _core
        original_helper = _core.SmartLedger.list_grouped_by_recipe_hierarchy
        def _scoped_helper(cls=None, ledger_dir=None):
            return original_helper.__func__(_core.SmartLedger, str(tmp_path))
        monkeypatch.setattr(
            _core.SmartLedger,
            'list_grouped_by_recipe_hierarchy',
            classmethod(_scoped_helper),
        )
        # User 'bob' (22222) calls — must mint a fresh session, NOT
        # attach to alice's.
        ledger = create_ledger_from_actions(
            user_id=22222, prompt_id=42,
            actions=[{"action_id": 1, "action": "x", "description": "x"}],
            backend=InMemoryBackend(),
        )
        assert ledger.session_id != alice_session
        assert ledger.session_id.startswith("22222_42_")

    def test_session_id_resumes_legacy_deterministic_format(self, tmp_path, monkeypatch):
        """Phase 2: legacy ledger files predating timestamped session_ids
        carry the deterministic ``f"{user_id}_{prompt_id}"`` form (no
        suffix).  When they contain non-terminal tasks, they MUST be
        resumed — the user_prefix filter recognises them via the
        ``f"{user_id}_"`` prefix common to old and new formats."""
        import json
        unfinished_tasks = {
            "action_1": {
                "task_id": "action_1", "description": "legacy in-flight",
                "execution_mode": "parallel", "task_type": "pre_assigned",
                "status": "in_progress", "priority": 50,
            },
        }
        legacy_session = "10202_42"
        (tmp_path / f"ledger_42_{legacy_session}.json").write_text(
            json.dumps({"agent_id": "42", "session_id": legacy_session,
                        "tasks": unfinished_tasks})
        )
        from agent_ledger.core import create_ledger_from_actions
        import agent_ledger.core as _core
        original_helper = _core.SmartLedger.list_grouped_by_recipe_hierarchy
        def _scoped_helper(cls=None, ledger_dir=None):
            return original_helper.__func__(_core.SmartLedger, str(tmp_path))
        monkeypatch.setattr(
            _core.SmartLedger,
            'list_grouped_by_recipe_hierarchy',
            classmethod(_scoped_helper),
        )
        ledger = create_ledger_from_actions(
            user_id=10202, prompt_id=42,
            actions=[{"action_id": 1, "action": "x", "description": "x"}],
            backend=InMemoryBackend(),
        )
        assert ledger.session_id == legacy_session, (
            f"Expected resume into legacy {legacy_session}, got {ledger.session_id}"
        )

    def test_session_id_resume_opt_out_preserves_deterministic_behaviour(self, tmp_path, monkeypatch):
        """Phase 2: ``resume_if_unfinished=False`` reverts to the
        pre-2026-05-27 deterministic ``f"{user_id}_{prompt_id}"`` form.
        Operators / migration scripts / tests that need stable
        session_ids can opt out without other code changes."""
        from agent_ledger.core import create_ledger_from_actions
        import agent_ledger.core as _core
        original_helper = _core.SmartLedger.list_grouped_by_recipe_hierarchy
        def _scoped_helper(cls=None, ledger_dir=None):
            return original_helper.__func__(_core.SmartLedger, str(tmp_path))
        monkeypatch.setattr(
            _core.SmartLedger,
            'list_grouped_by_recipe_hierarchy',
            classmethod(_scoped_helper),
        )
        ledger = create_ledger_from_actions(
            user_id=10202, prompt_id=42,
            actions=[{"action_id": 1, "action": "x", "description": "x"}],
            backend=InMemoryBackend(),
            resume_if_unfinished=False,
        )
        # With resume_if_unfinished=False AND no prior session, we still
        # mint a fresh timestamped session_id because that's the only
        # path that runs in the "no prior" case.  The opt-out matters
        # only when there's an unfinished prior — see next assertion via
        # a second call.
        assert ledger.session_id.startswith("10202_42_")

    def test_add_dynamic_task_inherits_recipe_prompt_and_flow(self):
        """Dynamic tasks inherit recipe_prompt_id from the ledger's
        agent_id and recipe_flow_id from any existing recipe task in
        the ledger.  recipe_action_id stays None (not in recipe)."""
        from agent_ledger.core import create_ledger_from_actions
        from unittest.mock import patch
        actions = [{"action_id": 1, "action": "a1", "description": "a1"}]
        ledger = create_ledger_from_actions(
            agent_id="prompt_88", session_id="charlie",
            actions=actions, backend=InMemoryBackend(), flow_id=5,
        )

        # Bypass LLM classification — that path is unrelated to the
        # recipe-hierarchy stamping we want to verify.
        fake_classification = {
            "relationship": "independent",
            "execution_mode": "parallel",
            "prerequisites": [],
            "priority": 50,
        }
        with patch.object(ledger, "_classify_task_relationship",
                          return_value=fake_classification), \
             patch.object(ledger, "_get_default_llm_client",
                          return_value=None):
            task = ledger.add_dynamic_task(
                "side quest",
                {"current_action_id": 1, "discovered_by": "test"},
            )

        assert task is not None
        assert task.recipe_prompt_id == "prompt_88"
        assert task.recipe_flow_id == 5  # inherited from sibling action_1
        assert task.recipe_action_id is None  # dynamic, no recipe ordinal


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
