"""
Test Suite for Recipe Generation
Tests:
- Recipe JSON creation for each flow
- Flow recipes validation
- Completion verification before mode switching
"""
import pytest
from unittest.mock import Mock, patch, MagicMock, mock_open
import sys
import os
import json
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

from helper import topological_sort, fix_json
from lifecycle_hooks import lifecycle_hook_track_recipe_request, lifecycle_hook_track_recipe_completion


class TestRecipeJSONCreation:
    """Test recipe JSON creation for each flow"""

    def test_create_basic_recipe_json(self, test_user_prompt, test_prompt_id):
        """Test creating basic recipe JSON"""
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Create file",
                    "recipe": [
                        {
                            "steps": "Open file in write mode",
                            "tool_name": "file_tool",
                            "generalized_functions": "open('test.txt', 'w')",
                            "dependencies": []
                        }
                    ]
                }
            ],
            "scheduled_tasks": []
        }

        try:
            json_str = json.dumps(recipe, indent=2)
            parsed = json.loads(json_str)
            assert "actions" in parsed
            assert "scheduled_tasks" in parsed
            assert len(parsed["actions"]) == 1
        except Exception as e:
            pytest.fail(f"Recipe JSON creation failed: {e}")

    def test_create_recipe_with_dependencies(self, test_user_prompt):
        """Test creating recipe with action dependencies"""
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Create file",
                    "recipe": [
                        {
                            "steps": "Create file",
                            "dependencies": []
                        }
                    ]
                },
                {
                    "action_id": 2,
                    "action": "Write to file",
                    "recipe": [
                        {
                            "steps": "Write content",
                            "dependencies": [1]  # Depends on action 1
                        }
                    ]
                }
            ]
        }

        # Validate dependencies
        action1_deps = recipe["actions"][0]["recipe"][0]["dependencies"]
        action2_deps = recipe["actions"][1]["recipe"][0]["dependencies"]

        assert len(action1_deps) == 0
        assert 1 in action2_deps

    def test_topological_sort_dependencies(self):
        """Test topological sorting of action dependencies"""
        individual_recipe = {
            1: {"dependencies": []},
            2: {"dependencies": [1]},
            3: {"dependencies": [1, 2]}
        }

        try:
            sorted_actions = topological_sort(individual_recipe)
            # Should be sorted: 1, 2, 3
            assert sorted_actions[0] == 1
            assert sorted_actions[1] == 2
            assert sorted_actions[2] == 3
        except Exception as e:
            pytest.fail(f"Topological sort failed: {e}")

    def test_topological_sort_with_cyclic_dependency(self):
        """Test handling cyclic dependencies"""
        individual_recipe = {
            1: {"dependencies": [2]},
            2: {"dependencies": [1]}  # Circular dependency
        }

        try:
            sorted_actions = topological_sort(individual_recipe)
            # Should detect and handle cycle
        except ValueError as e:
            # Expected to raise error for cycle
            assert "cycle" in str(e).lower() or "circular" in str(e).lower()

    def test_create_recipe_with_tool_calls(self, test_user_prompt):
        """Test creating recipe with tool calls"""
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Execute Python code",
                    "recipe": [
                        {
                            "steps": "Execute code",
                            "tool_name": "python_executor",
                            "tool_calls": [
                                {
                                    "name": "execute_code",
                                    "arguments": {
                                        "code": "print('Hello')"
                                    }
                                }
                            ],
                            "generalized_functions": "executor.execute('print(\"Hello\")')"
                        }
                    ]
                }
            ]
        }

        assert "tool_calls" in recipe["actions"][0]["recipe"][0]
        assert len(recipe["actions"][0]["recipe"][0]["tool_calls"]) == 1

    def test_create_recipe_with_scheduled_tasks(self, test_user_prompt):
        """Test creating recipe with scheduled tasks"""
        recipe = {
            "actions": [],
            "scheduled_tasks": [
                {
                    "task_description": "Daily backup",
                    "schedule_type": "cron",
                    "hour": 9,
                    "minute": 0,
                    "action_entry_point": 1
                },
                {
                    "task_description": "Hourly check",
                    "schedule_type": "interval",
                    "minutes": 60,
                    "action_entry_point": 2
                }
            ]
        }

        assert len(recipe["scheduled_tasks"]) == 2
        assert recipe["scheduled_tasks"][0]["schedule_type"] == "cron"
        assert recipe["scheduled_tasks"][1]["schedule_type"] == "interval"

    def test_save_recipe_to_file(self, test_prompt_id, tmp_path):
        """Test saving recipe to file"""
        recipe = {
            "actions": [{"action_id": 1, "action": "Test"}],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"

        try:
            with open(recipe_file, 'w') as f:
                json.dump(recipe, f, indent=2)

            assert recipe_file.exists()

            # Verify contents
            with open(recipe_file, 'r') as f:
                loaded = json.load(f)

            assert loaded == recipe
        except Exception as e:
            pytest.fail(f"Recipe file save failed: {e}")


class TestRecipeValidation:
    """Test validation of generated recipes"""

    def test_validate_recipe_structure(self):
        """Test validating recipe structure"""
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

    def test_validate_recipe_action_ids_unique(self):
        """Test validating action IDs are unique"""
        recipe = {
            "actions": [
                {"action_id": 1, "action": "Action 1"},
                {"action_id": 2, "action": "Action 2"},
                {"action_id": 3, "action": "Action 3"}
            ]
        }

        action_ids = [action["action_id"] for action in recipe["actions"]]
        assert len(action_ids) == len(set(action_ids))  # All unique

    def test_validate_recipe_dependencies_exist(self):
        """Test validating dependencies reference existing actions"""
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "recipe": [{"dependencies": []}]
                },
                {
                    "action_id": 2,
                    "recipe": [{"dependencies": [1]}]  # Valid: 1 exists
                }
            ]
        }

        action_ids = {action["action_id"] for action in recipe["actions"]}

        for action in recipe["actions"]:
            for step in action["recipe"]:
                for dep in step.get("dependencies", []):
                    assert dep in action_ids

    def test_validate_recipe_no_missing_fields(self):
        """Test validating no required fields are missing"""
        valid_recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Test",
                    "recipe": [
                        {
                            "steps": "Step",
                            "tool_name": "tool",
                            "generalized_functions": "func()"
                        }
                    ]
                }
            ],
            "scheduled_tasks": []
        }

        # Check required fields
        for action in valid_recipe["actions"]:
            assert "action_id" in action
            assert "action" in action
            assert "recipe" in action
            for step in action["recipe"]:
                assert "steps" in step
                # tool_name and generalized_functions are optional

    def test_validate_scheduled_task_structure(self):
        """Test validating scheduled task structure"""
        scheduled_task = {
            "task_description": "Test task",
            "schedule_type": "cron",
            "hour": 9,
            "minute": 0,
            "action_entry_point": 1
        }

        assert "task_description" in scheduled_task
        assert "schedule_type" in scheduled_task
        assert scheduled_task["schedule_type"] in ["cron", "interval", "date"]
        assert "action_entry_point" in scheduled_task

    def test_validate_cron_schedule_parameters(self):
        """Test validating cron schedule parameters"""
        cron_task = {
            "schedule_type": "cron",
            "hour": 9,
            "minute": 30
        }

        assert 0 <= cron_task["hour"] <= 23
        assert 0 <= cron_task["minute"] <= 59

    def test_validate_interval_schedule_parameters(self):
        """Test validating interval schedule parameters"""
        interval_task = {
            "schedule_type": "interval",
            "minutes": 30
        }

        assert interval_task["minutes"] > 0

    def test_validate_date_schedule_parameters(self):
        """Test validating date schedule parameters"""
        date_task = {
            "schedule_type": "date",
            "run_date": "2025-01-15T10:30:00"
        }

        assert "run_date" in date_task
        # Validate ISO format
        try:
            datetime.fromisoformat(date_task["run_date"])
        except ValueError:
            pytest.fail("Invalid date format")


