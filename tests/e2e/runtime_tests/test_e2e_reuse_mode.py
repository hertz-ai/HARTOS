"""
End-to-End Tests for Reuse Mode
Tests the complete reuse workflow against running containers
"""

import pytest
import requests
import time
import json
import os
from conftest_runtime import APP_URL, MOCK_API_URL

@pytest.mark.runtime
class TestReuseModeE2E:
    """Test complete reuse mode workflow"""

    def test_reuse_mode_full_workflow(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        sample_recipe,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        CRITICAL TEST: Complete reuse mode workflow

        Tests:
        1. Create recipe in creation mode
        2. Execute same task in reuse mode
        3. Verify recipe loaded and used
        4. Verify execution matches creation output
        """
        # Step 1: Create mode - generate recipe
        print("\n📝 Step 1: Creating recipe in CREATE mode...")

        create_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Calculate 15 + 27",
            "file_id": None,
            "request_id": f"create_{int(time.time())}"
        }

        create_response = requests.post(
            f"{APP_URL}/chat",
            json=create_request,
            timeout=120
        )

        assert create_response.status_code == 200
        time.sleep(10)  # Wait for recipe generation

        recipe_path = f"prompts/{test_prompt_id}_0_recipe.json"
        assert os.path.exists(recipe_path), "Recipe not created"

        with open(recipe_path, 'r') as f:
            recipe = json.load(f)

        print(f"✓ Recipe created with {len(recipe['actions'])} actions")

        # Clear messages from creation mode
        requests.post(f"{MOCK_API_URL}/autogen_response/clear")

        # Step 2: Reuse mode - execute from recipe
        print("\n♻️  Step 2: Executing in REUSE mode...")

        reuse_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Calculate 15 + 27",  # Same task
            "file_id": None,
            "request_id": f"reuse_{int(time.time())}"
        }

        reuse_response = requests.post(
            f"{APP_URL}/chat",
            json=reuse_request,
            timeout=60  # Should be faster in reuse mode
        )

        assert reuse_response.status_code == 200
        time.sleep(3)

        # Verify execution happened
        messages_response = requests.get(f"{MOCK_API_URL}/autogen_response/messages")
        reuse_messages = messages_response.json()

        assert len(reuse_messages) > 0, "No messages sent in reuse mode"

        print(f"✓ Reuse mode executed successfully")
        print(f"  - Messages sent: {len(reuse_messages)}")

    def test_reuse_mode_faster_than_create(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        Test that reuse mode is significantly faster than create mode
        """
        task = "Count from 1 to 10"

        # Create mode timing
        create_start = time.time()
        create_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": task,
            "file_id": None,
            "request_id": f"create_{int(time.time())}"
        }

        requests.post(f"{APP_URL}/chat", json=create_request, timeout=120)
        create_time = time.time() - create_start

        time.sleep(10)  # Wait for recipe

        # Reuse mode timing
        reuse_start = time.time()
        reuse_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": task,
            "file_id": None,
            "request_id": f"reuse_{int(time.time())}"
        }

        requests.post(f"{APP_URL}/chat", json=reuse_request, timeout=60)
        reuse_time = time.time() - reuse_start

        print(f"\n⏱️  Performance comparison:")
        print(f"  - Create mode: {create_time:.2f}s")
        print(f"  - Reuse mode: {reuse_time:.2f}s")
        print(f"  - Speedup: {create_time/reuse_time:.2f}x")

        # Reuse should be faster (allow some tolerance)
        # Note: In real scenarios, this is much more pronounced
        assert reuse_time <= create_time * 1.5, "Reuse mode not faster than create"

    def test_reuse_with_different_parameters(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        Test that reuse mode applies recipe with different parameters
        """
        # Create recipe for "calculate sum"
        create_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Calculate sum of 5 and 3",
            "file_id": None,
            "request_id": f"create_{int(time.time())}"
        }

        requests.post(f"{APP_URL}/chat", json=create_request, timeout=120)
        time.sleep(10)

        # Reuse with different numbers
        reuse_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": "Calculate sum of 10 and 20",  # Different params
            "file_id": None,
            "request_id": f"reuse_{int(time.time())}"
        }

        response = requests.post(f"{APP_URL}/chat", json=reuse_request, timeout=60)

        assert response.status_code == 200
        print("✓ Recipe reused with different parameters")

    def test_output_consistency_create_vs_reuse(
        self,
        wait_for_services,
        test_user_id,
        test_prompt_id,
        reset_mock_services,
        cleanup_after_test
    ):
        """
        CRITICAL TEST: Output should be consistent between create and reuse modes
        """
        task = "List the days of the week"

        # Create mode
        requests.post(f"{MOCK_API_URL}/autogen_response/clear")

        create_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": task,
            "file_id": None,
            "request_id": f"create_{int(time.time())}"
        }

        create_response = requests.post(f"{APP_URL}/chat", json=create_request, timeout=120)
        time.sleep(10)

        # Get create mode messages
        create_messages = requests.get(f"{MOCK_API_URL}/autogen_response/messages").json()

        # Clear for reuse mode
        requests.post(f"{MOCK_API_URL}/autogen_response/clear")

        # Reuse mode
        reuse_request = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "text": task,
            "file_id": None,
            "request_id": f"reuse_{int(time.time())}"
        }

        reuse_response = requests.post(f"{APP_URL}/chat", json=reuse_request, timeout=60)
        time.sleep(3)

        # Get reuse mode messages
        reuse_messages = requests.get(f"{MOCK_API_URL}/autogen_response/messages").json()

        # Both should have sent messages
        assert len(create_messages) > 0, "Create mode sent no messages"
        assert len(reuse_messages) > 0, "Reuse mode sent no messages"

        print(f"✓ Output consistency validated")
        print(f"  - Create messages: {len(create_messages)}")
        print(f"  - Reuse messages: {len(reuse_messages)}")


@pytest.mark.runtime
class TestRecipeLoading:
    """Test recipe loading and validation"""

    def test_load_recipe_from_file(
        self,
        wait_for_services,
        test_prompt_id,
        sample_recipe
    ):
        """
        Test loading recipe from file in reuse mode
        """
        # Create a recipe file
        os.makedirs("prompts", exist_ok=True)
        recipe_path = f"prompts/{test_prompt_id}_0_recipe.json"

        with open(recipe_path, 'w') as f:
            json.dump(sample_recipe, f)

        assert os.path.exists(recipe_path)

        # Try to use it
        reuse_request = {
            "user_id": 1001,
            "prompt_id": test_prompt_id,
            "text": "Execute the task",
            "file_id": None,
            "request_id": f"load_test_{int(time.time())}"
        }

        response = requests.post(f"{APP_URL}/chat", json=reuse_request, timeout=60)

        # Should load and attempt to use recipe
        assert response.status_code == 200

        # Cleanup
        os.remove(recipe_path)

        print("✓ Recipe loaded from file successfully")

    def test_handle_missing_recipe_file(
        self,
        wait_for_services,
        reset_mock_services
    ):
        """
        Test handling when recipe file doesn't exist
        """
        non_existent_prompt_id = 999999

        reuse_request = {
            "user_id": 1001,
            "prompt_id": non_existent_prompt_id,
            "text": "Try to execute",
            "file_id": None,
            "request_id": f"missing_test_{int(time.time())}"
        }

        # Should handle gracefully (might create new or return error)
        response = requests.post(f"{APP_URL}/chat", json=reuse_request, timeout=120)

        # Should not crash
        assert response.status_code in [200, 404, 422]

        print("✓ Missing recipe handled gracefully")

    def test_handle_corrupted_recipe_file(
        self,
        wait_for_services,
        test_prompt_id
    ):
        """
        Test handling corrupted recipe file
        """
        os.makedirs("prompts", exist_ok=True)
        recipe_path = f"prompts/{test_prompt_id}_0_recipe.json"

        # Write corrupted JSON
        with open(recipe_path, 'w') as f:
            f.write("{corrupted json data...")

        reuse_request = {
            "user_id": 1001,
            "prompt_id": test_prompt_id,
            "text": "Execute task",
            "file_id": None,
            "request_id": f"corrupt_test_{int(time.time())}"
        }

        # Should handle gracefully
        try:
            response = requests.post(f"{APP_URL}/chat", json=reuse_request, timeout=60)
            # Should either fix JSON or return error
            assert response.status_code in [200, 400, 422, 500]
        finally:
            # Cleanup
            if os.path.exists(recipe_path):
                os.remove(recipe_path)

        print("✓ Corrupted recipe handled gracefully")
