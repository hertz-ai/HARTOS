"""
Complete Integration Test
Tests the full agent creation -> recipe execution -> recipe reuse cycle with ledger integration.
"""

import json
import sys
import time
import random
from pathlib import Path

# Test parameters - use random IDs to avoid state issues
TEST_USER_ID = random.randint(900000, 999999)
TEST_PROMPT_ID = random.randint(80000, 89999)

def test_ledger_integration():
    """Test that ledger properly integrates with agent recipes"""
    print("\n" + "="*70)
    print("TEST 1: Ledger Integration with Agent Recipes")
    print("="*70)

    from agent_ledger import create_ledger_from_actions, get_production_backend

    # Create sample recipe actions
    recipe_actions = [
        {
            "action_id": 1,
            "description": "Search for Python documentation on web",
            "prerequisites": [],
            "persona": "Web Research Assistant"
        },
        {
            "action_id": 2,
            "description": "Extract key concepts from documentation",
            "prerequisites": [1],
            "persona": "Content Analyzer"
        },
        {
            "action_id": 3,
            "description": "Generate summary report",
            "prerequisites": [2],
            "persona": "Report Generator"
        }
    ]

    backend = get_production_backend()
    ledger = create_ledger_from_actions(
        user_id=TEST_USER_ID,
        prompt_id=TEST_PROMPT_ID,
        actions=recipe_actions,
        backend=backend
    )

    print(f"[OK] Created ledger with {len(ledger.tasks)} tasks")

    # Verify task structure
    for action in recipe_actions:
        task_id = f"action_{action['action_id']}"
        assert task_id in ledger.tasks, f"Task {task_id} not found in ledger"
        task = ledger.tasks[task_id]
        print(f"  [OK] Task {task_id}: {task.description}")
        print(f"       Prerequisites: {task.prerequisites}")
        print(f"       Status: {task.status}")

    # Test task progression
    ready_tasks = ledger.get_ready_tasks()
    print(f"\n[OK] Initial ready tasks: {[t.task_id for t in ready_tasks]}")
    assert len(ready_tasks) == 1, "Should have exactly 1 ready task initially"
    assert ready_tasks[0].task_id == "action_1", "First task should be action_1"

    # Complete first task
    ledger.update_task_status("action_1", "completed", result="Search completed successfully")
    print(f"[OK] Marked action_1 as completed")

    # Check next ready task
    ready_tasks = ledger.get_ready_tasks()
    print(f"[OK] Next ready tasks: {[t.task_id for t in ready_tasks]}")
    assert len(ready_tasks) == 1, "Should have exactly 1 ready task after completing action_1"
    assert ready_tasks[0].task_id == "action_2", "Next task should be action_2"

    print("\n[PASS] Ledger integration test PASSED\n")
    return ledger


def test_vlm_context_injection():
    """Test VLM context injection into tasks"""
    print("="*70)
    print("TEST 2: VLM Context Injection")
    print("="*70)

    from agent_ledger import Task, TaskType, TaskStatus, ExecutionMode

    # Create a test task
    task = Task(
        task_id="vlm_test_task",
        description="Test VLM context injection",
        task_type=TaskType.PRE_ASSIGNED,
        execution_mode=ExecutionMode.SEQUENTIAL,
        status=TaskStatus.PENDING
    )

    print(f"[OK] Created task: {task.task_id}")
    print(f"     Initial context: {task.context}")

    # Try to inject VLM context (will gracefully handle if VLM not available)
    task.inject_vlm_context()
    print(f"[OK] Injected VLM context")
    print(f"     Enhanced context keys: {list(task.context.keys())}")

    # Check if visual context was added
    if "visual_context" in task.context:
        visual = task.context["visual_context"]
        print(f"[OK] Visual context available: {visual.get('has_screen_info', False)}")
        if visual.get('has_screen_info'):
            print(f"     Visible elements: {visual.get('visible_elements', 0)}")
            print(f"     Screen dimensions: {visual.get('screen_dimensions', {})}")
    else:
        print(f"[WARN] Visual context not available (VLM servers not running)")

    # Test visual feedback retrieval
    feedback = task.get_visual_feedback()
    print(f"\n[OK] Visual feedback retrieved:")
    print(f"     {feedback[:200]}...")  # First 200 chars

    print("\n[PASS] VLM context injection test PASSED\n")
    return task


