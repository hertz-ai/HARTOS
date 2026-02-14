"""
Comprehensive Test Suite for Task State Management System

Tests all 11 task states, state transitions, validation, history tracking,
and SmartLedger state management methods.
"""

import random
import time
from pathlib import Path

from agent_ledger import (
    Task, TaskType, TaskStatus, ExecutionMode,
    SmartLedger, create_ledger_from_actions, get_production_backend
)

# Use random IDs to avoid state pollution
TEST_USER_ID = random.randint(800000, 899999)
TEST_PROMPT_ID = random.randint(70000, 79999)


def test_all_states_defined():
    """Test that all 11 states are properly defined"""
    print("\n" + "="*70)
    print("TEST 1: All States Defined")
    print("="*70)

    expected_states = [
        "pending", "in_progress", "paused", "user_stopped", "blocked",
        "completed", "failed", "cancelled", "terminated", "skipped",
        "not_applicable", "resuming"
    ]

    for state_name in expected_states:
        try:
            state = TaskStatus(state_name)
            print(f"  [OK] {state_name.upper()}: {state}")
        except ValueError:
            print(f"  [FAIL] {state_name.upper()} not defined")
            return False

    print("\n[PASS] All 11 states defined correctly\n")
    return True


def test_state_categories():
    """Test state category classification methods"""
    print("="*70)
    print("TEST 2: State Categories")
    print("="*70)

    # Terminal states
    terminal_states = [
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
        TaskStatus.TERMINATED, TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE
    ]

    for state in terminal_states:
        assert TaskStatus.is_terminal_state(state), f"{state} should be terminal"
        print(f"  [OK] {state} is terminal")

    # Active states
    active_states = [TaskStatus.IN_PROGRESS, TaskStatus.RESUMING]
    for state in active_states:
        assert TaskStatus.is_active_state(state), f"{state} should be active"
        print(f"  [OK] {state} is active")

    # Paused states
    paused_states = [TaskStatus.PAUSED, TaskStatus.USER_STOPPED, TaskStatus.BLOCKED]
    for state in paused_states:
        assert TaskStatus.is_paused_state(state), f"{state} should be paused"
        print(f"  [OK] {state} is paused")

    print("\n[PASS] State categories working correctly\n")
    return True


def test_basic_task_transitions():
    """Test basic happy-path transitions"""
    print("="*70)
    print("TEST 3: Basic Task State Transitions")
    print("="*70)

    task = Task(
        task_id="test_task_1",
        description="Test basic transitions",
        task_type=TaskType.PRE_ASSIGNED,
        status=TaskStatus.PENDING
    )

    # PENDING -> IN_PROGRESS
    assert task.start("Starting task"), "Should start from pending"
    assert task.status == TaskStatus.IN_PROGRESS
    print(f"  [OK] PENDING -> IN_PROGRESS")

    # IN_PROGRESS -> COMPLETED
    assert task.complete(result="Task done", reason="Finished"), "Should complete from in_progress"
    assert task.status == TaskStatus.COMPLETED
    assert task.completed_at is not None
    print(f"  [OK] IN_PROGRESS -> COMPLETED")

    # Try to transition from terminal (should fail)
    assert not task.start("Try to restart"), "Should not start from terminal state"
    print(f"  [OK] Cannot transition from terminal state")

    print("\n[PASS] Basic transitions working\n")
    return True


def test_pause_resume_cycle():
    """Test pause and resume functionality"""
    print("="*70)
    print("TEST 4: Pause/Resume Cycle")
    print("="*70)

    task = Task(
        task_id="test_task_2",
        description="Test pause/resume",
        task_type=TaskType.AUTONOMOUS,
        status=TaskStatus.PENDING
    )

    # Start task
    task.start("Begin work")
    print(f"  [OK] Task started: {task.status}")

    # Pause task
    assert task.pause("Need a break"), "Should pause from in_progress"
    assert task.status == TaskStatus.PAUSED
    assert task.paused_at is not None
    assert task.pause_count == 1
    print(f"  [OK] Task paused, count: {task.pause_count}")

    # Resume task
    assert task.resume("Back to work"), "Should resume from paused"
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.resumed_at is not None
    print(f"  [OK] Task resumed: {task.status}")

    # Pause again
    task.pause("Another break")
    assert task.pause_count == 2
    print(f"  [OK] Task paused again, count: {task.pause_count}")

    # Check resumable
    assert task.is_resumable(), "Paused task should be resumable"
    print(f"  [OK] Task is resumable")

    print("\n[PASS] Pause/resume cycle working\n")
    return True


