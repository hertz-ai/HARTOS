#!/usr/bin/env python3
"""
Comprehensive Test Suite for Nested Task Management System

Tests:
1. Parent-child task relationships
2. Sibling task creation
3. Sequential task chains with auto-dependencies
4. Automatic dependency resolution
5. Auto-resume on dependency completion
6. Inter-task communication
7. Result passing between tasks
8. Task tree visualization
9. Hierarchy queries
10. Complex nested scenarios
"""

import sys
import time
from datetime import datetime
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
print(" NESTED TASK MANAGEMENT TEST SUITE")
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
ledger.task_order.append("parent_1")

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

# ==================== TEST 2: Sibling Tasks ====================
print_test_header(2, "Sibling Task Creation")

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
    print(f"   Parent children: {parent.child_task_ids}")
else:
    failed += 1

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
    sequential[0].task_id in sequential[1].blocked_by
)

if print_result(test_passed, "Sequential chain created with automatic blocking"):
    passed += 1
    for i, task in enumerate(sequential, 1):
        print(f"   Step {i}: {task.task_id} - Status: {task.status}, Blocked by: {task.blocked_by}")
else:
    failed += 1

# ==================== TEST 4: Auto-Resume on Dependency Completion ====================
print_test_header(4, "Auto-Resume on Dependency Completion")

# Start and complete first task
sequential[0].start("Begin step 1")
ledger2.update_task_status(
    sequential[0].task_id,
    TaskStatus.COMPLETED,
    result={"data": "initialized"}
)

# Check if second task auto-resumed
time.sleep(0.1)  # Small delay for processing

test_passed = (
    sequential[1].status == TaskStatus.IN_PROGRESS and
    not sequential[1].is_blocked() and
    sequential[0].task_id not in sequential[1].blocked_by and
    len(sequential[1].received_messages) > 0
)

if print_result(test_passed, "Task 2 auto-resumed when Task 1 completed"):
    passed += 1
    print(f"   Task 1: {sequential[0].status}")
    print(f"   Task 2: {sequential[1].status} (was BLOCKED)")
    print(f"   Messages received: {len(sequential[1].received_messages)}")
else:
    failed += 1

# ==================== TEST 5: Inter-Task Communication ====================
print_test_header(5, "Inter-Task Communication")

# Check messages delivered
messages = sequential[1].get_messages_from_prerequisites()
result_messages = sequential[1].get_messages_from_prerequisites(message_type="result")

test_passed = (
    len(messages) >= 1 and
    len(result_messages) >= 1 and
    result_messages[0].get("from_task_id") == sequential[0].task_id and
    "data" in result_messages[0]
)

if print_result(test_passed, "Messages delivered from prerequisite to dependent task"):
    passed += 1
    print(f"   Total messages: {len(messages)}")
    print(f"   Result messages: {len(result_messages)}")
    if result_messages:
        print(f"   Result data: {result_messages[0].get('data')}")
else:
    failed += 1

# ==================== TEST 6: Result Passing ====================
print_test_header(6, "Result Passing Between Tasks")

prerequisite_results = sequential[1].get_prerequisite_results()

test_passed = (
    sequential[0].task_id in prerequisite_results and
    prerequisite_results[sequential[0].task_id] == {"data": "initialized"}
)

if print_result(test_passed, "Results extracted from prerequisite tasks"):
    passed += 1
    print(f"   Results: {prerequisite_results}")
else:
    failed += 1

# ==================== TEST 7: Chain Auto-Resume ====================
print_test_header(7, "Full Sequential Chain Auto-Resume")

# Complete task 2 using update_task_status to trigger auto-resume chain
ledger2.update_task_status(
    sequential[1].task_id,
    TaskStatus.COMPLETED,
    result={"processed": 100}
)

time.sleep(0.1)

# Complete task 3 using update_task_status to trigger auto-resume chain
ledger2.update_task_status(
    sequential[2].task_id,
    TaskStatus.COMPLETED,
    result={"report": "generated"}
)