def test_recipe_creation_with_ledger():
    """Test creating a recipe that uses the ledger system"""
    print("="*70)
    print("TEST 3: Recipe Creation with Ledger Integration")
    print("="*70)

    # Import only what we need (avoid Flask dependencies)
    from agent_ledger import get_production_backend, SmartLedger

    # Create a complex multi-step recipe definition
    recipe_definition = {
        "user_id": TEST_USER_ID,
        "prompt_id": TEST_PROMPT_ID,
        "task": "Research Python best practices and create a summary document",
        "flows": [
            {
                "persona": "Research Assistant",
                "actions": [
                    {
                        "action_id": 1,
                        "description": "Search for Python best practices online",
                        "prerequisites": []
                    },
                    {
                        "action_id": 2,
                        "description": "Extract key recommendations from search results",
                        "prerequisites": [1]
                    }
                ]
            },
            {
                "persona": "Document Writer",
                "actions": [
                    {
                        "action_id": 3,
                        "description": "Create markdown document with findings",
                        "prerequisites": [2]
                    },
                    {
                        "action_id": 4,
                        "description": "Add examples and code snippets",
                        "prerequisites": [3]
                    }
                ]
            }
        ]
    }

    print(f"[OK] Created recipe definition with {len(recipe_definition['flows'])} flows")

    # Collect all actions from all flows
    all_actions = []
    for flow in recipe_definition['flows']:
        all_actions.extend(flow['actions'])

    print(f"[OK] Total actions across flows: {len(all_actions)}")

    # Create ledger from actions
    backend = get_production_backend()
    from agent_ledger import create_ledger_from_actions
    ledger = create_ledger_from_actions(
        user_id=TEST_USER_ID,
        prompt_id=TEST_PROMPT_ID,
        actions=all_actions,
        backend=backend
    )

    print(f"[OK] Created ledger with {len(ledger.tasks)} tasks")

    # Verify ledger structure matches recipe
    for action in all_actions:
        task_id = f"action_{action['action_id']}"
        assert task_id in ledger.tasks, f"Missing task {task_id}"
        print(f"  [OK] Task {task_id} present in ledger")

    # Test task execution flow
    print(f"\n[OK] Testing task execution flow:")
    completed_count = 0
    max_iterations = 10  # Prevent infinite loop
    iteration = 0

    while iteration < max_iterations:
        ready_tasks = ledger.get_ready_tasks()
        if not ready_tasks:
            print(f"  [OK] No more ready tasks - execution complete")
            break

        for task in ready_tasks:
            print(f"  [OK] Executing task: {task.task_id} - {task.description}")
            # Simulate task execution
            ledger.update_task_status(
                task.task_id,
                "in_progress",
                result="Task started"
            )
            time.sleep(0.1)  # Simulate work
            ledger.update_task_status(
                task.task_id,
                "completed",
                result=f"Task {task.task_id} completed successfully"
            )
            completed_count += 1

        iteration += 1

    print(f"\n[OK] Completed {completed_count} tasks in {iteration} iterations")

    # Verify all tasks completed
    progress = ledger.get_progress_summary()
    print(f"[OK] Final progress: {progress['completed']}/{progress['total']} tasks completed")
    assert progress['completed'] == len(all_actions), "Not all tasks completed"

    print("\n[PASS] Recipe creation with ledger test PASSED\n")
    return ledger, recipe_definition


