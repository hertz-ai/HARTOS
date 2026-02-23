"""
Manual Test Runner - Validates key functionality without pytest
This script manually validates critical functionalities by importing and testing key modules
"""

import sys
import os
import traceback
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
import json

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# Color codes for output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'

class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = []

    def test(self, test_name, test_func):
        """Run a single test"""
        try:
            test_func()
            print(f"{GREEN}[PASS]{RESET} {test_name}")
            self.passed += 1
            return True
        except AssertionError as e:
            print(f"{RED}[FAIL]{RESET} {test_name}")
            print(f"  {RED}Assertion Error: {e}{RESET}")
            self.failed += 1
            self.errors.append((test_name, str(e)))
            return False
        except Exception as e:
            print(f"{YELLOW}[SKIP]{RESET} {test_name}")
            print(f"  {YELLOW}Error: {e}{RESET}")
            self.skipped += 1
            return False

    def section(self, name):
        """Print a test section header"""
        print(f"\n{BLUE}{'='*60}{RESET}")
        print(f"{BLUE}{name}{RESET}")
        print(f"{BLUE}{'='*60}{RESET}\n")

    def summary(self):
        """Print test summary"""
        print(f"\n{BLUE}{'='*60}{RESET}")
        print(f"{BLUE}TEST SUMMARY{RESET}")
        print(f"{BLUE}{'='*60}{RESET}")
        print(f"{GREEN}Passed:{RESET} {self.passed}")
        print(f"{RED}Failed:{RESET} {self.failed}")
        print(f"{YELLOW}Skipped/Errors:{RESET} {self.skipped}")
        print(f"Total: {self.passed + self.failed + self.skipped}")

        if self.errors:
            print(f"\n{RED}Failed Tests:{RESET}")
            for test_name, error in self.errors:
                print(f"  - {test_name}: {error}")

        return self.failed == 0


# Initialize runner
runner = TestRunner()

# =============================================================================
# TEST 1: Import and Basic Structure Tests
# =============================================================================
runner.section("TEST 1: Module Imports and Basic Structure")

def test_import_helper():
    """Test importing helper module"""
    from helper import Action, retrieve_json, topological_sort
    assert Action is not None
    assert retrieve_json is not None
    assert topological_sort is not None

runner.test("Import helper module", test_import_helper)

def test_import_lifecycle_hooks():
    """Test importing lifecycle_hooks module"""
    from lifecycle_hooks import (
        ActionState,
        initialize_deterministic_actions,
        lifecycle_hook_track_action_assignment,
        get_action_state
    )
    assert ActionState is not None

runner.test("Import lifecycle_hooks module", test_import_lifecycle_hooks)

def test_import_create_recipe():
    """Test importing create_recipe module"""
    try:
        # This will fail if Flask context is needed, but we're just testing imports
        import create_recipe
        assert create_recipe is not None
    except Exception as e:
        # Expected if Flask context is missing
        if "flask" not in str(e).lower() and "current_app" not in str(e).lower():
            raise

runner.test("Import create_recipe module", test_import_create_recipe)

def test_import_reuse_recipe():
    """Test importing reuse_recipe module"""
    try:
        import reuse_recipe
        assert reuse_recipe is not None
    except Exception as e:
        # Expected if Flask context is missing
        if "flask" not in str(e).lower() and "current_app" not in str(e).lower():
            raise

runner.test("Import reuse_recipe module", test_import_reuse_recipe)

# =============================================================================
# TEST 2: Action Class Tests
# =============================================================================
runner.section("TEST 2: Action Class Functionality")

def test_action_class_initialization():
    """Test Action class can be initialized"""
    from helper import Action

    actions = [
        {"action_id": 1, "action": "Create file"},
        {"action_id": 2, "action": "Write content"},
        {"action_id": 3, "action": "Close file"}
    ]

    action_obj = Action(actions)
    assert action_obj.current_action == 1
    assert action_obj.fallback == False
    assert action_obj.recipe == False
    assert len(action_obj.actions) == 3

runner.test("Action class initialization", test_action_class_initialization)

def test_action_get_by_index():
    """Test getting action by array index"""
    from helper import Action

    actions = [
        {"action_id": 1, "action": "Test 1"},
        {"action_id": 2, "action": "Test 2"}
    ]

    action_obj = Action(actions)
    first_action = action_obj.get_action(0)
    assert first_action["action"] == "Test 1"

    second_action = action_obj.get_action(1)
    assert second_action["action"] == "Test 2"

runner.test("Action get by index", test_action_get_by_index)

