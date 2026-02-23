#!/usr/bin/env python3
"""
Comprehensive Test Suite for Nested Task Management System

Tests:
1. Parent-child task relationships
2. Sibling task creation and cross-registration
3. Sequential task chains with auto-dependencies
4. Deterministic auto-resume on dependency completion
5. Event generation and querying
6. Inter-task communication and message passing
7. Result passing between dependent tasks
8. Task tree visualization
9. Hierarchy queries
10. Dependency status queries
11. Complex multi-level workflows
12. Event-driven agent patterns
"""

import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent_ledger import SmartLedger, Task, TaskType, TaskStatus

def print_test_header(test_num: int, test_name: str):
    """Print formatted test header."""
    print(f"\n{'='*70}")
    print(f" TEST {test_num}: {test_name}")
    print(f"{'='*70}\n")

def print_result(passed: bool, message: str = ""):
    """Print test result."""
    status = "[PASS]" if passed else "[FAIL]"
    print(f"{status}: {message}")
    return passed

# Test counters
passed = 0
failed = 0

print("="*70)
print(" NESTED TASK MANAGEMENT SYSTEM - COMPLETE TEST SUITE")
print("="*70)
print(f"Testing: Ledger + Events + Deterministic Auto-Resume")
print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("="*70)

# ==================== TEST 1: Parent-Child Relationships ====================
print_test_header(1, "Parent-Child Task Relationships")

ledger = SmartLedger(agent_id=999, session_id=8001)

# Create parent task
parent = Task(
    task_id="parent_1",
    description="Main project task",
    task_type=TaskType.PRE_ASSIGNED,
    status=TaskStatus.PENDING
)
ledger.tasks["parent_1"] = parent

# Create child tasks
child1 = ledger.create_parent_child_task(
    parent_task_id="parent_1",
    child_description="Subtask 1"
)
child2 = ledger.create_parent_child_task(
    parent_task_id="parent_1",
    child_description="Subtask 2"
)

# Verify relationships
test_passed = (
    child1 is not None and
    child2 is not None and
    child1.task_id in parent.child_task_ids and
    child2.task_id in parent.child_task_ids and
    child1.parent_task_id == "parent_1" and
    child2.parent_task_id == "parent_1" and
    len(parent.child_task_ids) == 2
)

if print_result(test_passed, "Parent-child relationships established correctly"):
    passed += 1
    print(f"   Parent: {parent.task_id}")
    print(f"   Children: {parent.child_task_ids}")
else:
    failed += 1
    print(f"   ERROR: Parent children: {parent.child_task_ids}")

# ==================== TEST 2: Sibling Tasks ====================
print_test_header(2, "Sibling Task Creation and Cross-Registration")

siblings = ledger.create_sibling_tasks(
    parent_task_id="parent_1",
    sibling_descriptions=[
        "Parallel task A",
        "Parallel task B",
        "Parallel task C"
    ]
)

# Verify sibling relationships
test_passed = (
    len(siblings) == 3 and
    all(s.parent_task_id == "parent_1" for s in siblings) and
    all(len(s.sibling_task_ids) == 2 for s in siblings)  # Each has 2 siblings
)

# Verify cross-registration
sibling_ids = [s.task_id for s in siblings]
cross_registered = all(
    all(other_id in s.sibling_task_ids for other_id in sibling_ids if other_id != s.task_id)
    for s in siblings
)

if print_result(test_passed and cross_registered, "Sibling tasks created and cross-registered"):
    passed += 1
    print(f"   Siblings: {sibling_ids}")
    print(f"   Each sibling knows about {len(siblings[0].sibling_task_ids)} others")
else:
    failed += 1
    print(f"   ERROR: Cross-registration failed")

# ==================== TEST 3: Sequential Task Chain ====================
print_test_header(3, "Sequential Task Chain with Auto-Dependencies")

ledger2 = SmartLedger(agent_id=999, session_id=8002)

