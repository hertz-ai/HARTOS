"""
Direct test of nested task system without Flask server.
This validates the core functionality of nested tasks, auto-resume, and events.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent_ledger import (
    SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
)
from datetime import datetime

def print_header(text):
    """Print a section header"""
    print("\n" + "="*80)
    print(f"  {text}")
    print("="*80)

def print_task_status(ledger, task_id):
    """Print task status"""
    task = ledger.get_task(task_id)
    if task:
        print(f"  Task {task_id}: {task.status.name} | Blocked by: {task.blocked_by}")

def test_sequential_tasks_with_auto_resume():
    """Test sequential tasks with deterministic auto-resume"""
    print_header("TEST: Sequential Tasks with Auto-Resume")

    # Create ledger
    ledger = SmartLedger(agent_id="test_user", session_id="test_seq")

    # Create 3 sequential tasks
    task_descriptions = [
        "Phase 1: Setup repository",
        "Phase 2: Design specification",
        "Phase 3: Implementation"
    ]

    task_list = ledger.create_sequential_tasks(
        task_descriptions=task_descriptions
    )
    task_ids = [t.task_id for t in task_list]

    print(f"\n[CREATED] {len(task_ids)} sequential tasks:")
    for task_id in task_ids:
        print_task_status(ledger, task_id)

    # Verify first task is PENDING, rest are BLOCKED
    task1 = ledger.get_task(task_ids[0])
    task2 = ledger.get_task(task_ids[1])
    task3 = ledger.get_task(task_ids[2])

    assert task1.status == TaskStatus.PENDING, f"Task 1 should be PENDING, got {task1.status}"
    assert task2.status == TaskStatus.BLOCKED, f"Task 2 should be BLOCKED, got {task2.status}"
    assert task3.status == TaskStatus.BLOCKED, f"Task 3 should be BLOCKED, got {task3.status}"

    print("\n[PASS] Initial states correct")

    # Start and complete Task 1
    print("\n[ACTION] Starting Task 1...")
    ledger.update_task_status(task_ids[0], TaskStatus.IN_PROGRESS)
    ledger.update_task_status(task_ids[0], TaskStatus.COMPLETED,
                              result={"status": "repository_ready"})

    # Check if Task 2 auto-resumed
    task2_updated = ledger.get_task(task_ids[1])
    print(f"\n[CHECK] After Task 1 completes:")
    print_task_status(ledger, task_ids[1])

    # Task 2 should auto-resume to IN_PROGRESS (not PENDING) when unblocked
    assert task2_updated.status == TaskStatus.IN_PROGRESS, \
        f"Task 2 should auto-resume to IN_PROGRESS, got {task2_updated.status}"
    assert len(task2_updated.blocked_by) == 0, \
        f"Task 2 should have no blockers, got {task2_updated.blocked_by}"

    print("[PASS] Task 2 auto-resumed to IN_PROGRESS!")

    # Check events were generated
    events = ledger.get_events(event_type="task_auto_resumed")
    print(f"\n[EVENTS] {len(events)} auto-resume events generated")
    for event in events:
        print(f"  - {event['type']}: Task {event['data']['task_id']}")

    assert len(events) > 0, "Expected auto-resume events"
    print("[PASS] Events generated correctly")

    # Complete Task 2
    print("\n[ACTION] Starting and completing Task 2...")
    ledger.update_task_status(task_ids[1], TaskStatus.IN_PROGRESS)
    ledger.update_task_status(task_ids[1], TaskStatus.COMPLETED,
                              result={"status": "design_ready"})

    # Check if Task 3 auto-resumed
    task3_updated = ledger.get_task(task_ids[2])
    print(f"\n[CHECK] After Task 2 completes:")
    print_task_status(ledger, task_ids[2])

    assert task3_updated.status == TaskStatus.IN_PROGRESS, \
        f"Task 3 should auto-resume to IN_PROGRESS, got {task3_updated.status}"

    print("[PASS] Task 3 auto-resumed to IN_PROGRESS!")

    print("\n" + "="*80)
    print("  ALL TESTS PASSED - Sequential auto-resume working!")
    print("="*80)

def test_parallel_tasks():
    """Test sibling tasks (parallel execution)"""
    print_header("TEST: Parallel (Sibling) Tasks")

    ledger = SmartLedger(agent_id="test_user", session_id="test_parallel")

    # Create parent task directly
    parent = Task(
        task_id="parent_test",
        description="Run all tests",
        task_type=TaskType.PRE_ASSIGNED
    )
    ledger.tasks[parent.task_id] = parent
    ledger.save()
    parent_id = parent.task_id

    # Create 4 sibling tasks (parallel test types)
    test_types = [
        "Unit tests",
        "Integration tests",
        "Functional tests",
        "Performance tests"
    ]

    sibling_list = ledger.create_sibling_tasks(
        parent_task_id=parent_id,
        sibling_descriptions=test_types
    )
    sibling_ids = [t.task_id for t in sibling_list]

    print(f"\n[CREATED] Parent task and {len(sibling_ids)} sibling tasks")
    print(f"  Parent: {parent_id}")
    for sid in sibling_ids:
        task = ledger.get_task(sid)
        print(f"  Sibling: {sid} - {task.description} (Status: {task.status.name})")

    # Verify all siblings are PENDING (can run in parallel)
    for sid in sibling_ids:
        task = ledger.get_task(sid)
        assert task.status == TaskStatus.PENDING, \
            f"Sibling {sid} should be PENDING for parallel execution"

    print("\n[PASS] All sibling tasks ready for parallel execution")

    # Verify cross-registration
    for i, sid in enumerate(sibling_ids):
        task = ledger.get_task(sid)
        other_siblings = [s for j, s in enumerate(sibling_ids) if j != i]
        for other in other_siblings:
            assert other in task.sibling_task_ids, \
                f"Task {sid} should know about sibling {other}"

    print("[PASS] Siblings cross-registered correctly")

    print("\n" + "="*80)
    print("  ALL TESTS PASSED - Parallel tasks working!")
    print("="*80)

def test_nested_hierarchy():
    """Test parent-child nested hierarchy"""
    print_header("TEST: Parent-Child Hierarchy")

    ledger = SmartLedger(agent_id="test_user", session_id="test_nested")

    # Create root task directly
    root = Task(
        task_id="root_feature",
        description="Implement User Authentication Feature",
        task_type=TaskType.PRE_ASSIGNED
    )
    ledger.tasks[root.task_id] = root
    ledger.save()
    root_id = root.task_id

    # Create Phase 1 as child
    phase1 = ledger.create_parent_child_task(
        parent_task_id=root_id,
        child_description="Phase 1: Setup & Validation"
    )
    phase1_id = phase1.task_id

    # Create Phase 2 as child
    phase2 = ledger.create_parent_child_task(
        parent_task_id=root_id,
        child_description="Phase 2: Design Specification"
    )
    phase2_id = phase2.task_id

    print(f"\n[CREATED] Nested hierarchy:")
    print(f"  Root: {root_id}")
    print(f"    Child: {phase1_id}")
    print(f"    Child: {phase2_id}")

    # Verify parent knows about children
    root = ledger.get_task(root_id)
    assert phase1_id in root.child_task_ids, "Root should know about Phase 1"
    assert phase2_id in root.child_task_ids, "Root should know about Phase 2"

    print("\n[PASS] Parent-child relationships established")

    # Get task tree
    tree = ledger.get_task_tree(root_id)
    print(f"\n[TREE] Task tree structure:")
    print(f"  Root has {len(tree.get('children', []))} children")

    print("\n" + "="*80)
    print("  ALL TESTS PASSED - Nested hierarchy working!")
    print("="*80)

def test_inter_task_communication():
    """Test message passing between tasks"""
    print_header("TEST: Inter-Task Communication")

    ledger = SmartLedger(agent_id="test_user", session_id="test_comm")

    # Create two sequential tasks
    task_list = ledger.create_sequential_tasks(
        task_descriptions=["Task A: Generate data", "Task B: Process data"]
    )
    task_ids = [t.task_id for t in task_list]

    task_a_id = task_ids[0]
    task_b_id = task_ids[1]

    # Task A completes with result
    task_a = ledger.get_task(task_a_id)
    result_data = {"data": [1, 2, 3, 4, 5], "count": 5}
    task_a.send_message_to_dependents({
        "message_type": "result",
        "data": result_data
    })
    ledger.update_task_status(task_a_id, TaskStatus.IN_PROGRESS)
    ledger.update_task_status(task_a_id, TaskStatus.COMPLETED, result=result_data)

    # Check Task B received the message
    task_b = ledger.get_task(task_b_id)
    messages = task_b.received_messages

    print(f"\n[CHECK] Task B received {len(messages)} messages")
    for msg in messages:
        print(f"  Type: {msg.get('message_type')}, From: {msg.get('from_task_id')}")

    assert len(messages) > 0, "Task B should have received messages"

    # Verify Task B can access predecessor results
    prereq_results = task_b.get_prerequisite_results()
    print(f"\n[RESULTS] Task B has access to {len(prereq_results)} prerequisite results")

    assert task_a_id in prereq_results, "Task B should have Task A's results"
    assert prereq_results[task_a_id] == result_data, "Results should match"

    print("[PASS] Inter-task communication working!")

    print("\n" + "="*80)
    print("  ALL TESTS PASSED - Message passing working!")
    print("="*80)

def main():
    """Run all tests"""
    print("\n" + "#"*80)
    print("#  NESTED TASK SYSTEM - DIRECT VALIDATION TESTS")
    print("#"*80)
    print(f"\nStart Time: {datetime.now().isoformat()}")

    try:
        # Test 1: Sequential tasks with auto-resume
        test_sequential_tasks_with_auto_resume()

        # Test 2: Parallel tasks
        test_parallel_tasks()

        # Test 3: Nested hierarchy
        test_nested_hierarchy()

        # Test 4: Inter-task communication
        test_inter_task_communication()

        print("\n" + "#"*80)
        print("#  ALL NESTED TASK TESTS PASSED SUCCESSFULLY!")
        print("#"*80)
        print(f"End Time: {datetime.now().isoformat()}\n")

        return 0

    except AssertionError as e:
        print(f"\n[FAIL] Test failed: {e}\n")
        return 1
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}\n")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