def test_user_stop_functionality():
    """Test user stop and resume"""
    print("="*70)
    print("TEST 5: User Stop Functionality")
    print("="*70)

    task = Task(
        task_id="test_task_3",
        description="Test user stop",
        task_type=TaskType.USER_REQUESTED,
        status=TaskStatus.PENDING
    )

    task.start("Start work")
    print(f"  [OK] Task started")

    # User stops
    assert task.user_stop("User wants to do something else"), "Should stop from in_progress"
    assert task.status == TaskStatus.USER_STOPPED
    assert task.stop_reason == "User wants to do something else"
    print(f"  [OK] User stopped task: {task.stop_reason}")

    # User resumes
    assert task.resume("User ready to continue"), "Should resume from user_stopped"
    assert task.status == TaskStatus.IN_PROGRESS
    print(f"  [OK] Task resumed from user stop")

    print("\n[PASS] User stop working correctly\n")
    return True


def test_block_and_fail():
    """Test blocking and failure states"""
    print("="*70)
    print("TEST 6: Block and Fail States")
    print("="*70)

    # Test blocking
    task1 = Task(
        task_id="test_task_4",
        description="Test blocking",
        task_type=TaskType.AUTONOMOUS,
        status=TaskStatus.PENDING
    )

    task1.start("Begin")
    assert task1.block("Missing dependency"), "Should block from in_progress"
    assert task1.status == TaskStatus.BLOCKED
    assert task1.error_message == "Missing dependency"
    print(f"  [OK] Task blocked: {task1.error_message}")

    # Can transition from blocked to pending
    task1._record_state_transition(TaskStatus.PENDING, "Dependency resolved")
    assert task1.status == TaskStatus.PENDING
    print(f"  [OK] Unblocked task back to pending")

    # Test failure
    task2 = Task(
        task_id="test_task_5",
        description="Test failure",
        task_type=TaskType.AUTONOMOUS,
        status=TaskStatus.PENDING
    )

    task2.start("Start")
    assert task2.fail("Fatal error occurred", "Execution failed"), "Should fail from in_progress"
    assert task2.status == TaskStatus.FAILED
    assert task2.completed_at is not None
    assert "Fatal error" in task2.error_message
    print(f"  [OK] Task failed: {task2.error_message}")

    # Cannot resume failed task
    assert task2.is_terminal(), "Failed task should be terminal"
    print(f"  [OK] Failed task is terminal")

    print("\n[PASS] Block and fail working correctly\n")
    return True