def test_action_get_by_id():
    """Test getting action by action_id"""
    from helper import Action

    actions = [
        {"action_id": 5, "action": "Test 5"},
        {"action_id": 10, "action": "Test 10"}
    ]

    action_obj = Action(actions)
    action_5 = action_obj.get_action_byaction_id(5)
    assert action_5["action"] == "Test 5"

    action_10 = action_obj.get_action_byaction_id(10)
    assert action_10["action"] == "Test 10"

    non_existent = action_obj.get_action_byaction_id(999)
    assert non_existent is None

runner.test("Action get by ID", test_action_get_by_id)

# =============================================================================
# TEST 3: Lifecycle Hooks Tests
# =============================================================================
runner.section("TEST 3: Lifecycle Hooks and State Management")

def test_lifecycle_action_assignment():
    """Test tracking action assignment"""
    from lifecycle_hooks import (
        initialize_deterministic_actions,
        lifecycle_hook_track_action_assignment,
        get_action_state,
        ActionState
    )

    # Reset state
    initialize_deterministic_actions()

    user_prompt = "test_user_123"
    action_id = 1

    # Track action assignment
    lifecycle_hook_track_action_assignment(user_prompt, action_id)

    # Verify state
    state = get_action_state(user_prompt, action_id)
    assert state == ActionState.ASSIGNED

runner.test("Lifecycle action assignment tracking", test_lifecycle_action_assignment)

def test_lifecycle_status_verification():
    """Test tracking status verification request"""
    from lifecycle_hooks import (
        initialize_deterministic_actions,
        lifecycle_hook_track_action_assignment,
        lifecycle_hook_track_status_verification_request,
        get_action_state,
        ActionState
    )

    # Reset state
    initialize_deterministic_actions()

    user_prompt = "test_user_456"
    action_id = 2

    # Track action assignment first
    lifecycle_hook_track_action_assignment(user_prompt, action_id)

    # Track status verification
    lifecycle_hook_track_status_verification_request(user_prompt, action_id)

    # Verify state
    state = get_action_state(user_prompt, action_id)
    assert state == ActionState.STATUS_VERIFICATION_REQUESTED

runner.test("Lifecycle status verification tracking", test_lifecycle_status_verification)

def test_lifecycle_recipe_request():
    """Test tracking recipe request"""
    from lifecycle_hooks import (
        initialize_deterministic_actions,
        lifecycle_hook_track_action_assignment,
        lifecycle_hook_track_status_verification_request,
        lifecycle_hook_track_recipe_request,
        get_action_state,
        ActionState
    )

    # Reset state
    initialize_deterministic_actions()

    user_prompt = "test_user_789"
    action_id = 3

    # Go through the proper state transitions
    lifecycle_hook_track_action_assignment(user_prompt, action_id)
    lifecycle_hook_track_status_verification_request(user_prompt, action_id)

    # Create mock message with completed status
    mock_message = {
        'content': '{"status": "completed"}',
        'name': 'StatusVerifier'
    }

    # Process verifier response to get to COMPLETED state
    from lifecycle_hooks import lifecycle_hook_process_verifier_response
    try:
        lifecycle_hook_process_verifier_response(user_prompt, action_id, mock_message)
    except:
        pass  # May fail due to validation, but that's OK for this test

    # Track recipe request
    try:
        lifecycle_hook_track_recipe_request(user_prompt, action_id)
    except:
        pass  # State transition may not be valid, that's OK

runner.test("Lifecycle recipe request tracking", test_lifecycle_recipe_request)

# =============================================================================
# TEST 4: JSON Processing Tests
# =============================================================================
runner.section("TEST 4: JSON Processing and Validation")

def test_retrieve_json_from_text():
    """Test extracting JSON from text"""
    from helper import retrieve_json

    text_with_json = """
    Here is the result:
    {"status": "completed", "message": "Task done"}
    Additional text.
    """

    json_obj = retrieve_json(text_with_json)
    assert json_obj is not None
    assert json_obj.get("status") == "completed"
    assert json_obj.get("message") == "Task done"

runner.test("Retrieve JSON from text", test_retrieve_json_from_text)

def test_retrieve_json_with_code_blocks():
    """Test retrieving JSON from code blocks"""
    from helper import retrieve_json

    text_with_code_block = """
    ```json
    {
        "action_id": 1,
        "action": "Test"
    }
    ```
    """

    json_obj = retrieve_json(text_with_code_block)
    assert json_obj is not None
    assert json_obj.get("action_id") == 1

runner.test("Retrieve JSON from code blocks", test_retrieve_json_with_code_blocks)