sequential = ledger2.create_sequential_tasks(
    task_descriptions=[
        "Step 1: Initialize",
        "Step 2: Process data",
        "Step 3: Generate report",
        "Step 4: Send notification"
    ]
)

# Verify sequential dependencies
test_passed = (
    len(sequential) == 4 and
    sequential[0].status == TaskStatus.PENDING and  # First not blocked
    sequential[1].status == TaskStatus.BLOCKED and  # Rest blocked
    sequential[2].status == TaskStatus.BLOCKED and
    sequential[3].status == TaskStatus.BLOCKED and
    sequential[1].task_id in sequential[0].dependent_task_ids and
    sequential[0].task_id in sequential[1].blocked_by and
    sequential[0].task_id in sequential[1].prerequisites
)

if print_result(test_passed, "Sequential chain created with automatic blocking"):
    passed += 1
    for i, task in enumerate(sequential, 1):
        print(f"   Step {i}: {task.task_id} - Status: {task.status}, Blocked by: {task.blocked_by}")
else:
    failed += 1
    print(f"   ERROR: Dependencies not set up correctly")

# ==================== TEST 4: Deterministic Auto-Resume ====================
print_test_header(4, "Deterministic Auto-Resume on Dependency Completion")

# Clear events before test
ledger2.clear_events()

# Start and complete first task
sequential[0].start("Begin step 1")
ledger2.update_task_status(
    sequential[0].task_id,
    TaskStatus.COMPLETED,
    result={"data": "initialized", "status": "success"}
)

# Small delay for processing
time.sleep(0.1)

# Check if second task auto-resumed (DETERMINISTIC RULE)
test_passed = (
    sequential[1].status == TaskStatus.IN_PROGRESS and  # Auto-resumed!
    not sequential[1].is_blocked() and
    sequential[0].task_id not in sequential[1].blocked_by
)

# Check event was generated
events = ledger2.get_events(event_type="task_auto_resumed")
event_generated = len(events) > 0 and events[0]["data"]["task_id"] == sequential[1].task_id

if print_result(test_passed and event_generated, "Task 2 auto-resumed by deterministic rule"):
    passed += 1
    print(f"   Task 1: {sequential[0].status}")
    print(f"   Task 2: {sequential[1].status} (was BLOCKED, now auto-resumed)")
    print(f"   Event generated: {event_generated}")
    print(f"   Event data: {events[0]['data'] if events else 'None'}")
else:
    failed += 1
    print(f"   ERROR: Task 2 status: {sequential[1].status}")
    print(f"   ERROR: Task 2 blocked_by: {sequential[1].blocked_by}")
    print(f"   ERROR: Events: {events}")

# ==================== TEST 5: Event System ====================
print_test_header(5, "Event Generation and Querying")

# Get all events
all_events = ledger2.get_events()

# Get specific event types
completed_events = ledger2.get_events(event_type="task_completed")
resumed_events = ledger2.get_events(event_type="task_auto_resumed")

test_passed = (
    len(all_events) >= 2 and  # At least completion + auto-resume
    len(completed_events) >= 1 and
    len(resumed_events) >= 1 and
    completed_events[0]["type"] == "task_completed" and
    resumed_events[0]["type"] == "task_auto_resumed"
)

if print_result(test_passed, "Events generated and queryable"):
    passed += 1
    print(f"   Total events: {len(all_events)}")
    print(f"   Completion events: {len(completed_events)}")
    print(f"   Auto-resume events: {len(resumed_events)}")
    print(f"   Latest event: {all_events[-1]['type'] if all_events else 'None'}")
else:
    failed += 1
    print(f"   ERROR: Event counts incorrect")

# Test event filtering by timestamp
timestamp_before = datetime.now().isoformat()
time.sleep(0.2)  # Increased sleep time for timestamp differentiation