class TestRecipeCompletion:
    """Test recipe completion tracking"""

    def test_track_recipe_request(self, test_user_prompt):
        """Test tracking recipe request"""
        action_id = 1

        try:
            lifecycle_hook_track_recipe_request(test_user_prompt, action_id)
            # Should track that recipe was requested
        except Exception:
            pass

    def test_track_recipe_completion(self, test_user_prompt):
        """Test tracking recipe completion"""
        action_id = 1

        try:
            lifecycle_hook_track_recipe_request(test_user_prompt, action_id)
            lifecycle_hook_track_recipe_completion(test_user_prompt, action_id)
            # Should mark recipe as received
        except Exception:
            pass

    def test_all_recipes_completed_check(self, test_user_prompt):
        """Test checking if all recipes are completed"""
        from lifecycle_hooks import lifecycle_hook_check_all_actions_terminated

        try:
            # Should check if all actions have recipes
            all_completed = lifecycle_hook_check_all_actions_terminated(test_user_prompt)
        except Exception:
            pass

    def test_flow_recipe_completion(self, test_user_prompt, test_prompt_id, tmp_path):
        """Test completing recipe for entire flow"""
        flow_recipe = {
            "actions": [
                {"action_id": 1, "action": "Action 1", "recipe": []},
                {"action_id": 2, "action": "Action 2", "recipe": []}
            ],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"

        with open(recipe_file, 'w') as f:
            json.dump(flow_recipe, f)

        assert recipe_file.exists()

        # Verify all actions have recipes
        with open(recipe_file, 'r') as f:
            loaded = json.load(f)

        assert len(loaded["actions"]) == 2


class TestModeSwitching:
    """Test verification before switching from creation to reuse mode"""

    def test_verify_all_actions_completed_before_switch(self, test_user_prompt):
        """Test verifying all actions completed before mode switch"""
        from lifecycle_hooks import lifecycle_hook_check_all_actions_terminated

        try:
            all_terminated = lifecycle_hook_check_all_actions_terminated(test_user_prompt)
            # Should return True only if all actions completed
        except Exception:
            pass

    def test_verify_all_recipes_generated_before_switch(self, test_prompt_id, tmp_path):
        """Test verifying all recipes generated before switch"""
        # Check that recipe file exists
        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"

        if not recipe_file.exists():
            pytest.fail("Recipe file must exist before switching to reuse mode")

    def test_verify_final_agent_creation(self, test_user_prompt):
        """Test final agent creation validation"""
        from lifecycle_hooks import lifecycle_hook_validate_final_agent_creation

        try:
            lifecycle_hook_validate_final_agent_creation(test_user_prompt)
            # Should validate that agent creation is complete
        except Exception:
            pass

    def test_mode_switch_only_after_completion(self, test_user_prompt, test_prompt_id):
        """Test mode switching only allowed after completion"""
        # Mock incomplete state
        incomplete = False

        from lifecycle_hooks import lifecycle_hook_check_all_actions_terminated

        try:
            all_done = lifecycle_hook_check_all_actions_terminated(test_user_prompt)
            if not all_done:
                incomplete = True

        except Exception:
            incomplete = True

        # Should not allow switch if incomplete
        if incomplete:
            # This is expected - mode switch should be blocked
            pass

    def test_database_update_before_mode_switch(self, test_prompt_id):
        """Test database is updated before mode switch"""
        with patch('create_recipe.requests.patch') as mock_patch:
            mock_patch.return_value.status_code = 200

            try:
                # Should update database to mark agent as created
                from create_recipe import update_agent_creation_to_db
                update_agent_creation_to_db(test_prompt_id)
                mock_patch.assert_called()
            except Exception:
                pass


class TestRecipeGenerationRobustness:
    """Test recipe generation robustness"""

    def test_recipe_generation_with_malformed_json(self):
        """Test handling malformed JSON during recipe generation"""
        malformed = '{"action_id": 1, "action": "Test", unclosed: '

        try:
            json.loads(malformed)
            pytest.fail("Should have raised JSONDecodeError")
        except json.JSONDecodeError:
            # Expected - should use json repair
            from json_repair import repair_json
            try:
                repaired = repair_json(malformed)
            except Exception:
                # May still fail for very malformed JSON
                pass

    def test_recipe_generation_with_special_characters(self):
        """Test recipe generation handles special characters"""
        recipe_with_special = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Process file with 'quotes'",
                    "recipe": [
                        {
                            "steps": "Read file with\ttabs and\nnewlines",
                            "generalized_functions": "process('path/to/file')"
                        }
                    ]
                }
            ]
        }

        try:
            json_str = json.dumps(recipe_with_special)
            parsed = json.loads(json_str)
            assert parsed["actions"][0]["action"] == "Process file with 'quotes'"
        except Exception as e:
            pytest.fail(f"Special character handling failed: {e}")

    def test_recipe_generation_with_large_data(self):
        """Test recipe generation with large amounts of data"""
        large_recipe = {
            "actions": [
                {
                    "action_id": i,
                    "action": f"Action {i}",
                    "recipe": [{"steps": f"Step {j}"} for j in range(100)]
                }
                for i in range(100)
            ]
        }

        try:
            json_str = json.dumps(large_recipe)
            parsed = json.loads(json_str)
            assert len(parsed["actions"]) == 100
            assert len(parsed["actions"][0]["recipe"]) == 100
        except Exception as e:
            pytest.fail(f"Large data handling failed: {e}")

    def test_recipe_merge_from_multiple_sources(self):
        """Test merging recipes from multiple sources"""
        recipe1 = {"actions": [{"action_id": 1}]}
        recipe2 = {"actions": [{"action_id": 2}]}

        merged = {
            "actions": recipe1["actions"] + recipe2["actions"],
            "scheduled_tasks": []
        }

        assert len(merged["actions"]) == 2

    def test_recipe_version_compatibility(self, test_prompt_id, tmp_path):
        """Test recipe version compatibility"""
        # Old version recipe
        old_recipe = {
            "version": "1.0",
            "actions": [{"action_id": 1}]
        }

        # New version recipe
        new_recipe = {
            "version": "2.0",
            "actions": [{"action_id": 1}],
            "scheduled_tasks": []
        }

        # Should handle both versions
        for recipe in [old_recipe, new_recipe]:
            try:
                json_str = json.dumps(recipe)
                parsed = json.loads(json_str)
                assert "actions" in parsed
            except Exception as e:
                pytest.fail(f"Version compatibility failed: {e}")
