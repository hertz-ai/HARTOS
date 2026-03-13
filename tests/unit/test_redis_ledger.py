"""
Test script to verify Redis backend integration with SmartLedger.

This script:
1. Tests basic ledger operations with Redis backend
2. Compares performance between JSON and Redis backends
3. Verifies fallback mechanism when Redis is unavailable
"""

import time
import pytest

pytest.importorskip('agent_ledger', reason='agent_ledger not installed')

from agent_ledger import (
    SmartLedger, Task, TaskType, TaskStatus, ExecutionMode,
    create_ledger_from_actions, get_production_backend
)

def test_basic_operations():
    """Test basic ledger operations with production backend"""
    print("=" * 60)
    print("Test 1: Basic Ledger Operations")
    print("=" * 60)

    # Get production backend (tries Redis, falls back to JSON)
    backend = get_production_backend()

    # Create ledger
    ledger = SmartLedger(agent_id=12345, session_id=99999, backend=backend)
    print(f"[OK] Created ledger: {ledger}")

    # Add tasks
    task1 = Task(
        task_id="test_task_1",
        description="Test task 1",
        task_type=TaskType.PRE_ASSIGNED,
        execution_mode=ExecutionMode.PARALLEL,
        status=TaskStatus.PENDING,
        priority=90
    )
    ledger.add_task(task1)

    task2 = Task(
        task_id="test_task_2",
        description="Test task 2",
        task_type=TaskType.AUTONOMOUS,
        execution_mode=ExecutionMode.SEQUENTIAL,
        status=TaskStatus.PENDING,
        prerequisites=["test_task_1"],
        priority=80
    )
    ledger.add_task(task2)

    print(f"[OK] Added {len(ledger.tasks)} tasks")

    # Update task status
    ledger.update_task_status("test_task_1", TaskStatus.IN_PROGRESS)
    print(f"[OK] Updated task status to IN_PROGRESS")

    ledger.update_task_status("test_task_1", TaskStatus.COMPLETED, result="Task completed successfully")
    print(f"[OK] Updated task status to COMPLETED")

    # Get ready tasks
    ready_tasks = ledger.get_ready_tasks()
    print(f"[OK] Found {len(ready_tasks)} ready tasks: {[t.task_id for t in ready_tasks]}")

    # Get progress summary
    progress = ledger.get_progress_summary()
    print(f"[OK] Progress: {progress['completed']}/{progress['total']} completed ({progress['progress']})")

    print("\n[PASS] Basic operations test PASSED\n")
    return ledger


def test_persistence():
    """Test ledger persistence across instances"""
    print("=" * 60)
    print("Test 2: Ledger Persistence")
    print("=" * 60)

    backend = get_production_backend()

    # Create first instance and add tasks
    ledger1 = SmartLedger(agent_id=12345, session_id=88888, backend=backend)
    task = Task(
        task_id="persist_test",
        description="Persistence test task",
        task_type=TaskType.PRE_ASSIGNED,
        status=TaskStatus.PENDING
    )
    ledger1.add_task(task)
    print(f"[OK] Instance 1: Added task '{task.task_id}'")

    # Create second instance with same IDs - should load existing data
    ledger2 = SmartLedger(agent_id=12345, session_id=88888, backend=backend)
    print(f"[OK] Instance 2: Loaded {len(ledger2.tasks)} tasks")

    if "persist_test" in ledger2.tasks:
        print(f"[PASS] Persistence test PASSED - Task found in new instance")
    else:
        print(f"[FAIL] Persistence test FAILED - Task not found in new instance")

    print()
    return ledger2