# Complete another task - use update_task_status to trigger auto-resume chain
ledger2.update_task_status(
    sequential[1].task_id,
    TaskStatus.COMPLETED,
    result={"processed": 100}
)
time.sleep(0.1)  # Allow event to be recorded

new_events = ledger2.get_events(since=timestamp_before)
timestamp_filter_works = len(new_events) >= 1

if print_result(timestamp_filter_works, "Event timestamp filtering works"):
    passed += 1
    print(f"   New events since timestamp: {len(new_events)}")
else:
    failed += 1
    print(f"   Note: Timestamp may be too close, got {len(new_events)} events")

# ==================== TEST 6: Inter-Task Communication ====================
print_test_header(6, "Inter-Task Communication and Message Passing")

# Check messages delivered to task 2 from task 1
messages = sequential[1].get_messages_from_prerequisites()
result_messages = sequential[1].get_messages_from_prerequisites(message_type="result")

test_passed = (
    len(messages) >= 1 and
    len(result_messages) >= 1 and
    result_messages[0].get("from_task_id") == sequential[0].task_id and
    "data" in result_messages[0]
)

if print_result(test_passed, "Messages delivered from prerequisite to dependent"):
    passed += 1
    print(f"   Total messages: {len(messages)}")
    print(f"   Result messages: {len(result_messages)}")
    if result_messages:
        print(f"   Result data: {result_messages[0].get('data')}")
else:
    failed += 1
    print(f"   ERROR: Messages: {messages}")

# ==================== TEST 7: Result Passing ====================
print_test_header(7, "Result Passing Between Dependent Tasks")

prerequisite_results = sequential[1].get_prerequisite_results()

test_passed = (
    sequential[0].task_id in prerequisite_results and
    prerequisite_results[sequential[0].task_id]["data"] == "initialized"
)

if print_result(test_passed, "Results extracted from prerequisite tasks"):
    passed += 1
    print(f"   Prerequisite results: {prerequisite_results}")
else:
    failed += 1
    print(f"   ERROR: Results: {prerequisite_results}")

# ==================== TEST 8: Full Chain Auto-Resume ====================
print_test_header(8, "Full Sequential Chain Auto-Resume")

# Task 2 was completed in test 5, so task 3 should have auto-resumed
# Let's verify the chain progression
task1_done = sequential[0].status == TaskStatus.COMPLETED
task2_done = sequential[1].status == TaskStatus.COMPLETED
task3_resumed = sequential[2].status == TaskStatus.IN_PROGRESS

# Complete task 3 if it's running - use update_task_status for auto-resume chain
if task3_resumed:
    ledger2.update_task_status(
        sequential[2].task_id,
        TaskStatus.COMPLETED,
        result={"report": "generated"}
    )
    time.sleep(0.1)

# Check task 4 auto-resumed
task4_resumed = sequential[3].status == TaskStatus.IN_PROGRESS

test_passed = task1_done and task2_done and task3_resumed and task4_resumed

if print_result(test_passed, "Entire chain auto-resumed sequentially"):
    passed += 1
    for i, task in enumerate(sequential, 1):
        print(f"   Step {i}: {task.status}")
else:
    failed += 1
    print(f"   Note: Tasks completed in previous tests, checking chain consistency")
    for i, task in enumerate(sequential, 1):
        print(f"   Step {i}: {task.status}")

# ==================== TEST 9: Task Tree Visualization ====================
print_test_header(9, "Task Tree Visualization")

# Create complex hierarchy
ledger3 = SmartLedger(agent_id=999, session_id=8003)
root = Task(
    task_id="root",
    description="Root task",
    task_type=TaskType.PRE_ASSIGNED,
    status=TaskStatus.IN_PROGRESS
)
ledger3.tasks["root"] = root

# Add children
child_a = ledger3.create_parent_child_task("root", "Child A")
child_b = ledger3.create_parent_child_task("root", "Child B")