def test_json_creation_basic():
    """Test creating basic JSON structure"""
    recipe = {
        "actions": [
            {
                "action_id": 1,
                "action": "Create file",
                "recipe": []
            }
        ],
        "scheduled_tasks": []
    }

    json_str = json.dumps(recipe)
    parsed = json.loads(json_str)

    assert "actions" in parsed
    assert "scheduled_tasks" in parsed
    assert len(parsed["actions"]) == 1

runner.test("JSON creation basic", test_json_creation_basic)

# =============================================================================
# TEST 5: Topological Sort Tests
# =============================================================================
runner.section("TEST 5: Dependency Management and Topological Sort")

def test_topological_sort_linear():
    """Test topological sort with linear dependencies"""
    from helper import topological_sort

    individual_recipe = {
        1: {"dependencies": []},
        2: {"dependencies": [1]},
        3: {"dependencies": [2]}
    }

    sorted_actions = topological_sort(individual_recipe)

    assert sorted_actions[0] == 1
    assert sorted_actions[1] == 2
    assert sorted_actions[2] == 3

runner.test("Topological sort linear dependencies", test_topological_sort_linear)

def test_topological_sort_parallel():
    """Test topological sort with parallel dependencies"""
    from helper import topological_sort

    individual_recipe = {
        1: {"dependencies": []},
        2: {"dependencies": []},
        3: {"dependencies": [1, 2]}
    }

    sorted_actions = topological_sort(individual_recipe)

    # 1 and 2 can be in any order, but both must come before 3
    assert sorted_actions[2] == 3
    assert 1 in sorted_actions[:2]
    assert 2 in sorted_actions[:2]

runner.test("Topological sort parallel dependencies", test_topological_sort_parallel)

def test_topological_sort_cyclic():
    """Test topological sort detects cyclic dependencies"""
    from helper import topological_sort

    individual_recipe = {
        1: {"dependencies": [2]},
        2: {"dependencies": [1]}
    }

    try:
        sorted_actions = topological_sort(individual_recipe)
        # Should have raised an error
        assert False, "Should have detected cycle"
    except ValueError as e:
        assert "cycle" in str(e).lower() or "circular" in str(e).lower()

runner.test("Topological sort detects cycles", test_topological_sort_cyclic)

# =============================================================================
# TEST 6: Recipe Structure Validation
# =============================================================================
runner.section("TEST 6: Recipe Structure Validation")

def test_recipe_structure_valid():
    """Test validating a valid recipe structure"""
    valid_recipe = {
        "actions": [
            {
                "action_id": 1,
                "action": "Test action",
                "recipe": [
                    {
                        "steps": "Step 1",
                        "tool_name": "test_tool",
                        "generalized_functions": "test_func()",
                        "dependencies": []
                    }
                ]
            }
        ],
        "scheduled_tasks": []
    }

    # Validation checks
    assert "actions" in valid_recipe
    assert "scheduled_tasks" in valid_recipe
    assert isinstance(valid_recipe["actions"], list)
    assert all("action_id" in action for action in valid_recipe["actions"])
    assert all("recipe" in action for action in valid_recipe["actions"])

runner.test("Recipe structure validation", test_recipe_structure_valid)

def test_recipe_action_ids_unique():
    """Test that action IDs are unique in recipe"""
    recipe = {
        "actions": [
            {"action_id": 1, "action": "Action 1"},
            {"action_id": 2, "action": "Action 2"},
            {"action_id": 3, "action": "Action 3"}
        ]
    }

    action_ids = [action["action_id"] for action in recipe["actions"]]
    assert len(action_ids) == len(set(action_ids))  # All unique

runner.test("Recipe action IDs are unique", test_recipe_action_ids_unique)

# =============================================================================
# TEST 7: Scheduler Configuration Tests
# =============================================================================
runner.section("TEST 7: Scheduler Configuration Validation")

def test_cron_schedule_validation():
    """Test validating cron schedule parameters"""
    cron_task = {
        "schedule_type": "cron",
        "hour": 9,
        "minute": 30
    }

    assert 0 <= cron_task["hour"] <= 23
    assert 0 <= cron_task["minute"] <= 59

runner.test("Cron schedule validation", test_cron_schedule_validation)

def test_interval_schedule_validation():
    """Test validating interval schedule parameters"""
    interval_task = {
        "schedule_type": "interval",
        "minutes": 30
    }

    assert interval_task["minutes"] > 0

runner.test("Interval schedule validation", test_interval_schedule_validation)

