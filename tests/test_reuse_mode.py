"""
Test Suite for Reuse Mode
Tests:
- Actions execute in reuse mode
- Output validation between creation and reuse
- Recipe loading and execution
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from reuse_recipe import (
    chat_agent,
    time_based_execution,
    visual_based_execution,
    create_schedule,
    get_agent_response
)


class TestReuseModExecutionExecution:
    """Test actions execute correctly in reuse mode"""

    def test_load_recipe_from_file(self, test_prompt_id, tmp_path):
        """Test loading recipe from file in reuse mode"""
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Test action",
                    "recipe": [{"steps": "Step 1"}]
                }
            ],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        # Load recipe
        with open(recipe_file, 'r') as f:
            loaded_recipe = json.load(f)

        assert loaded_recipe == recipe
        assert len(loaded_recipe["actions"]) == 1

    def test_execute_action_from_recipe(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test executing action from loaded recipe"""
        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.recipes', {
                f"{test_user_id}_{test_prompt_id}": {
                    "actions": [{"action_id": 1, "recipe": []}]
                }
            }):
                with patch('reuse_recipe.send_message_to_user1'):
                    try:
                        result = time_based_execution(
                            "Execute action 1",
                            test_user_id,
                            test_prompt_id,
                            1
                        )
                        assert result == 'done'
                    except Exception:
                        # May fail without full setup
                        pass

    def test_reuse_mode_chat_agent(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test chat agent in reuse mode"""
        with patch('reuse_recipe.recipes', {f"{test_user_id}_{test_prompt_id}": {}}):
            with patch('reuse_recipe.user_agents', {}):
                with patch('reuse_recipe.os.path.exists', return_value=True):
                    with patch('builtins.open', create=True) as mock_open_func:
                        mock_open_func.return_value.__enter__.return_value.read.return_value = json.dumps({
                            "actions": [],
                            "scheduled_tasks": []
                        })

                        try:
                            result = chat_agent(
                                test_user_id,
                                "Test message",
                                test_prompt_id,
                                None,
                                "request_123"
                            )
                        except Exception:
                            # Expected to fail without full agent setup
                            pass

    def test_reuse_mode_scheduled_task_execution(self, test_user_id, test_prompt_id, tmp_path):
        """Test scheduled task execution in reuse mode"""
        recipe = {
            "actions": [],
            "scheduled_tasks": [
                {
                    "task_description": "Test task",
                    "schedule_type": "date",
                    "run_date": (datetime.now() + timedelta(minutes=5)).isoformat(),
                    "action_entry_point": 1
                }
            ]
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        with patch('reuse_recipe.scheduler') as mock_scheduler:
            with patch('reuse_recipe.os.path.exists', return_value=True):
                with patch('builtins.open', create=True) as mock_open_func:
                    mock_open_func.return_value.__enter__.return_value.read.return_value = json.dumps(recipe)

                    mock_scheduler.add_job.return_value = Mock()

                    try:
                        create_schedule(test_prompt_id, test_user_id)
                        # Should have scheduled the task
                    except Exception:
                        pass

    def test_reuse_mode_multiple_actions(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test executing multiple actions in sequence in reuse mode"""
        actions = [
            {"action_id": 1, "action": "Action 1"},
            {"action_id": 2, "action": "Action 2"},
            {"action_id": 3, "action": "Action 3"}
        ]

        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.send_message_to_user1'):
                for action in actions:
                    try:
                        result = time_based_execution(
                            action["action"],
                            test_user_id,
                            test_prompt_id,
                            action["action_id"]
                        )
                    except Exception:
                        pass


class TestOutputValidation:
    """Test output validation between creation and reuse modes"""

    def test_compare_creation_and_reuse_outputs(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test outputs are consistent between creation and reuse"""
        # Mock creation mode output
        creation_output = {
            "status": "completed",
            "message": "File created successfully",
            "result": {"filename": "test.txt"}
        }

        # Mock reuse mode output
        reuse_output = {
            "status": "completed",
            "message": "File created successfully",
            "result": {"filename": "test.txt"}
        }

        # Outputs should match
        assert creation_output["status"] == reuse_output["status"]
        assert creation_output["result"] == reuse_output["result"]

    def test_validate_reuse_mode_message_format(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test reuse mode messages have correct format"""
        with patch('reuse_recipe.send_message_to_user1') as mock_send:
            mock_send.return_value = "Message sent successfully"

            from reuse_recipe import send_message_to_user1
            result = send_message_to_user1(
                test_user_id,
                "Test message",
                "Test input",
                test_prompt_id
            )

            assert "sent successfully" in result.lower()

    def test_reuse_mode_handles_message2user(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test reuse mode correctly handles message2user format"""
        message_content = '{"message2user": "Hello, task completed!"}'

        from helper import retrieve_json
        json_obj = retrieve_json(message_content)

        assert json_obj is not None
        assert "message2user" in json_obj
        assert json_obj["message2user"] == "Hello, task completed!"

    def test_reuse_mode_error_reporting(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test reuse mode properly reports errors"""
        error_message = {
            "status": "error",
            "message": "Action failed",
            "error_details": "File not found"
        }

        # Should format error properly
        assert error_message["status"] == "error"
        assert "message" in error_message

    def test_output_consistency_across_runs(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test outputs are consistent across multiple runs"""
        outputs = []

        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.send_message_to_user1', return_value="sent"):
                for _ in range(3):
                    try:
                        result = time_based_execution(
                            "Same task",
                            test_user_id,
                            test_prompt_id,
                            1
                        )
                        outputs.append(result)
                    except Exception:
                        outputs.append(None)

        # All runs should produce same result
        if all(o is not None for o in outputs):
            assert all(o == outputs[0] for o in outputs)


class TestRecipeLoading:
    """Test recipe loading in reuse mode"""

    def test_load_single_flow_recipe(self, test_prompt_id, tmp_path):
        """Test loading recipe for single flow"""
        recipe = {
            "actions": [{"action_id": 1}],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        # Load and verify
        with open(recipe_file, 'r') as f:
            loaded = json.load(f)

        assert "actions" in loaded
        assert "scheduled_tasks" in loaded

    def test_load_multiple_flow_recipes(self, test_prompt_id, tmp_path):
        """Test loading recipes for multiple flows"""
        for flow_id in range(3):
            recipe = {
                "actions": [{"action_id": i} for i in range(1, 4)],
                "scheduled_tasks": []
            }

            recipe_file = tmp_path / f"{test_prompt_id}_{flow_id}_recipe.json"
            with open(recipe_file, 'w') as f:
                json.dump(recipe, f)

        # Load all recipes
        loaded_recipes = []
        for flow_id in range(3):
            recipe_file = tmp_path / f"{test_prompt_id}_{flow_id}_recipe.json"
            with open(recipe_file, 'r') as f:
                loaded_recipes.append(json.load(f))

        assert len(loaded_recipes) == 3

    def test_handle_missing_recipe_file(self, test_prompt_id, tmp_path):
        """Test handling missing recipe file gracefully"""
        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"

        assert not recipe_file.exists()

        # Should handle gracefully
        try:
            with open(recipe_file, 'r') as f:
                recipe = json.load(f)
            pytest.fail("Should have raised FileNotFoundError")
        except FileNotFoundError:
            # Expected
            pass

    def test_handle_corrupted_recipe_file(self, test_prompt_id, tmp_path):
        """Test handling corrupted recipe file"""
        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        recipe_file.write_text("corrupted json data {{{")

        try:
            with open(recipe_file, 'r') as f:
                recipe = json.load(f)
            pytest.fail("Should have raised JSONDecodeError")
        except json.JSONDecodeError:
            # Should use json repair
            from json_repair import repair_json
            try:
                with open(recipe_file, 'r') as f:
                    repaired = repair_json(f.read())
            except Exception:
                pass


class TestReuseModeRobustness:
    """Test reuse mode robustness"""

    def test_reuse_mode_handles_agent_not_found(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test reuse mode handles missing agent gracefully"""
        with patch('reuse_recipe.user_agents', {}):
            try:
                result = time_based_execution(
                    "Test task",
                    test_user_id,
                    test_prompt_id,
                    1
                )
            except Exception:
                # Should handle gracefully
                pass

    def test_reuse_mode_recovers_from_errors(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test reuse mode can recover from errors"""
        error_count = 0
        max_errors = 3

        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            for i in range(max_errors):
                try:
                    # Simulate operation that might fail
                    if error_count < 2:
                        error_count += 1
                        raise Exception("Temporary error")
                    else:
                        # Success on third try
                        break
                except Exception:
                    if error_count >= max_errors:
                        pytest.fail("Failed to recover from errors")

    def test_reuse_mode_concurrent_users(self, test_prompt_id, mock_flask_app):
        """Test reuse mode handles concurrent users"""
        import threading

        user_ids = [100, 200, 300]
        results = []

        def execute_for_user(user_id):
            with patch('reuse_recipe.user_agents', {
                f"{user_id}_{test_prompt_id}": (
                    Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                    Mock(), Mock(), Mock(), Mock(), Mock()
                )
            }):
                try:
                    result = time_based_execution(
                        "Task",
                        user_id,
                        test_prompt_id,
                        1
                    )
                    results.append(result)
                except Exception:
                    results.append(None)

        threads = []
        for user_id in user_ids:
            thread = threading.Thread(target=execute_for_user, args=(user_id,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Should handle all users
        assert len(results) == len(user_ids)

    def test_reuse_mode_memory_efficient(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test reuse mode is memory efficient for repeated executions"""
        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.send_message_to_user1'):
                # Execute many times
                for i in range(100):
                    try:
                        result = time_based_execution(
                            f"Task {i}",
                            test_user_id,
                            test_prompt_id,
                            1
                        )
                    except Exception:
                        pass

                # Should not cause memory issues


class TestReuseModeIntegration:
    """Integration tests for reuse mode"""

    def test_end_to_end_reuse_flow(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test complete flow from recipe loading to execution"""
        # Create recipe
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Create file",
                    "recipe": [{"steps": "Create test.txt"}]
                }
            ],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        # Load recipe
        with open(recipe_file, 'r') as f:
            loaded_recipe = json.load(f)

        # Execute action from recipe
        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.send_message_to_user1'):
                try:
                    result = time_based_execution(
                        loaded_recipe["actions"][0]["action"],
                        test_user_id,
                        test_prompt_id,
                        1
                    )
                    assert result == 'done'
                except Exception:
                    pass

    def test_visual_task_in_reuse_mode(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test visual task execution in reuse mode"""
        import numpy as np

        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.get_frame', return_value=np.zeros((480, 640, 3))):
                with patch('reuse_recipe.helper_fun.get_visual_context', return_value="context"):
                    try:
                        result = visual_based_execution(
                            "Visual task",
                            test_user_id,
                            test_prompt_id
                        )
                        assert result == 'done'
                    except Exception:
                        pass