# Add grandchildren
grandchild_a1 = ledger3.create_parent_child_task(child_a.task_id, "Grandchild A1")
grandchild_a2 = ledger3.create_parent_child_task(child_a.task_id, "Grandchild A2")

# Get tree visualization
tree_viz = ledger3.visualize_task_tree("root")

test_passed = (
    tree_viz is not None and
    len(tree_viz) > 0 and
    "root" in tree_viz and
    "Child A" in tree_viz and
    "Grandchild A1" in tree_viz
)

if print_result(test_passed, "Task tree visualization generated"):
    passed += 1
    print("\n" + tree_viz + "\n")
else:
    failed += 1
    print(f"   ERROR: Visualization: {tree_viz}")

# ==================== TEST 10: Task Tree Structure ====================
print_test_header(10, "Task Tree Data Structure")

tree_structure = ledger3.get_task_tree("root")

test_passed = (
    tree_structure is not None and
    tree_structure["task_id"] == "root" and
    len(tree_structure["children"]) == 2 and
    len(tree_structure["children"][0]["children"]) == 2  # Child A has 2 children
)

if print_result(test_passed, "Task tree structure correctly built"):
    passed += 1
    print(f"   Root: {tree_structure['task_id']}")
    print(f"   Direct children: {len(tree_structure['children'])}")
    print(f"   Child A's children: {len(tree_structure['children'][0]['children'])}")
else:
    failed += 1
    print(f"   ERROR: Tree structure: {tree_structure}")

# ==================== TEST 11: Hierarchy Queries ====================
print_test_header(11, "Hierarchy Query Methods")

# Get all descendants
descendants = ledger3.get_all_descendants("root")

# Get task depths
root_depth = ledger3.get_task_depth("root")
child_depth = ledger3.get_task_depth(child_a.task_id)
grandchild_depth = ledger3.get_task_depth(grandchild_a1.task_id)

test_passed = (
    len(descendants) == 4 and  # 2 children + 2 grandchildren
    root_depth == 0 and
    child_depth == 1 and
    grandchild_depth == 2
)

if print_result(test_passed, "Hierarchy queries return correct results"):
    passed += 1
    print(f"   Total descendants: {len(descendants)}")
    print(f"   Depths: root={root_depth}, child={child_depth}, grandchild={grandchild_depth}")
else:
    failed += 1
    print(f"   ERROR: Descendants: {len(descendants)}, Depths: {root_depth}, {child_depth}, {grandchild_depth}")

# ==================== TEST 12: Dependency Status Queries ====================
print_test_header(12, "Dependency Status Queries")

# Get dependency status for sequential tasks
dep_status = ledger2.get_dependency_status(sequential[3].task_id)

test_passed = (
    dep_status is not None and
    dep_status["task_id"] == sequential[3].task_id and
    "is_blocked" in dep_status and
    "blocked_by" in dep_status and
    "dependents" in dep_status and
    "ready_to_resume" in dep_status
)

if print_result(test_passed, "Dependency status query returns comprehensive data"):
    passed += 1
    print(f"   Task: {dep_status.get('task_id')}")
    print(f"   Is blocked: {dep_status.get('is_blocked')}")
    print(f"   Blocked by: {dep_status.get('blocked_by')}")
    print(f"   Ready to resume: {dep_status.get('ready_to_resume')}")
else:
    failed += 1
    print(f"   ERROR: Status: {dep_status}")

# ==================== TEST 13: Get Tasks Ready to Resume ====================
print_test_header(13, "Get Tasks Ready to Resume Query")

# Create new ledger with blocked tasks
ledger4 = SmartLedger(agent_id=999, session_id=8004)

task_a = Task(task_id="task_a", description="Task A", task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.PENDING)
task_b = Task(task_id="task_b", description="Task B", task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.BLOCKED)
task_c = Task(task_id="task_c", description="Task C", task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.BLOCKED)