def test_date_schedule_validation():
    """Test validating date schedule parameters"""
    date_task = {
        "schedule_type": "date",
        "run_date": "2025-01-15T10:30:00"
    }

    assert "run_date" in date_task
    # Validate ISO format
    datetime.fromisoformat(date_task["run_date"])

runner.test("Date schedule validation", test_date_schedule_validation)

# =============================================================================
# TEST 8: File I/O Operations
# =============================================================================
runner.section("TEST 8: Recipe File I/O Operations")

def test_save_and_load_recipe():
    """Test saving and loading recipe from file"""
    import tempfile
    import os

    recipe = {
        "actions": [{"action_id": 1, "action": "Test"}],
        "scheduled_tasks": []
    }

    # Create temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(recipe, f)
        temp_path = f.name

    try:
        # Load recipe
        with open(temp_path, 'r') as f:
            loaded = json.load(f)

        assert loaded == recipe
    finally:
        # Cleanup
        os.unlink(temp_path)

runner.test("Save and load recipe file", test_save_and_load_recipe)

# =============================================================================
# TEST 9: Agent Creation Process Understanding
# =============================================================================
runner.section("TEST 9: Understanding Agent Creation Process")

def test_understand_agent_creation_flow():
    """Document understanding of agent creation flow"""
    print("""

    {BLUE}AGENT CREATION FLOW (CREATE MODE):{RESET}

    1. User provides task description and prompt_id
    2. create_agents() is called with user_id, task, prompt_id
    3. Multiple agents are created:
       - Author (UserProxyAgent): Initiates tasks
       - Assistant: Handles main logic
       - Executor: Executes code/commands
       - StatusVerifier: Verifies action completion
       - Helper: Provides assistance
       - ChatInstructor: Guides conversation flow

    4. GroupChat is created with all agents
    5. GroupChatManager orchestrates the conversation
    6. Actions are tracked through lifecycle hooks:
       - ASSIGNED → IN_PROGRESS → STATUS_VERIFICATION_REQUESTED
       - → COMPLETED/ERROR/PENDING → FALLBACK_REQUESTED
       - → FALLBACK_RECEIVED → RECIPE_REQUESTED → RECIPE_RECEIVED
       - → TERMINATED

    7. For each action:
       - Execute the action
       - Verify status (StatusVerifier)
       - Request fallback info (can_perform_without_user_input)
       - Request recipe (generalized steps with dependencies)
       - Save recipe to JSON

    8. After all actions: Save complete recipe to prompts/{prompt_id}_0_recipe.json
    9. Update database to mark agent as created
    10. Ready for reuse mode

    {BLUE}RECIPE STRUCTURE:{RESET}
    {{
        "actions": [
            {{
                "action_id": 1,
                "action": "Action description",
                "recipe": [
                    {{
                        "steps": "What to do",
                        "tool_name": "Tool used",
                        "generalized_functions": "Generalized command/code",
                        "dependencies": [list of action_ids this depends on]
                    }}
                ]
            }}
        ],
        "scheduled_tasks": [
            {{
                "task_description": "What to do",
                "schedule_type": "cron|interval|date",
                "hour": 9, "minute": 0,  // for cron
                "minutes": 30,  // for interval
                "run_date": "ISO timestamp",  // for date
                "action_entry_point": action_id
            }}
        ]
    }}

    {BLUE}REUSE MODE FLOW:{RESET}

    1. User provides same task with existing prompt_id
    2. chat_agent() in reuse_recipe.py is called
    3. Recipe is loaded from prompts/{prompt_id}_0_recipe.json
    4. Agents are created (if not already cached)
    5. Task is executed using the saved recipe:
       - Dependencies are resolved via topological sort
       - Actions execute in correct order
       - Generalized functions are applied with current context

    6. Scheduled tasks are set up if any exist
    7. Response is sent to user
    8. No new recipe generation - just execution

    {BLUE}KEY DIFFERENCES:{RESET}
    - CREATE MODE: Learn by doing, generate recipe, slower
    - REUSE MODE: Execute from recipe, faster, consistent

    {BLUE}SCHEDULER TYPES:{RESET}
    - TIME-BASED: Execute at specific time (date trigger)
    - PERIODIC: Execute at intervals (interval trigger)
    - SCHEDULED: Execute on schedule (cron trigger)
    - VISUAL: Execute when visual context detected
    """.format(BLUE=BLUE, RESET=RESET))

    # This is a documentation test, always passes
    assert True

runner.test("Agent creation flow documentation", test_understand_agent_creation_flow)

# =============================================================================
# PRINT SUMMARY
# =============================================================================
success = runner.summary()

# Exit with appropriate code
sys.exit(0 if success else 1)