def test_termination_and_cancellation():
    """Test terminate, cancel, skip, not_applicable"""
    print("="*70)
    print("TEST 7: Termination and Cancellation")
    print("="*70)

    # Test terminate
    task1 = Task("term_1", "Test terminate", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    task1.start("Start")
    assert task1.terminate("Emergency stop"), "Should terminate from in_progress"
    assert task1.status == TaskStatus.TERMINATED
    assert task1.termination_reason == "Emergency stop"
    print(f"  [OK] Task terminated: {task1.termination_reason}")

    # Test cancel
    task2 = Task("cancel_1", "Test cancel", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    assert task2.cancel("User cancelled"), "Should cancel from pending"
    assert task2.status == TaskStatus.CANCELLED
    print(f"  [OK] Task cancelled")

    # Test skip
    task3 = Task("skip_1", "Test skip", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    assert task3.skip("Not needed"), "Should skip from pending"
    assert task3.status == TaskStatus.SKIPPED
    print(f"  [OK] Task skipped")

    # Test not_applicable
    task4 = Task("na_1", "Test not applicable", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    task4.start("Start")
    assert task4.mark_not_applicable("Goal changed"), "Should mark not applicable"
    assert task4.status == TaskStatus.NOT_APPLICABLE
    print(f"  [OK] Task marked not applicable")

    print("\n[PASS] All terminal transitions working\n")
    return True


def test_state_history_tracking():
    """Test state history is properly tracked"""
    print("="*70)
    print("TEST 8: State History Tracking")
    print("="*70)

    task = Task(
        task_id="history_task",
        description="Test history tracking",
        task_type=TaskType.PRE_ASSIGNED,
        status=TaskStatus.PENDING
    )

    # Initial state
    history = task.get_state_history()
    assert len(history) == 1, "Should have initial state"
    assert history[0]["status"] == TaskStatus.PENDING
    print(f"  [OK] Initial history entry: {history[0]['reason']}")

    # Make transitions
    task.start("Starting work")
    task.pause("Taking a break")
    task.resume("Continuing")
    task.complete(result="Done")

    # Check history
    history = task.get_state_history()
    assert len(history) == 6, f"Should have 6 entries (got {len(history)})"  # pending, in_progress, paused, resuming, in_progress, completed

    expected_sequence = [
        TaskStatus.PENDING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.PAUSED,
        TaskStatus.RESUMING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.COMPLETED
    ]

    for i, expected in enumerate(expected_sequence):
        actual = history[i]["status"]
        assert actual == expected, f"Entry {i}: expected {expected}, got {actual}"
        print(f"  [OK] History[{i}]: {actual} - {history[i]['reason']}")

    print(f"\n[PASS] State history tracking working correctly\n")
    return True


def test_invalid_transitions():
    """Test that invalid transitions are rejected"""
    print("="*70)
    print("TEST 9: Invalid Transition Validation")
    print("="*70)

    # Try to complete a pending task (invalid - must start first)
    task = Task("invalid_1", "Test invalid", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)

    # PENDING can't complete directly (must start first)
    assert not task.complete(result="Try to complete pending"), "Should not complete from pending"
    assert task.status == TaskStatus.PENDING, "Status should remain pending"
    print(f"  [OK] Cannot complete pending task directly")

    # Start then complete
    task.start("Start")
    task.complete(result="Done")

    # Try to resume completed task (terminal state)
    assert not task.resume("Try to resume completed"), "Should not resume terminal state"
    assert task.status == TaskStatus.COMPLETED, "Status should remain completed"
    print(f"  [OK] Cannot resume terminal task")

    # Create blocked task
    task2 = Task("invalid_2", "Test blocked", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    task2.start("Start")
    task2.block("Blocked")

    # Blocked can't complete directly
    assert not task2.complete(result="Try to complete"), "Should not complete from blocked"
    assert task2.status == TaskStatus.BLOCKED, "Status should remain blocked"
    print(f"  [OK] Cannot complete blocked task directly")

    print("\n[PASS] Invalid transitions properly rejected\n")
    return True


def test_ledger_state_methods():
    """Test SmartLedger state management methods"""
    print("="*70)
    print("TEST 10: SmartLedger State Methods")
    print("="*70)

    backend = get_production_backend()
    ledger = SmartLedger(agent_id=TEST_USER_ID, session_id=TEST_PROMPT_ID, backend=backend)

    # Add various tasks in different states
    tasks = [
        Task("ledger_1", "Pending task", TaskType.PRE_ASSIGNED, status=TaskStatus.PENDING),
        Task("ledger_2", "In progress task", TaskType.AUTONOMOUS, status=TaskStatus.IN_PROGRESS),
        Task("ledger_3", "Paused task", TaskType.USER_REQUESTED, status=TaskStatus.PAUSED),
        Task("ledger_4", "Completed task", TaskType.PRE_ASSIGNED, status=TaskStatus.COMPLETED),
        Task("ledger_5", "Another in progress", TaskType.AUTONOMOUS, status=TaskStatus.IN_PROGRESS),
    ]

    for task in tasks:
        ledger.add_task(task)

    print(f"  [OK] Added {len(tasks)} tasks in various states")

    # Test get methods
    active_tasks = ledger.get_active_tasks()
    assert len(active_tasks) == 2, f"Should have 2 active tasks (got {len(active_tasks)})"
    print(f"  [OK] get_active_tasks(): {len(active_tasks)} tasks")

    paused_tasks = ledger.get_paused_tasks()
    assert len(paused_tasks) == 1, "Should have 1 paused task"
    print(f"  [OK] get_paused_tasks(): {len(paused_tasks)} task")

    terminal_tasks = ledger.get_terminal_tasks()
    assert len(terminal_tasks) == 1, "Should have 1 terminal task"
    print(f"  [OK] get_terminal_tasks(): {len(terminal_tasks)} task")

    # Test pause/resume operations
    success = ledger.pause_task("ledger_2", reason="Test pause")
    assert success, "Should pause task successfully"
    print(f"  [OK] pause_task() successful")

    success = ledger.resume_task("ledger_2", reason="Test resume")
    assert success, "Should resume task successfully"
    print(f"  [OK] resume_task() successful")

    # Test bulk operations
    paused_count = ledger.pause_all_active_tasks(reason="Pause all")
    print(f"  [OK] pause_all_active_tasks(): paused {paused_count} tasks")

    resumed_count = ledger.resume_all_paused_tasks(reason="Resume all")
    print(f"  [OK] resume_all_paused_tasks(): resumed {resumed_count} tasks")

    # Test state summary
    state_summary = ledger.get_task_state_summary()
    print(f"  [OK] get_task_state_summary(): {state_summary}")

    # Test detailed progress
    progress = ledger.get_detailed_progress()
    assert progress["total_tasks"] == len(tasks)
    print(f"  [OK] get_detailed_progress(): {progress['total_tasks']} total tasks")

    print("\n[PASS] SmartLedger state methods working correctly\n")
    return True


def test_state_duration():
    """Test getting current state duration"""
    print("="*70)
    print("TEST 11: State Duration Tracking")
    print("="*70)

    task = Task("duration_test", "Test duration", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)

    # Wait a bit
    time.sleep(0.5)

    duration = task.get_current_state_duration()
    assert duration >= 0.5, f"Duration should be at least 0.5 seconds (got {duration})"
    print(f"  [OK] Task in PENDING for {duration:.2f} seconds")

    # Transition to in_progress
    task.start("Start")
    time.sleep(0.3)

    duration = task.get_current_state_duration()
    assert duration >= 0.3, f"Duration should be at least 0.3 seconds (got {duration})"
    print(f"  [OK] Task in IN_PROGRESS for {duration:.2f} seconds")

    print("\n[PASS] State duration tracking working\n")
    return True


def test_all_terminal_methods():
    """Test all terminal state methods"""
    print("="*70)
    print("TEST 12: All Terminal State Methods")
    print("="*70)

    backend = get_production_backend()
    ledger = SmartLedger(agent_id=TEST_USER_ID, session_id=TEST_PROMPT_ID+1, backend=backend)

    # Test skip_task
    task1 = Task("term_skip", "Skip test", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    ledger.add_task(task1)
    success = ledger.skip_task("term_skip", reason="Not needed")
    assert success, "Should skip task"
    assert ledger.get_task("term_skip").status == TaskStatus.SKIPPED
    print(f"  [OK] skip_task() working")

    # Test mark_task_not_applicable
    task2 = Task("term_na", "N/A test", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    ledger.add_task(task2)
    success = ledger.mark_task_not_applicable("term_na", reason="Goal changed")
    assert success, "Should mark not applicable"
    assert ledger.get_task("term_na").status == TaskStatus.NOT_APPLICABLE
    print(f"  [OK] mark_task_not_applicable() working")

    # Test terminate_task
    task3 = Task("term_kill", "Terminate test", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    task3.start("Start")
    ledger.add_task(task3)
    success = ledger.terminate_task("term_kill", reason="Emergency stop")
    assert success, "Should terminate task"
    assert ledger.get_task("term_kill").status == TaskStatus.TERMINATED
    print(f"  [OK] terminate_task() working")

    # Test user_stop_task
    task4 = Task("term_userstop", "User stop test", TaskType.AUTONOMOUS, status=TaskStatus.PENDING)
    task4.start("Start")
    ledger.add_task(task4)
    success = ledger.user_stop_task("term_userstop", reason="User stopped")
    assert success, "Should user stop task"
    assert ledger.get_task("term_userstop").status == TaskStatus.USER_STOPPED
    print(f"  [OK] user_stop_task() working")

    print("\n[PASS] All terminal state methods working\n")
    return True


def run_all_tests():
    """Run all state management tests"""
    print("\n" + "="*70)
    print(" TASK STATE MANAGEMENT TEST SUITE")
    print("="*70)
    print(f"\nTest User ID: {TEST_USER_ID}")
    print(f"Test Prompt ID: {TEST_PROMPT_ID}\n")

    tests = [
        ("All States Defined", test_all_states_defined),
        ("State Categories", test_state_categories),
        ("Basic Transitions", test_basic_task_transitions),
        ("Pause/Resume Cycle", test_pause_resume_cycle),
        ("User Stop Functionality", test_user_stop_functionality),
        ("Block and Fail States", test_block_and_fail),
        ("Termination and Cancellation", test_termination_and_cancellation),
        ("State History Tracking", test_state_history_tracking),
        ("Invalid Transition Validation", test_invalid_transitions),
        ("SmartLedger State Methods", test_ledger_state_methods),
        ("State Duration Tracking", test_state_duration),
        ("All Terminal Methods", test_all_terminal_methods),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
                print(f"[FAIL] {name} did not pass")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {name} raised exception: {e}")
            import traceback
            traceback.print_exc()

    print("="*70)
    print(f" TEST RESULTS")
    print("="*70)
    print(f"\nPassed: {passed}/{len(tests)}")
    print(f"Failed: {failed}/{len(tests)}")

    if failed == 0:
        print("\n[SUCCESS] All tests passed!\n")
        return True
    else:
        print(f"\n[FAILURE] {failed} tests failed\n")
        return False


if __name__ == "__main__":
    import sys
    success = run_all_tests()
    sys.exit(0 if success else 1)