time.sleep(0.1)

# Check full chain progressed
test_passed = (
    sequential[0].status == TaskStatus.COMPLETED and
    sequential[1].status == TaskStatus.COMPLETED and
    sequential[2].status == TaskStatus.COMPLETED and
    sequential[3].status == TaskStatus.IN_PROGRESS and  # Auto-resumed
    not sequential[3].is_blocked()
)

if print_result(test_passed, "Entire chain auto-resumed sequentially"):
    passed += 1
    for i, task in enumerate(sequential, 1):
        print(f"   Step {i}: {task.status}")
else:
    failed += 1

# ==================== TEST 8: Task Tree Visualization ====================
print_test_header(8, "Task Tree Visualization")

# Create complex hierarchy
ledger3 = SmartLedger(agent_id=999, session_id=8003)
root = Task(
    task_id="root",
    description="Root task",
    task_type=TaskType.PRE_ASSIGNED,
    status=TaskStatus.IN_PROGRESS
)
ledger3.tasks["root"] = root
ledger3.task_order.append("root")

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

# ==================== TEST 9: Task Tree Structure ====================
print_test_header(9, "Task Tree Data Structure")

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
    print(f"   Child A children: {len(tree_structure['children'][0]['children'])}")
else:
    failed += 1

# ==================== TEST 10: Hierarchy Queries ====================
print_test_header(10, "Hierarchy Query Methods")

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

# ==================== TEST 11: All Prerequisites Completed Check ====================
print_test_header(11, "Prerequisites Completion Check")

ledger4 = SmartLedger(agent_id=999, session_id=8004)

# Create tasks
task_a = Task(task_id="task_a", description="Task A", task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.PENDING)
task_b = Task(task_id="task_b", description="Task B", task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.PENDING)
task_c = Task(
    task_id="task_c",
    description="Task C (depends on A and B)",
    task_type=TaskType.PRE_ASSIGNED,
    status=TaskStatus.BLOCKED,
    prerequisites=["task_a", "task_b"]
)

ledger4.tasks["task_a"] = task_a
ledger4.tasks["task_b"] = task_b
ledger4.tasks["task_c"] = task_c
ledger4.task_order.extend(["task_a", "task_b", "task_c"])

# Set up dependencies
task_c.add_blocking_task("task_a")
task_c.add_blocking_task("task_b")
task_a.add_dependent_task("task_c")
task_b.add_dependent_task("task_c")

# Initially not all completed
check1 = task_c.has_all_prerequisites_completed(ledger4)

# Complete task A
task_a.status = TaskStatus.COMPLETED
check2 = task_c.has_all_prerequisites_completed(ledger4)

# Complete task B
task_b.status = TaskStatus.COMPLETED
check3 = task_c.has_all_prerequisites_completed(ledger4)

test_passed = (
    check1 == False and
    check2 == False and  # Still waiting on B
    check3 == True  # Both complete
)

if print_result(test_passed, "Prerequisites completion check works correctly"):
    passed += 1
    print(f"   Before: {check1}")
    print(f"   After A: {check2}")
    print(f"   After B: {check3}")
else:
    failed += 1

# ==================== TEST 12: Children Completion Check ====================
print_test_header(12, "Children Completion Check")

# Use previous hierarchy
check1 = root.has_all_children_completed(ledger3)

# Complete all children
for child_id in root.child_task_ids:
    child = ledger3.get_task(child_id)
    if child:
        child.status = TaskStatus.COMPLETED
        # Complete grandchildren too
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

# ==================== FINAL SUMMARY ====================
print("\n" + "="*70)
print(" TEST SUMMARY")
print("="*70)
print(f"\nPassed: {passed}/12")
print(f"Failed: {failed}/12")

if failed == 0:
    print("\n[SUCCESS] All nested task tests passed!")
    sys.exit(0)
else:
    print(f"\n[FAILURE] {failed} test(s) failed.")
    sys.exit(1)
