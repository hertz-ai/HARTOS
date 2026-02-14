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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