# Task B is blocked but has no blockers (ready to resume)
task_b.blocked_by = []

# Task C is blocked and has blockers (not ready)
task_c.blocked_by = ["task_a"]

ledger4.tasks["task_a"] = task_a
ledger4.tasks["task_b"] = task_b
ledger4.tasks["task_c"] = task_c

# Get ready tasks
ready_tasks = ledger4.get_tasks_ready_to_resume()

test_passed = (
    len(ready_tasks) == 1 and
    ready_tasks[0].task_id == "task_b"
)

if print_result(test_passed, "Correctly identifies tasks ready to resume"):
    passed += 1
    print(f"   Ready tasks: {[t.task_id for t in ready_tasks]}")
else:
    failed += 1
    print(f"   ERROR: Ready tasks: {[t.task_id for t in ready_tasks]}")

# ==================== TEST 14: Get Tasks Blocked By ====================
print_test_header(14, "Get Tasks Blocked By Query")

# Get tasks blocked by task_a
blocked_by_a = ledger4.get_tasks_blocked_by("task_a")

test_passed = (
    len(blocked_by_a) == 1 and
    blocked_by_a[0].task_id == "task_c"
)

if print_result(test_passed, "Correctly identifies tasks blocked by specific task"):
    passed += 1
    print(f"   Tasks blocked by task_a: {[t.task_id for t in blocked_by_a]}")
else:
    failed += 1
    print(f"   ERROR: Blocked tasks: {[t.task_id for t in blocked_by_a]}")

# ==================== TEST 15: Prerequisites Completion Check ====================
print_test_header(15, "Prerequisites Completion Check")

# Create tasks with prerequisites
ledger5 = SmartLedger(agent_id=999, session_id=8005)

pre1 = Task(task_id="pre1", description="Prerequisite 1", task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.PENDING)
pre2 = Task(task_id="pre2", description="Prerequisite 2", task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.PENDING)
dependent = Task(
    task_id="dependent",
    description="Dependent task",
    task_type=TaskType.PRE_ASSIGNED,
    status=TaskStatus.BLOCKED,
    prerequisites=["pre1", "pre2"]
)

ledger5.tasks["pre1"] = pre1
ledger5.tasks["pre2"] = pre2
ledger5.tasks["dependent"] = dependent

# Initially not all completed
check1 = dependent.has_all_prerequisites_completed(ledger5)

# Complete pre1
pre1.status = TaskStatus.COMPLETED
check2 = dependent.has_all_prerequisites_completed(ledger5)

# Complete pre2
pre2.status = TaskStatus.COMPLETED
check3 = dependent.has_all_prerequisites_completed(ledger5)

test_passed = (
    check1 == False and
    check2 == False and
    check3 == True
)

if print_result(test_passed, "Prerequisites completion check works correctly"):
    passed += 1
    print(f"   Before: {check1}")
    print(f"   After pre1: {check2}")
    print(f"   After both: {check3}")
else:
    failed += 1
    print(f"   ERROR: Checks: {check1}, {check2}, {check3}")

# ==================== TEST 16: Children Completion Check ====================
print_test_header(16, "Children Completion Check")

# Use hierarchy from test 9
check1 = root.has_all_children_completed(ledger3)

# Complete all children and grandchildren
for child_id in root.child_task_ids:
    child = ledger3.get_task(child_id)
    if child:
        child.status = TaskStatus.COMPLETED
        for grandchild_id in child.child_task_ids:
            grandchild = ledger3.get_task(grandchild_id)
            if grandchild:
                grandchild.status = TaskStatus.COMPLETED

check2 = root.has_all_children_completed(ledger3)

test_passed = (
    check1 == False and
    check2 == True
)

if print_result(test_passed, "Children completion check works correctly"):
    passed += 1
    print(f"   Before: {check1}")
    print(f"   After: {check2}")
else:
    failed += 1
    print(f"   ERROR: Checks: {check1}, {check2}")

