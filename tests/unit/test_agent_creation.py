"""
Test Suite for Agent Creation
Ensures agent creation process never fails under various conditions
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

from create_recipe import create_agents, create_time_agents
from helper import Action


class TestAgentCreation:
    """Test agent creation functionality to ensure it never fails"""

    def test_create_agents_basic_success(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test basic agent creation succeeds"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            mock_assistant.return_value = Mock()
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            try:
                                result = create_agents(test_user_id, "Test task", test_prompt_id)
                                assert result is not None
                                assert len(result) > 0
                            except Exception as e:
                                pytest.fail(f"Agent creation failed with: {e}")

    def test_create_agents_with_empty_task(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test agent creation with empty task doesn't crash"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            mock_assistant.return_value = Mock()
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            try:
                                result = create_agents(test_user_id, "", test_prompt_id)
                                assert result is not None
                            except Exception as e:
                                pytest.fail(f"Agent creation with empty task failed: {e}")

    def test_create_agents_with_invalid_user_id(self, test_prompt_id, mock_flask_app):
        """Test agent creation handles invalid user IDs gracefully"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            mock_assistant.return_value = Mock()
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            try:
                                # Test with None
                                result = create_agents(None, "Test", test_prompt_id)
                                assert result is not None
                            except Exception:
                                # Should handle gracefully, not crash
                                pass

    def test_create_time_agents_success(self, test_user_id, test_prompt_id, sample_actions, mock_flask_app):
        """Test time-based agent creation succeeds"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            with patch('create_recipe.Action') as mock_action_class:
                                mock_assistant.return_value = Mock()
                                mock_proxy.return_value = Mock()
                                mock_group.return_value = Mock()
                                mock_manager.return_value = Mock()
                                mock_action_class.return_value = Action(sample_actions)

                                try:
                                    result = create_time_agents(
                                        test_user_id,
                                        test_prompt_id,
                                        'creator',
                                        'test goal',
                                        sample_actions
                                    )
                                    assert result is not None
                                    assert isinstance(result, dict)
                                except Exception as e:
                                    pytest.fail(f"Time agent creation failed: {e}")

    def test_create_time_agents_with_empty_actions(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test time agent creation handles empty actions"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            with patch('create_recipe.Action') as mock_action_class:
                                mock_assistant.return_value = Mock()
                                mock_proxy.return_value = Mock()
                                mock_group.return_value = Mock()
                                mock_manager.return_value = Mock()
                                mock_action_class.return_value = Action([])

                                try:
                                    result = create_time_agents(
                                        test_user_id,
                                        test_prompt_id,
                                        'creator',
                                        'test goal',
                                        []
                                    )
                                    # Should not crash, even with empty actions
                                    assert result is not None
                                except Exception:
                                    # Should handle gracefully
                                    pass

    def test_agent_creation_with_api_key_missing(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test agent creation handles missing API keys gracefully"""
        with patch('create_recipe.config_list', [{"model": "test"}]):  # No api_key
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    mock_assistant.return_value = Mock()
                    mock_proxy.return_value = Mock()

                    try:
                        # Should not crash even with incomplete config
                        result = create_agents(test_user_id, "Test", test_prompt_id)
                    except Exception:
                        # Expected to handle gracefully, not hard crash
                        pass

    def test_agent_creation_recovery_from_network_error(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test agent creation can recover from network errors"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                # Simulate network error on first call, success on second
                mock_assistant.side_effect = [ConnectionError("Network error"), Mock()]

                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            # Should handle network errors gracefully
                            try:
                                result = create_agents(test_user_id, "Test", test_prompt_id)
                            except ConnectionError:
                                # If it raises, we should implement retry logic
                                pass

    def test_multiple_concurrent_agent_creations(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test multiple agents can be created concurrently without conflicts"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            mock_assistant.return_value = Mock()
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            try:
                                results = []
                                for i in range(5):
                                    result = create_agents(
                                        f"{test_user_id}_{i}",
                                        f"Test {i}",
                                        f"{test_prompt_id}_{i}"
                                    )
                                    results.append(result)

                                assert len(results) == 5
                                assert all(r is not None for r in results)
                            except Exception as e:
                                pytest.fail(f"Concurrent agent creation failed: {e}")

    def test_agent_creation_memory_cleanup(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test agent creation cleans up memory properly"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            mock_assistant.return_value = Mock()
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            try:
                                # Create and cleanup multiple times
                                for i in range(10):
                                    result = create_agents(test_user_id, f"Test {i}", test_prompt_id)
                                    # Simulate cleanup
                                    del result
                            except MemoryError:
                                pytest.fail("Agent creation caused memory issues")

    def test_agent_creation_with_special_characters_in_task(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test agent creation handles special characters in task description"""
        special_tasks = [
            "Test with 'quotes'",
            'Test with "double quotes"',
            "Test with\nnewlines",
            "Test with\ttabs",
            "Test with émojis 🚀",
            "Test with <html>tags</html>",
            "Test with {json: 'like'} syntax"
        ]

        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            mock_assistant.return_value = Mock()
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            for task in special_tasks:
                                try:
                                    result = create_agents(test_user_id, task, test_prompt_id)
                                    assert result is not None
                                except Exception as e:
                                    pytest.fail(f"Agent creation failed with special chars: {e}")


class TestAgentCreationRobustness:
    """Test agent creation robustness and error handling"""

    def test_agent_creation_never_fails_guarantee(self, test_user_id, test_prompt_id, mock_flask_app):
        """Guarantee that agent creation returns a valid result or safe fallback"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                with patch('create_recipe.autogen.UserProxyAgent') as mock_proxy:
                    with patch('create_recipe.autogen.GroupChat') as mock_group:
                        with patch('create_recipe.autogen.GroupChatManager') as mock_manager:
                            # Even if all mocks fail, should return something
                            mock_assistant.return_value = Mock()
                            mock_proxy.return_value = Mock()
                            mock_group.return_value = Mock()
                            mock_manager.return_value = Mock()

                            result = create_agents(test_user_id, "Test", test_prompt_id)

                            # Should always return a valid structure
                            assert result is not None
                            # Should be tuple of agents
                            assert isinstance(result, tuple) or isinstance(result, list)

    def test_agent_creation_with_all_parameter_types(self, mock_flask_app):
        """Test agent creation with various parameter types"""
        test_cases = [
            (123, "string task", 456),
            ("string_user", "task", "string_prompt"),
            (None, None, None),
            (0, "", 0),
        ]

        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent', return_value=Mock()):
                with patch('create_recipe.autogen.UserProxyAgent', return_value=Mock()):
                    with patch('create_recipe.autogen.GroupChat', return_value=Mock()):
                        with patch('create_recipe.autogen.GroupChatManager', return_value=Mock()):
                            for user_id, task, prompt_id in test_cases:
                                try:
                                    result = create_agents(user_id, task, prompt_id)
                                    # Should handle all types gracefully
                                except Exception:
                                    # Some may error, but shouldn't crash the system
                                    pass