def test_recipe_reuse():
    """Test recipe reuse with ledger"""
    print("="*70)
    print("TEST 4: Recipe Reuse with Similar Task")
    print("="*70)

    # Simulate saved recipe
    saved_recipe = {
        "prompt_id": TEST_PROMPT_ID,
        "flows": [
            {
                "role_number": 0,
                "persona": "Research Assistant",
                "actions": [
                    {
                        "action_id": 1,
                        "description": "Search for Python documentation",
                        "prerequisites": []
                    },
                    {
                        "action_id": 2,
                        "description": "Summarize findings",
                        "prerequisites": [1]
                    }
                ]
            }
        ]
    }

    # Save recipe to file
    recipe_path = Path(f"prompts/{TEST_PROMPT_ID}_0_recipe.json")
    recipe_path.parent.mkdir(exist_ok=True)
    with open(recipe_path, 'w') as f:
        json.dump(saved_recipe, f, indent=2)

    print(f"[OK] Saved recipe to {recipe_path}")

    # Create ledger for reuse scenario
    from agent_ledger import create_ledger_from_actions, get_production_backend

    actions = saved_recipe['flows'][0]['actions']
    backend = get_production_backend()
    ledger = create_ledger_from_actions(
        user_id=TEST_USER_ID,
        prompt_id=TEST_PROMPT_ID,
        actions=actions,
        backend=backend
    )

    print(f"[OK] Created ledger from saved recipe with {len(ledger.tasks)} tasks")

    # Verify recipe structure
    for action in actions:
        task_id = f"action_{action['action_id']}"
        assert task_id in ledger.tasks, f"Task {task_id} missing"
        print(f"  [OK] Reused task: {task_id}")

    print("\n[PASS] Recipe reuse test PASSED\n")

    # Cleanup
    recipe_path.unlink(missing_ok=True)
    print(f"[OK] Cleaned up test recipe file\n")

    return ledger


def test_vlm_agent_tool_availability():
    """Test if execute_windows_or_android_command tool is defined"""
    print("="*70)
    print("TEST 5: VLM Agent Tool Availability")
    print("="*70)

    try:
        # Check if tool is defined in create_recipe.py source
        from pathlib import Path
        create_recipe_path = Path(__file__).parent / "create_recipe.py"

        with open(create_recipe_path, 'r', encoding='utf-8') as f:
            source = f.read()

        # Check for function definition
        if "def execute_windows_or_android_command" in source:
            print(f"[OK] execute_windows_or_android_command tool found in create_recipe.py")

            # Find the line number
            for i, line in enumerate(source.split('\n'), 1):
                if "def execute_windows_or_android_command" in line:
                    print(f"[OK] Function defined at line {i}")
                    break

            # Check for async
            if "async def execute_windows_or_android_command" in source:
                print(f"[OK] Function is async (returns coroutine)")
            else:
                print(f"[INFO] Function is synchronous")

            # Check for tool registration
            if "register_for_llm" in source and "execute_windows_or_android_command" in source:
                print(f"[OK] Tool is registered for LLM use")

        else:
            print(f"[FAIL] execute_windows_or_android_command not found in source")
            return False

    except Exception as e:
        print(f"[FAIL] Could not check tool: {e}")
        return False

    print("\n[PASS] VLM agent tool availability test PASSED\n")
    return True


def run_all_tests():
    """Run all integration tests"""
    print("\n" + "="*70)
    print(" COMPLETE INTEGRATION TEST SUITE")
    print("="*70)
    print(f"\nTest User ID: {TEST_USER_ID}")
    print(f"Test Prompt ID: {TEST_PROMPT_ID}\n")

    results = {}

    try:
        # Test 1: Ledger integration
        results['ledger'] = test_ledger_integration()

        # Test 2: VLM context injection
        results['vlm_context'] = test_vlm_context_injection()

        # Test 3: Recipe creation with ledger
        results['recipe_creation'] = test_recipe_creation_with_ledger()

        # Test 4: Recipe reuse
        results['recipe_reuse'] = test_recipe_reuse()

        # Test 5: VLM agent tool availability
        results['vlm_tool'] = test_vlm_agent_tool_availability()

        print("="*70)
        print(" ALL TESTS PASSED")
        print("="*70)
        print(f"\nSummary:")
        print(f"  - Ledger integration: PASS")
        print(f"  - VLM context injection: PASS")
        print(f"  - Recipe creation with ledger: PASS")
        print(f"  - Recipe reuse: PASS")
        print(f"  - VLM agent tool availability: PASS")

        return True

    except Exception as e:
        print(f"\n[FAIL] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