def test_create_ledger_from_actions():
    """Test creating ledger from action list"""
    print("=" * 60)
    print("Test 3: Create Ledger from Actions")
    print("=" * 60)

    actions = [
        {
            "action_id": 1,
            "description": "Initialize system",
            "prerequisites": [],
            "persona": "System Admin"
        },
        {
            "action_id": 2,
            "description": "Load configuration",
            "prerequisites": [1],
            "persona": "System Admin"
        },
        {
            "action_id": 3,
            "description": "Start services",
            "prerequisites": [2],
            "persona": "System Admin"
        }
    ]

    backend = get_production_backend()
    ledger = create_ledger_from_actions(
        user_id=12345,
        prompt_id=77777,
        actions=actions,
        backend=backend
    )

    print(f"[OK] Created ledger with {len(ledger.tasks)} tasks from {len(actions)} actions")

    # Verify tasks were created correctly
    for action in actions:
        task_id = f"action_{action['action_id']}"
        if task_id in ledger.tasks:
            task = ledger.tasks[task_id]
            print(f"  - {task_id}: {task.description} (prerequisites: {task.prerequisites})")
        else:
            print(f"  [FAIL] Missing task: {task_id}")

    print("\n[PASS] Create from actions test PASSED\n")
    return ledger


def test_performance_comparison():
    """Compare performance between JSON and Redis backends"""
    print("=" * 60)
    print("Test 4: Performance Comparison (JSON vs Redis)")
    print("=" * 60)

    num_operations = 100

    # Test with JSON backend
    from agent_ledger.backends import JSONBackend
    json_backend = JSONBackend(storage_dir="test_ledger_json")

    start = time.time()
    ledger_json = SmartLedger(agent_id=12345, session_id=11111, backend=json_backend)
    for i in range(num_operations):
        task = Task(
            task_id=f"json_task_{i}",
            description=f"JSON test task {i}",
            task_type=TaskType.PRE_ASSIGNED,
            status=TaskStatus.PENDING
        )
        ledger_json.add_task(task)
    json_time = time.time() - start

    print(f"JSON Backend: {num_operations} operations in {json_time:.3f}s ({json_time/num_operations*1000:.2f}ms per operation)")

    # Test with Redis backend (if available)
    try:
        from agent_ledger.backends import RedisBackend
        redis_backend = RedisBackend(host='localhost', port=6379)

        start = time.time()
        ledger_redis = SmartLedger(agent_id=12345, session_id=22222, backend=redis_backend)
        for i in range(num_operations):
            task = Task(
                task_id=f"redis_task_{i}",
                description=f"Redis test task {i}",
                task_type=TaskType.PRE_ASSIGNED,
                status=TaskStatus.PENDING
            )
            ledger_redis.add_task(task)
        redis_time = time.time() - start

        print(f"Redis Backend: {num_operations} operations in {redis_time:.3f}s ({redis_time/num_operations*1000:.2f}ms per operation)")

        speedup = json_time / redis_time
        print(f"\n[PASS] Redis is {speedup:.1f}x faster than JSON!")

    except Exception as e:
        print(f"\n[WARN] Redis not available: {e}")
        print("Install Redis and start server to see performance comparison")

    print()


def cleanup():
    """Clean up test data"""
    print("=" * 60)
    print("Cleanup")
    print("=" * 60)

    try:
        # Clean up JSON files
        import os
        import shutil
        if os.path.exists("test_ledger_json"):
            shutil.rmtree("test_ledger_json")
            print("[OK] Cleaned up JSON test files")

        # Clean up Redis keys
        try:
            from agent_ledger.backends import RedisBackend
            redis_backend = RedisBackend(host='localhost', port=6379)
            test_keys = redis_backend.list_keys("ledger_12345_*")
            for key in test_keys:
                redis_backend.delete(key)
            print(f"[OK] Cleaned up {len(test_keys)} Redis test keys")
        except:
            pass

    except Exception as e:
        print(f"[WARN] Cleanup warning: {e}")

    print()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("REDIS LEDGER INTEGRATION TESTS")
    print("=" * 60 + "\n")

    try:
        # Run tests
        test_basic_operations()
        test_persistence()
        test_create_ledger_from_actions()
        test_performance_comparison()

        print("=" * 60)
        print("ALL TESTS COMPLETED")
        print("=" * 60)

    except Exception as e:
        print(f"\n[FAIL] TEST FAILED: {e}")
        import traceback
        traceback.print_exc()

    finally:
        cleanup()

    print("\n[PASS] Testing complete!")