# ==================== TEST 17: Event Clearing ====================
print_test_header(17, "Event Clearing")

# Get current event count
events_before = len(ledger2.get_events())

# Clear events
ledger2.clear_events()

# Get event count after clearing
events_after = len(ledger2.get_events())

test_passed = (
    events_before > 0 and
    events_after == 0
)

if print_result(test_passed, "Event clearing works correctly"):
    passed += 1
    print(f"   Events before clear: {events_before}")
    print(f"   Events after clear: {events_after}")
else:
    failed += 1
    print(f"   ERROR: Before: {events_before}, After: {events_after}")

# ==================== TEST 18: Complete Workflow Integration ====================
print_test_header(18, "Complete Workflow Integration Test")

# Create a complete workflow with events
ledger6 = SmartLedger(agent_id=999, session_id=8006)
ledger6.clear_events()

# Create sequential workflow
workflow = ledger6.create_sequential_tasks([
    "Step 1: Setup",
    "Step 2: Execute",
    "Step 3: Cleanup"
])

# Start first task
workflow[0].start("Begin workflow")
ledger6.update_task_status(workflow[0].task_id, TaskStatus.COMPLETED, result={"setup": "done"})

time.sleep(0.1)

# Check step 2 auto-resumed
step2_resumed = workflow[1].status == TaskStatus.IN_PROGRESS

# Complete step 2
ledger6.update_task_status(workflow[1].task_id, TaskStatus.COMPLETED, result={"execution": "done"})

time.sleep(0.1)

# Check step 3 auto-resumed
step3_resumed = workflow[2].status == TaskStatus.IN_PROGRESS

# Check events generated
events = ledger6.get_events()
has_completion_events = len([e for e in events if e["type"] == "task_completed"]) >= 2
has_resume_events = len([e for e in events if e["type"] == "task_auto_resumed"]) >= 2

test_passed = (
    step2_resumed and
    step3_resumed and
    has_completion_events and
    has_resume_events
)

if print_result(test_passed, "Complete workflow with auto-resume and events"):
    passed += 1
    print(f"   Step 1: {workflow[0].status}")
    print(f"   Step 2: {workflow[1].status} (auto-resumed: {step2_resumed})")
    print(f"   Step 3: {workflow[2].status} (auto-resumed: {step3_resumed})")
    print(f"   Total events: {len(events)}")
    print(f"   Completion events: {len([e for e in events if e['type'] == 'task_completed'])}")
    print(f"   Auto-resume events: {len([e for e in events if e['type'] == 'task_auto_resumed'])}")
else:
    failed += 1
    print(f"   ERROR: Step 2 resumed: {step2_resumed}, Step 3 resumed: {step3_resumed}")
    print(f"   ERROR: Events: {len(events)}")

# ==================== FINAL SUMMARY ====================
print("\n" + "="*70)
print(" TEST SUMMARY")
print("="*70)
print(f"\nTotal Tests: {passed + failed}")
print(f"Passed: {passed}/{passed + failed}")
print(f"Failed: {failed}/{passed + failed}")
print(f"Success Rate: {(passed / (passed + failed) * 100):.1f}%")

if failed == 0:
    print("\n[SUCCESS] All nested task system tests passed!")
    print("\nVerified:")
    print("  [+] Parent-child relationships")
    print("  [+] Sibling task cross-registration")
    print("  [+] Sequential chains with auto-dependencies")
    print("  [+] Deterministic auto-resume")
    print("  [+] Event generation and querying")
    print("  [+] Inter-task communication")
    print("  [+] Result passing")
    print("  [+] Tree visualization")
    print("  [+] Hierarchy queries")
    print("  [+] Dependency status queries")
    print("  [+] Complete workflow integration")
    sys.exit(0)
else:
    print(f"\n[FAILURE] {failed} test(s) failed.")
    sys.exit(1)
