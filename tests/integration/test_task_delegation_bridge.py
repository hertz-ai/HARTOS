"""
Test suite for Task Delegation Bridge

Demonstrates proper integration between A2A delegation and task_ledger
with state management and auto-resume capabilities.
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import uuid
import json
import pytest
pytest.importorskip('agent_ledger', reason='agent_ledger not installed')

from integrations.internal_comm import skill_registry, a2a_context, register_agent_with_skills
from agent_ledger import SmartLedger, TaskType, TaskStatus
from integrations.internal_comm.task_delegation_bridge import TaskDelegationBridge


def test_delegation_with_task_blocking():
    """Test that parent task is blocked during delegation"""
    print("\n" + "=" * 80)
    print("TEST 1: Task Blocking During Delegation")
    print("=" * 80)

    # Setup agents
    register_agent_with_skills('agent_a', [
        {'name': 'general_tasks', 'description': 'General task handling', 'proficiency': 0.8}
    ])
    register_agent_with_skills('agent_b', [
        {'name': 'data_analysis', 'description': 'Data analysis specialist', 'proficiency': 0.95}
    ])

    # Create ledger and bridge
    ledger = SmartLedger(agent_id="test_user", session_id="test_delegation")
    bridge = TaskDelegationBridge(a2a_context, ledger)

    # Create parent task
    from agent_ledger import Task
    parent_task_id = f"task_{uuid.uuid4().hex[:12]}"
    parent_task = Task(
        task_id=parent_task_id,
        description="Process customer data",
        task_type=TaskType.PRE_ASSIGNED
    )
    ledger.add_task(parent_task)
    print(f"\n1. Created parent task: {parent_task.task_id}")
    print(f"   Status: {parent_task.status.value}")

    # Start parent task
    ledger.update_task_status(parent_task.task_id, TaskStatus.IN_PROGRESS)
    print(f"\n2. Started parent task")
    print(f"   Status: {ledger.get_task(parent_task.task_id).status.value}")

    # Delegate subtask
    delegation_id = bridge.delegate_task_with_tracking(
        parent_task_id=parent_task.task_id,
        from_agent='agent_a',
        task_description="Analyze customer purchase patterns",
        required_skills=['data_analysis'],
        context={'data_source': 'customers.csv'}
    )

    print(f"\n3. Delegated subtask: {delegation_id}")

    # Check parent task is BLOCKED
    parent_after_delegation = ledger.get_task(parent_task.task_id)
    assert parent_after_delegation.status == TaskStatus.BLOCKED
    print(f"   Parent task status: {parent_after_delegation.status.value} [OK]")

    # Check child task was created
    status = bridge.get_delegation_status(delegation_id)
    child_task_id = status['child_task']['task_id']
    child_task = ledger.get_task(child_task_id)
    print(f"\n4. Child task created: {child_task_id}")
    print(f"   Delegated to: {status['delegation']['to_agent']}")
    print(f"   Child status: {child_task.status.value}")

    print("\n[OK] Parent task properly blocked while delegation in progress")

    return delegation_id, parent_task.task_id, child_task_id, bridge


def test_auto_resume_after_delegation(delegation_id, parent_task_id, child_task_id, bridge):
    """Test that parent task auto-resumes when delegation completes"""
    print("\n" + "=" * 80)
    print("TEST 2: Auto-Resume After Delegation")
    print("=" * 80)

    ledger = bridge.ledger

    # Verify parent is still blocked
    parent_task = ledger.get_task(parent_task_id)
    print(f"\n1. Before completion:")
    print(f"   Parent status: {parent_task.status.value}")
    assert parent_task.status == TaskStatus.BLOCKED

    # Complete the delegated task
    result = {
        'patterns': ['repeat_purchases', 'seasonal_trends'],
        'recommendations': ['target_marketing', 'loyalty_program']
    }

    bridge.complete_delegation_with_tracking(
        delegation_id=delegation_id,
        result=result,
        success=True
    )
    print(f"\n2. Completed delegation: {delegation_id}")

    # Check child task is completed
    child_task = ledger.get_task(child_task_id)
    assert child_task.status == TaskStatus.COMPLETED
    print(f"   Child task status: {child_task.status.value} [OK]")

    # Check parent task auto-resumed
    parent_task = ledger.get_task(parent_task_id)
    print(f"\n3. After completion:")
    print(f"   Parent status: {parent_task.status.value}")

    # Parent should have auto-resumed
    assert parent_task.status == TaskStatus.IN_PROGRESS
    print(f"\n[OK] Parent task auto-resumed to IN_PROGRESS")

    return parent_task_id


def test_nested_delegations():
    """Test nested delegations (delegation within delegation)"""
    print("\n" + "=" * 80)
    print("TEST 3: Nested Delegations")
    print("=" * 80)

    # Setup three-level agent hierarchy
    register_agent_with_skills('coordinator', [
        {'name': 'coordination', 'description': 'Task coordination', 'proficiency': 0.9}
    ])
    register_agent_with_skills('analyst', [
        {'name': 'data_analysis', 'description': 'Data analysis', 'proficiency': 0.9},
        {'name': 'reporting', 'description': 'Report generation', 'proficiency': 0.8}
    ])
    register_agent_with_skills('specialist', [
        {'name': 'ml_modeling', 'description': 'Machine learning', 'proficiency': 0.95}
    ])

    ledger = SmartLedger(agent_id="test_user", session_id="test_nested")
    bridge = TaskDelegationBridge(a2a_context, ledger)

    # Level 1: Coordinator creates main task
    from agent_ledger import Task
    main_task_id = f"task_{uuid.uuid4().hex[:12]}"
    main_task = Task(
        task_id=main_task_id,
        description="Generate quarterly business report",
        task_type=TaskType.PRE_ASSIGNED
    )
    ledger.add_task(main_task)
    ledger.update_task_status(main_task.task_id, TaskStatus.IN_PROGRESS)
    print(f"\n1. Main task (coordinator): {main_task.task_id}")
    print(f"   Status: {main_task.status.value}")

    # Level 2: Coordinator delegates to analyst
    delegation_1 = bridge.delegate_task_with_tracking(
        parent_task_id=main_task.task_id,
        from_agent='coordinator',
        task_description="Analyze quarterly sales data",
        required_skills=['data_analysis'],
        context={'quarter': 'Q4_2024'}
    )
    print(f"\n2. Delegation 1 (coordinator → analyst): {delegation_1}")

    status_1 = bridge.get_delegation_status(delegation_1)
    analysis_task_id = status_1['child_task']['task_id']
    analysis_task = ledger.get_task(analysis_task_id)

    # Analyst starts the analysis task
    ledger.update_task_status(analysis_task_id, TaskStatus.IN_PROGRESS)
    print(f"   Analysis task: {analysis_task_id}")
    print(f"   Status: {ledger.get_task(analysis_task_id).status.value}")

    # Level 3: Analyst delegates ML modeling to specialist
    delegation_2 = bridge.delegate_task_with_tracking(
        parent_task_id=analysis_task_id,
        from_agent='analyst',
        task_description="Build predictive model for sales forecast",
        required_skills=['ml_modeling'],
        context={'algorithm': 'random_forest'}
    )
    print(f"\n3. Delegation 2 (analyst → specialist): {delegation_2}")

    status_2 = bridge.get_delegation_status(delegation_2)
    ml_task_id = status_2['child_task']['task_id']

    # Check task hierarchy
    main_task = ledger.get_task(main_task.task_id)
    analysis_task = ledger.get_task(analysis_task_id)
    ml_task = ledger.get_task(ml_task_id)

    print(f"\n4. Task hierarchy:")
    print(f"   Main task: {main_task.status.value} (BLOCKED - waiting for analysis)")
    print(f"   Analysis task: {analysis_task.status.value} (BLOCKED - waiting for ML)")
    print(f"   ML task: {ml_task.status.value}")

    assert main_task.status == TaskStatus.BLOCKED
    assert analysis_task.status == TaskStatus.BLOCKED
    assert ml_task.status == TaskStatus.PENDING

    # Specialist completes ML task
    print(f"\n5. Specialist completes ML modeling...")
    bridge.complete_delegation_with_tracking(
        delegation_id=delegation_2,
        result={'model_accuracy': 0.92, 'forecast': 'growth_15_percent'},
        success=True
    )

    # Analysis task should auto-resume
    analysis_task = ledger.get_task(analysis_task_id)
    print(f"   Analysis task: {analysis_task.status.value} (auto-resumed)")
    assert analysis_task.status == TaskStatus.IN_PROGRESS

    # Analyst completes analysis
    print(f"\n6. Analyst completes analysis...")
    bridge.complete_delegation_with_tracking(
        delegation_id=delegation_1,
        result={'sales_analysis': 'positive_trend', 'ml_forecast': 'growth_15_percent'},
        success=True
    )

    # Main task should auto-resume
    main_task = ledger.get_task(main_task.task_id)
    print(f"   Main task: {main_task.status.value} (auto-resumed)")
    assert main_task.status == TaskStatus.IN_PROGRESS

    print(f"\n[OK] Nested delegations with cascade auto-resume working correctly")


def test_delegation_status_tracking():
    """Test comprehensive delegation status tracking"""
    print("\n" + "=" * 80)
    print("TEST 4: Delegation Status Tracking")
    print("=" * 80)

    register_agent_with_skills('requester', [
        {'name': 'general', 'description': 'General tasks', 'proficiency': 0.8}
    ])
    register_agent_with_skills('executor', [
        {'name': 'code_execution', 'description': 'Code execution', 'proficiency': 0.95}
    ])

    ledger = SmartLedger(agent_id="test_user", session_id="test_status")
    bridge = TaskDelegationBridge(a2a_context, ledger)

    # Create task and delegate
    from agent_ledger import Task
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    task = Task(
        task_id=task_id,
        description="Run performance tests",
        task_type=TaskType.PRE_ASSIGNED
    )
    ledger.add_task(task)
    ledger.update_task_status(task.task_id, TaskStatus.IN_PROGRESS)

    delegation_id = bridge.delegate_task_with_tracking(
        parent_task_id=task.task_id,
        from_agent='requester',
        task_description="Execute load test suite",
        required_skills=['code_execution'],
        context={'test_type': 'load', 'duration': '5min'}
    )

    # Get detailed status
    status = bridge.get_delegation_status(delegation_id)

    print(f"\n1. Delegation Status:")
    print(json.dumps(status, indent=2, default=str))

    # Verify status structure
    assert 'delegation_id' in status
    assert 'parent_task' in status
    assert 'child_task' in status
    assert 'delegation' in status

    assert status['parent_task']['status'] == 'blocked'
    assert status['child_task']['status'] == 'pending'

    print(f"\n[OK] Delegation status tracking complete and accurate")

    # List active delegations
    active = bridge.list_active_delegations()
    print(f"\n2. Active delegations: {len(active)}")
    for i, del_status in enumerate(active, 1):
        print(f"   {i}. {del_status['delegation_id'][:8]}... - "
              f"Child: {del_status['child_task']['status']}")

    print(f"\n[OK] Active delegation listing working")


def run_all_tests():
    """Run all task delegation bridge tests"""
    print("\n" + "=" * 80)
    print("TASK DELEGATION BRIDGE - Integration Test Suite")
    print("=" * 80)
    print("\nDemonstrating proper integration between A2A and task_ledger")

    try:
        # Test 1: Basic delegation with blocking
        delegation_id, parent_task_id, child_task_id, bridge = test_delegation_with_task_blocking()

        # Test 2: Auto-resume
        test_auto_resume_after_delegation(delegation_id, parent_task_id, child_task_id, bridge)

        # Test 3: Nested delegations
        test_nested_delegations()

        # Test 4: Status tracking
        test_delegation_status_tracking()

        print("\n" + "=" * 80)
        print("ALL TESTS PASSED [OK]")
        print("=" * 80)
        print("\nTask Delegation Bridge Summary:")
        print("  [OK] Parent task blocks during delegation")
        print("  [OK] Child task created with proper parent-child relationship")
        print("  [OK] Parent task auto-resumes when delegation completes")
        print("  [OK] Nested delegations with cascade resume")
        print("  [OK] Comprehensive delegation status tracking")
        print("\nTask delegation is fully integrated with task_ledger!")

    except AssertionError as e:
        print(f"\n[FAIL] TEST FAILED: {e}")
        raise
    except Exception as e:
        print(f"\n[FAIL] ERROR: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    run_all_tests()
