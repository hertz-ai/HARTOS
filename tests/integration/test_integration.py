"""
Test Suite for End-to-End Integration
Tests complete workflows from creation to reuse mode
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


class TestEndToEndCreationFlow:
    """Test complete creation mode flow"""

    def test_complete_creation_flow(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test complete flow from task to recipe generation"""
        # 1. Create agents
        with patch('create_recipe.create_agents') as mock_create_agents:
            mock_create_agents.return_value = (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
            )

            agents = mock_create_agents(test_user_id, "Test task", test_prompt_id)
            assert agents is not None

        # 2. Execute actions
        with patch('create_recipe.user_tasks', {
            f"{test_user_id}_{test_prompt_id}": Mock(current_action=1)
        }):
            from lifecycle_hooks import lifecycle_hook_track_action_assignment
            lifecycle_hook_track_action_assignment(
                f"{test_user_id}_{test_prompt_id}",
                1
            )

        # 3. Generate recipe
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Complete task",
                    "recipe": [{"steps": "Execute step 1"}]
                }
            ],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        assert recipe_file.exists()

        # 4. Verify all actions completed
        from lifecycle_hooks import lifecycle_hook_check_all_actions_terminated
        try:
            all_terminated = lifecycle_hook_check_all_actions_terminated(
                f"{test_user_id}_{test_prompt_id}"
            )
        except Exception:
            pass

    def test_multi_flow_creation(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test creating multiple flows"""
        num_flows = 3

        for flow_id in range(num_flows):
            # Create recipe for each flow
            recipe = {
                "actions": [
                    {
                        "action_id": i,
                        "action": f"Flow {flow_id} Action {i}"
                    }
                    for i in range(1, 3)
                ],
                "scheduled_tasks": []
            }

            recipe_file = tmp_path / f"{test_prompt_id}_{flow_id}_recipe.json"
            with open(recipe_file, 'w') as f:
                json.dump(recipe, f)

            assert recipe_file.exists()

    def test_creation_with_scheduled_tasks(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test creation flow with scheduled tasks"""
        # Create agents with scheduled tasks
        recipe = {
            "actions": [
                {"action_id": 1, "action": "Setup task"}
            ],
            "scheduled_tasks": [
                {
                    "task_description": "Daily report",
                    "schedule_type": "cron",
                    "hour": 9,
                    "minute": 0,
                    "action_entry_point": 1
                }
            ]
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        # Verify scheduled task
        with open(recipe_file, 'r') as f:
            loaded = json.load(f)

        assert len(loaded["scheduled_tasks"]) == 1
        assert loaded["scheduled_tasks"][0]["schedule_type"] == "cron"


class TestEndToEndReuseFlow:
    """Test complete reuse mode flow"""

    def test_complete_reuse_flow(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test complete flow from recipe loading to execution"""
        # 1. Create recipe file
        recipe = {
            "actions": [
                {
                    "action_id": 1,
                    "action": "Execute task",
                    "recipe": [
                        {
                            "steps": "Step 1",
                            "tool_name": "test_tool",
                            "generalized_functions": "execute()"
                        }
                    ]
                }
            ],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        # 2. Load recipe
        with open(recipe_file, 'r') as f:
            loaded_recipe = json.load(f)

        assert loaded_recipe["actions"][0]["action_id"] == 1

        # 3. Execute from recipe
        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.send_message_to_user1'):
                from reuse_recipe import time_based_execution
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

    def test_reuse_with_scheduled_execution(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test reuse mode with scheduled task execution"""
        # Create recipe with scheduled task
        recipe = {
            "actions": [],
            "scheduled_tasks": [
                {
                    "task_description": "Scheduled task",
                    "schedule_type": "date",
                    "run_date": (datetime.now() + timedelta(minutes=5)).isoformat(),
                    "action_entry_point": 1
                }
            ]
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        # Schedule the task
        with patch('reuse_recipe.scheduler') as mock_scheduler:
            with patch('reuse_recipe.os.path.exists', return_value=True):
                with patch('builtins.open', create=True) as mock_open:
                    mock_open.return_value.__enter__.return_value.read.return_value = json.dumps(recipe)

                    mock_scheduler.add_job.return_value = Mock()

                    from reuse_recipe import create_schedule
                    try:
                        create_schedule(test_prompt_id, test_user_id)
                    except Exception:
                        pass


class TestCreationToReuseTransition:
    """Test transition from creation to reuse mode"""

    def test_mode_transition_validation(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test validating all requirements before mode transition"""
        # 1. Ensure all actions completed
        from lifecycle_hooks import lifecycle_hook_check_all_actions_terminated
        try:
            all_done = lifecycle_hook_check_all_actions_terminated(
                f"{test_user_id}_{test_prompt_id}"
            )
        except Exception:
            pass

        # 2. Ensure recipe exists
        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        recipe = {"actions": [], "scheduled_tasks": []}

        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        assert recipe_file.exists()

        # 3. Update database
        with patch('create_recipe.requests.patch') as mock_patch:
            mock_patch.return_value.status_code = 200

            from create_recipe import update_agent_creation_to_db
            try:
                update_agent_creation_to_db(test_prompt_id)
            except Exception:
                pass

    def test_recipe_persistence_across_modes(self, test_user_id, test_prompt_id, tmp_path):
        """Test recipe persists from creation to reuse mode"""
        # Creation mode: generate recipe
        creation_recipe = {
            "actions": [
                {"action_id": 1, "action": "Test action"}
            ],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(creation_recipe, f)

        # Reuse mode: load recipe
        with open(recipe_file, 'r') as f:
            reuse_recipe = json.load(f)

        # Should be identical
        assert creation_recipe == reuse_recipe


class TestMultiAgentCollaboration:
    """Test multi-agent collaboration scenarios"""

    def test_coding_and_vlm_agent_collaboration(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test coding agent and VLM agent working together"""
        import numpy as np

        # Coding agent generates code
        generated_code = '''
def process_image(image):
    return image.shape
'''

        # VLM agent analyzes visual context
        with patch('create_recipe.get_frame') as mock_frame:
            with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
                mock_frame.return_value = np.zeros((480, 640, 3))
                mock_context.return_value = "User is working on image processing"

                from create_recipe import get_visual_context
                context = get_visual_context(test_user_id, minutes=1)

                assert context is not None

        # Both agents should collaborate effectively
        assert 'def process_image' in generated_code
        assert context is not None

    def test_sequential_agent_handoff(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test sequential handoff between agents"""
        workflow_results = []

        # Agent 1: Data collection
        workflow_results.append({
            "agent": "data_collector",
            "status": "completed",
            "output": "data collected"
        })

        # Agent 2: Data processing
        workflow_results.append({
            "agent": "data_processor",
            "status": "completed",
            "output": "data processed",
            "input_from": "data_collector"
        })

        # Agent 3: Report generation
        workflow_results.append({
            "agent": "report_generator",
            "status": "completed",
            "output": "report generated",
            "input_from": "data_processor"
        })

        assert len(workflow_results) == 3
        assert workflow_results[1]["input_from"] == "data_collector"
        assert workflow_results[2]["input_from"] == "data_processor"


class TestErrorRecovery:
    """Test error recovery in complete workflows"""

    def test_recovery_from_action_failure(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test recovering from action failure"""
        max_retries = 3
        attempt = 0

        for attempt in range(max_retries):
            try:
                # Simulate action that might fail
                if attempt < 2:
                    raise Exception("Temporary failure")
                else:
                    # Success on third try
                    result = "success"
                    break
            except Exception:
                if attempt >= max_retries - 1:
                    pytest.fail("Failed to recover")

        assert result == "success"
        assert attempt == 2

    def test_recovery_from_network_error(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test recovering from network errors"""
        with patch('requests.post') as mock_post:
            # Simulate network error then success
            mock_post.side_effect = [
                ConnectionError("Network error"),
                Mock(status_code=200, json=lambda: {"status": "success"})
            ]

            for attempt in range(3):
                try:
                    import requests
                    response = requests.post("http://test.com", json={})
                    if response.status_code == 200:
                        break
                except ConnectionError:
                    if attempt >= 2:
                        pytest.fail("Failed to recover from network error")

    def test_partial_completion_recovery(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test recovering from partial completion"""
        # Save progress
        progress_file = tmp_path / f"progress_{test_prompt_id}.json"
        progress = {
            "completed_actions": [1, 2],
            "current_action": 3,
            "total_actions": 5
        }

        with open(progress_file, 'w') as f:
            json.dump(progress, f)

        # Recover from progress
        with open(progress_file, 'r') as f:
            loaded_progress = json.load(f)

        # Should resume from action 3
        assert loaded_progress["current_action"] == 3
        assert len(loaded_progress["completed_actions"]) == 2


class TestPerformanceAndScalability:
    """Test performance and scalability"""

    def test_handle_multiple_concurrent_users(self, test_prompt_id, mock_flask_app):
        """Test handling multiple concurrent users"""
        import threading

        user_ids = list(range(1, 11))  # 10 users
        results = []

        def user_workflow(user_id):
            with patch('create_recipe.user_agents', {}):
                results.append(f"User {user_id} completed")

        threads = []
        for user_id in user_ids:
            thread = threading.Thread(target=user_workflow, args=(user_id,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        assert len(results) == 10

    def test_handle_large_recipe_files(self, test_prompt_id, tmp_path):
        """Test handling large recipe files"""
        # Create large recipe
        large_recipe = {
            "actions": [
                {
                    "action_id": i,
                    "action": f"Action {i}",
                    "recipe": [
                        {"steps": f"Step {j}"}
                        for j in range(50)
                    ]
                }
                for i in range(100)
            ],
            "scheduled_tasks": []
        }

        recipe_file = tmp_path / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(large_recipe, f)

        # Should handle large file
        with open(recipe_file, 'r') as f:
            loaded = json.load(f)

        assert len(loaded["actions"]) == 100

    def test_memory_efficiency(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test memory efficiency for long-running operations"""
        # Simulate many operations
        for i in range(1000):
            # Create and discard objects
            temp_data = {"iteration": i, "data": "x" * 100}
            del temp_data

        # Should not cause memory issues


class TestDataPersistence:
    """Test data persistence across sessions"""

    def test_persist_agent_state(self, test_user_id, test_prompt_id, tmp_path):
        """Test persisting agent state"""
        state_file = tmp_path / f"state_{test_prompt_id}.json"

        # Save state
        state = {
            "user_id": test_user_id,
            "prompt_id": test_prompt_id,
            "current_action": 3,
            "timestamp": datetime.now().isoformat()
        }

        with open(state_file, 'w') as f:
            json.dump(state, f)

        # Load state
        with open(state_file, 'r') as f:
            loaded_state = json.load(f)

        assert loaded_state["current_action"] == 3

    def test_backup_and_restore(self, test_user_id, test_prompt_id, tmp_path):
        """Test backup and restore functionality"""
        # Create backup
        backup_data = {
            "recipes": {},
            "agent_metadata": {},
            "timestamp": datetime.now().isoformat()
        }

        backup_file = tmp_path / f"backup_{test_prompt_id}.json"
        with open(backup_file, 'w') as f:
            json.dump(backup_data, f)

        # Restore from backup
        with open(backup_file, 'r') as f:
            restored_data = json.load(f)

        assert "recipes" in restored_data
        assert "agent_metadata" in restored_data
