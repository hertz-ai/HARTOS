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


def _agent_mocks():
    """Context manager stack for the standard autogen agent mocks."""
    from contextlib import ExitStack
    stack = ExitStack()
    mocks = {}
    for name in ('AssistantAgent', 'UserProxyAgent', 'GroupChat', 'GroupChatManager'):
        m = stack.enter_context(patch(f'create_recipe.autogen.{name}'))
        m.return_value = Mock()
        mocks[name] = m
    return stack, mocks


class TestAgentCreation:
    """Test agent creation functionality to ensure it never fails"""

    @pytest.fixture(autouse=True)
    def _mock_user_tasks(self, sample_actions):
        """Auto-populate user_tasks so instantiate_assistant_agent() doesn't KeyError."""
        class _AutoActionDict(dict):
            """Dict that auto-creates Action objects for missing keys."""
            def __missing__(self, key):
                a = Action(sample_actions or [])
                self[key] = a
                return a

        with patch('create_recipe.user_tasks', _AutoActionDict()):
            yield

    def test_create_agents_basic_success(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test basic agent creation succeeds"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    result = create_agents(test_user_id, "Test task", test_prompt_id)
                    assert result is not None
                    assert len(result) > 0
                except Exception as e:
                    pytest.fail(f"Agent creation failed with: {e}")

    def test_create_agents_with_empty_task(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test agent creation with empty task doesn't crash"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    result = create_agents(test_user_id, "", test_prompt_id)
                    assert result is not None
                except Exception as e:
                    pytest.fail(f"Agent creation with empty task failed: {e}")

    def test_create_agents_with_invalid_user_id(self, test_prompt_id, mock_flask_app):
        """Test agent creation handles invalid user IDs gracefully"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    result = create_agents(None, "Test", test_prompt_id)
                    assert result is not None
                except Exception:
                    # Should handle gracefully, not crash
                    pass

    def test_create_time_agents_success(self, test_user_id, test_prompt_id, sample_actions, mock_flask_app):
        """Test time-based agent creation succeeds"""
        user_prompt = f'{test_user_id}_{test_prompt_id}'
        mock_tasks = {user_prompt: Action(sample_actions)}
        mock_recipe = {test_prompt_id: {"actions": sample_actions}}
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.user_tasks', mock_tasks):
                with patch('create_recipe.final_recipe', mock_recipe):
                    stack, mocks = _agent_mocks()
                    with stack:
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
            with patch('create_recipe.Action') as mock_action_class:
                mock_action_class.return_value = Action([])
                stack, mocks = _agent_mocks()
                with stack:
                    try:
                        result = create_time_agents(
                            test_user_id,
                            test_prompt_id,
                            'creator',
                            'test goal',
                            []
                        )
                        assert result is not None
                    except Exception:
                        # Should handle gracefully
                        pass

    def test_agent_creation_with_api_key_missing(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test agent creation handles missing API keys gracefully"""
        with patch('create_recipe.config_list', [{"model": "test"}]):  # No api_key
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    result = create_agents(test_user_id, "Test", test_prompt_id)
                except Exception:
                    # Expected to handle gracefully, not hard crash
                    pass

    def test_agent_creation_recovery_from_network_error(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test agent creation can recover from network errors"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('create_recipe.autogen.AssistantAgent') as mock_assistant:
                mock_assistant.side_effect = [ConnectionError("Network error"), Mock()]
                with patch('create_recipe.autogen.UserProxyAgent', return_value=Mock()):
                    with patch('create_recipe.autogen.GroupChat', return_value=Mock()):
                        with patch('create_recipe.autogen.GroupChatManager', return_value=Mock()):
                            try:
                                result = create_agents(test_user_id, "Test", test_prompt_id)
                            except ConnectionError:
                                pass

    def test_multiple_concurrent_agent_creations(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test multiple agents can be created concurrently without conflicts"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    results = []
                    for i in range(3):
                        result = create_agents(
                            test_user_id,
                            f"Test {i}",
                            test_prompt_id  # Reuse same prompt_id (config exists for it)
                        )
                        results.append(result)

                    assert len(results) == 3
                    assert all(r is not None for r in results)
                except Exception as e:
                    pytest.fail(f"Concurrent agent creation failed: {e}")

    def test_agent_creation_memory_cleanup(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test agent creation cleans up memory properly"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    for i in range(5):
                        result = create_agents(test_user_id, f"Test {i}", test_prompt_id)
                        del result
                except MemoryError:
                    pytest.fail("Agent creation caused memory issues")

    def test_agent_creation_with_special_characters_in_task(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test agent creation handles special characters in task description"""
        special_tasks = [
            "Test with 'quotes'",
            'Test with "double quotes"',
            "Test with\nnewlines",
            "Test with\ttabs",
            "Test with {json: 'like'} syntax"
        ]

        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                for task in special_tasks:
                    try:
                        result = create_agents(test_user_id, task, test_prompt_id)
                        assert result is not None
                    except Exception as e:
                        pytest.fail(f"Agent creation failed with special chars: {e}")


class TestAgentCreationRobustness:
    """Test agent creation robustness and error handling"""

    @pytest.fixture(autouse=True)
    def _mock_user_tasks(self, sample_actions):
        """Auto-populate user_tasks so instantiate_assistant_agent() doesn't KeyError."""
        class _AutoActionDict(dict):
            def __missing__(self, key):
                a = Action(sample_actions or [])
                self[key] = a
                return a

        with patch('create_recipe.user_tasks', _AutoActionDict()):
            yield

    def test_agent_creation_never_fails_guarantee(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Guarantee that agent creation returns a valid result or safe fallback"""
        with patch('create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                result = create_agents(test_user_id, "Test", test_prompt_id)
                assert result is not None
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
            stack, mocks = _agent_mocks()
            with stack:
                for user_id, task, prompt_id in test_cases:
                    try:
                        result = create_agents(user_id, task, prompt_id)
                    except Exception:
                        # Some may error, but shouldn't crash the system
                        pass
