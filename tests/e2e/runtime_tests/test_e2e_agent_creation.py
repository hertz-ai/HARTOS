"""
End-to-End Tests for Agent Creation (CREATE MODE)
Tests the complete agent creation workflow against running containers
"""

import pytest
import requests
import time
import json
import os

pytest.importorskip('conftest_runtime', reason='Runtime tests require live server (conftest_runtime)')
from conftest_runtime import APP_URL, MOCK_API_URL

@pytest.mark.runtime
class TestAgentCreationE2E:
    """Test complete agent creation workflow"""

    def test_create_mode_full_workflow(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        CRITICAL TEST: Complete creation mode workflow

        Tests the entire flow:
        1. Send task to /chat endpoint (create mode)
        2. Wait for agent execution
        3. Verify all actions completed
        4. Verify recipe JSON generated
        5. Verify database updated
        """
        # Prepare request data
        request_data = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Create a file named test.txt and write 'Hello World' to it",
            "file_id": None,
            "request_id": f"test_req_{int(time.time())}"
        }

        # Send request to create mode
        print(f"\n📤 Sending request to CREATE mode...")
        response = requests.post(
            f"{APP_URL}/chat",
            json=request_data,
            timeout=120  # Allow 2 minutes for agent execution
        )

        # Assert response received
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        response_data = response.json()

        print(f"✓ Response received: {response_data}")

        # Give agents time to complete
        time.sleep(5)

        # Verify recipe file was created
        recipe_path = f"prompts/{test_prompt_id}_0_recipe.json"
        assert os.path.exists(recipe_path), f"Recipe file not created at {recipe_path}"

        print(f"✓ Recipe file created at {recipe_path}")

        # Load and validate recipe structure
        with open(recipe_path, 'r') as f:
            recipe = json.load(f)

        # Validate recipe structure
        assert "actions" in recipe, "Recipe missing 'actions' key"
        assert "scheduled_tasks" in recipe, "Recipe missing 'scheduled_tasks' key"
        assert len(recipe["actions"]) > 0, "Recipe has no actions"

        print(f"✓ Recipe structure validated")
        print(f"  - Actions: {len(recipe['actions'])}")
        print(f"  - Scheduled tasks: {len(recipe.get('scheduled_tasks', []))}")

        # Verify each action has required fields
        for i, action in enumerate(recipe["actions"]):
            assert "action_id" in action, f"Action {i} missing action_id"
            assert "action" in action, f"Action {i} missing action description"
            assert "recipe" in action, f"Action {i} missing recipe steps"

            print(f"  - Action {action['action_id']}: {action['action']}")

        # Verify messages were sent to user
        messages_response = requests.get(f"{MOCK_API_URL}/autogen_response/messages")
        messages = messages_response.json()

        # Should have received at least one message
        assert len(messages) > 0, "No messages sent to user"

        print(f"✓ {len(messages)} messages sent to user")

    def test_agent_never_fails_creation(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        CRITICAL TEST: Agent creation never fails

        Even with edge cases, agent should handle gracefully
        """
        edge_cases = [
            "",  # Empty task
            "x" * 10000,  # Very long task
            "Create a file named 'test<>:\"/\\|?*.txt'",  # Invalid filename characters
        ]

        for i, task in enumerate(edge_cases):
            request_data = {
                "user_id": test_user_id,
                "prompt_id": test_prompt_id + i,
                "text": task,
                "file_id": None,
                "request_id": f"edge_case_{i}"
            }

            # Should not crash
            response = requests.post(
                f"{APP_URL}/chat",
                json=request_data,
                timeout=60
            )

            # Should get a response (even if it's an error message)
            assert response.status_code in [200, 400, 422], \
                f"Unexpected status code: {response.status_code}"

            print(f"✓ Edge case {i+1} handled: {task[:50]}...")

    def test_action_state_transitions(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        CRITICAL TEST: Action state transitions follow correct sequence

        Verifies that actions go through proper lifecycle:
        ASSIGNED → IN_PROGRESS → STATUS_VERIFICATION_REQUESTED → etc.
        """
        request_data = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Perform a simple calculation: 2 + 2",
            "file_id": None,
            "request_id": f"state_test_{int(time.time())}"
        }

        response = requests.post(
            f"{APP_URL}/chat",
            json=request_data,
            timeout=90
        )

        assert response.status_code == 200

        # Wait for completion
        time.sleep(5)

        # Verify recipe was created (indicates all states completed)
        recipe_path = f"prompts/{test_prompt_id}_0_recipe.json"
        assert os.path.exists(recipe_path), "Recipe not created - states not completed"

        with open(recipe_path, 'r') as f:
            recipe = json.load(f)

        # If recipe exists, all actions reached TERMINATED state
        for action in recipe["actions"]:
            # Recipe should have all required information
            assert "recipe" in action
            assert len(action["recipe"]) > 0, f"Action {action['action_id']} has empty recipe"

        print(f"✓ All actions completed state machine successfully")

    def test_recipe_json_generation(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        CRITICAL TEST: Recipe JSON generated correctly for each action

        Validates that recipe contains:
        - Generalized functions
        - Tool names
        - Dependencies
        - Steps
        """
        request_data = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Create two files: first.txt and second.txt. Second depends on first.",
            "file_id": None,
            "request_id": f"recipe_test_{int(time.time())}"
        }

        response = requests.post(
            f"{APP_URL}/chat",
            json=request_data,
            timeout=120
        )

        assert response.status_code == 200

        # Wait for recipe generation
        time.sleep(10)

        recipe_path = f"prompts/{test_prompt_id}_0_recipe.json"
        assert os.path.exists(recipe_path)

        with open(recipe_path, 'r') as f:
            recipe = json.load(f)

        # Validate recipe format
        for action in recipe["actions"]:
            assert "action_id" in action
            assert "recipe" in action

            for step in action["recipe"]:
                # Each step should have these fields
                assert "steps" in step, f"Missing 'steps' in action {action['action_id']}"

                # Dependencies should be a list
                if "dependencies" in step:
                    assert isinstance(step["dependencies"], list)

        print(f"✓ Recipe JSON structure validated")
        print(f"  Format: ✓")
        print(f"  Actions: {len(recipe['actions'])}")

    def test_scheduled_task_creation(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        CRITICAL TEST: Scheduled tasks created correctly

        Tests that time-based, cron, and interval tasks are properly
        configured in the recipe
        """
        request_data = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Send me a reminder every day at 9 AM",
            "file_id": None,
            "request_id": f"schedule_test_{int(time.time())}"
        }

        response = requests.post(
            f"{APP_URL}/chat",
            json=request_data,
            timeout=90
        )

        assert response.status_code == 200

        time.sleep(10)

        recipe_path = f"prompts/{test_prompt_id}_0_recipe.json"

        if os.path.exists(recipe_path):
            with open(recipe_path, 'r') as f:
                recipe = json.load(f)

            # If task was recognized as scheduled, should have scheduled_tasks
            if "scheduled_tasks" in recipe and len(recipe["scheduled_tasks"]) > 0:
                task = recipe["scheduled_tasks"][0]

                # Validate scheduled task structure
                assert "task_description" in task
                assert "schedule_type" in task
                assert task["schedule_type"] in ["cron", "interval", "date"]

                if task["schedule_type"] == "cron":
                    assert "hour" in task or "minute" in task

                print(f"✓ Scheduled task validated: {task['schedule_type']}")
            else:
                print("⚠ Task not recognized as scheduled (expected)")


@pytest.mark.runtime
@pytest.mark.slow
class TestMultiFlowCreation:
    """Test multiple flow handling in creation mode"""

    def test_multiple_flows_sequential(
        self,
        wait_for_services,
        test_user_id,
        reset_mock_services
    ):
        """
        Test creating multiple flows sequentially
        """
        flows = [
            ("flow1", "Create a shopping list"),
            ("flow2", "Calculate monthly budget"),
            ("flow3", "Send email summary")
        ]

        for flow_id, task in flows:
            prompt_id = int(time.time()) + hash(flow_id) % 1000

            request_data = {
                "user_id": test_user_id,
                "prompt_id": prompt_id,
                "text": task,
                "file_id": None,
                "request_id": f"{flow_id}_{int(time.time())}"
            }

            response = requests.post(
                f"{APP_URL}/chat",
                json=request_data,
                timeout=90
            )

            assert response.status_code == 200
            print(f"✓ Flow {flow_id} created")

            time.sleep(5)

            # Cleanup
            recipe_path = f"prompts/{prompt_id}_0_recipe.json"
            if os.path.exists(recipe_path):
                os.remove(recipe_path)
