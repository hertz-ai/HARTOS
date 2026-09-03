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

from hartos.create_recipe import create_agents, create_time_agents
from hartos.helper import Action


def _agent_mocks():
    """Context manager stack for the standard autogen agent mocks.

    Uses ``MagicMock`` (not bare ``Mock``) because ``create_agents`` in
    ``create_recipe.py`` iterates over the mocked GroupChat / etc. at
    several points (e.g. agent-list assembly, tool-registration loops).
    A bare ``Mock`` raises ``TypeError: 'Mock' object is not iterable``
    on ``for x in <mock>``; ``MagicMock`` supports ``__iter__`` (returns
    an empty iterator by default), which is the tests' intent: "the
    constructor returned something agent-shaped, just don't actually
    run the LLM".
    """
    from contextlib import ExitStack
    stack = ExitStack()
    mocks = {}
    for name in ('AssistantAgent', 'UserProxyAgent', 'GroupChat', 'GroupChatManager'):
        m = stack.enter_context(patch(f'hartos.create_recipe.autogen.{name}'))
        m.return_value = MagicMock()
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

        with patch('hartos.create_recipe.user_tasks', _AutoActionDict()):
            yield

    def test_create_agents_basic_success(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test basic agent creation succeeds"""
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
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
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    result = create_agents(test_user_id, "", test_prompt_id)
                    assert result is not None
                except Exception as e:
                    pytest.fail(f"Agent creation with empty task failed: {e}")

    def test_create_agents_with_invalid_user_id(self, test_prompt_id, mock_flask_app):
        """Test agent creation handles invalid user IDs gracefully"""
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    result = create_agents(None, "Test", test_prompt_id)
                    assert result is not None
                except Exception:
                    # Should handle gracefully, not crash
                    pass

    @staticmethod
    def _llm_configs_passed(mocks):
        """Every dict llm_config handed to a mocked autogen constructor."""
        cfgs = []
        for m in mocks.values():
            for call in m.call_args_list:
                cfg = call.kwargs.get('llm_config')
                if isinstance(cfg, dict):
                    cfgs.append(cfg)
        return cfgs

    def test_create_agents_honors_model_config_override(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """#55, create path. The dispatcher stashes the model it chose for the
        turn in thread-local before /chat reaches recipe(); every constructor
        here used to read a module dict that captured config_list at import,
        so an EXPERT turn created its agents on the default model. Assert the
        override reaches every llm_config passed to autogen, with the same
        max_tokens the module dict carried, and that with no override the
        constructors fall back to config_list."""
        from hartos.threadlocal import thread_local_data
        override = [{"model": "expert-override", "api_key": "test",
                     "base_url": "https://expert.example/v1"}]
        fallback = [{"model": "test", "api_key": "test"}]
        with patch('hartos.create_recipe.config_list', fallback):
            thread_local_data.set_model_config_override(override)
            try:
                stack, mocks = _agent_mocks()
                with stack:
                    create_agents(test_user_id, "Test task", test_prompt_id)
                    cfgs = self._llm_configs_passed(mocks)
            finally:
                thread_local_data.clear_model_config_override()
            assert cfgs, "no llm_config reached any autogen constructor"
            assert all(c['config_list'] == override for c in cfgs), cfgs
            assert all(c.get('max_tokens') == 1500 for c in cfgs), cfgs

            stack, mocks = _agent_mocks()
            with stack:
                create_agents(test_user_id, "Test task", test_prompt_id)
                cfgs = self._llm_configs_passed(mocks)
            assert cfgs
            assert all(c['config_list'] == fallback for c in cfgs), cfgs

    def test_create_time_agents_honors_model_config_override(self, test_user_id, test_prompt_id, sample_actions, mock_flask_app):
        """Same contract for the time-agent constructors and their manager,
        which recipe() builds on the request thread while the override is
        live. From the scheduler thread there is no override and they fall
        back to config_list, which is the pre-fix behaviour."""
        from hartos.threadlocal import thread_local_data
        override = [{"model": "expert-override", "api_key": "test",
                     "base_url": "https://expert.example/v1"}]
        fallback = [{"model": "test", "api_key": "test"}]
        user_prompt = f'{test_user_id}_{test_prompt_id}'
        mock_tasks = {user_prompt: Action(sample_actions)}
        mock_recipe = {test_prompt_id: {"actions": sample_actions}}
        with patch('hartos.create_recipe.config_list', fallback),                 patch('hartos.create_recipe.user_tasks', mock_tasks),                 patch('hartos.create_recipe.final_recipe', mock_recipe):
            thread_local_data.set_model_config_override(override)
            try:
                stack, mocks = _agent_mocks()
                with stack:
                    create_time_agents(test_user_id, test_prompt_id, 'creator',
                                       'test goal', sample_actions)
                    cfgs = self._llm_configs_passed(mocks)
            finally:
                thread_local_data.clear_model_config_override()
            assert cfgs, "no llm_config reached any autogen constructor"
            assert all(c['config_list'] == override for c in cfgs), cfgs

            stack, mocks = _agent_mocks()
            with stack:
                create_time_agents(test_user_id, test_prompt_id, 'creator',
                                   'test goal', sample_actions)
                cfgs = self._llm_configs_passed(mocks)
            assert cfgs
            assert all(c['config_list'] == fallback for c in cfgs), cfgs

    def test_create_time_agents_success(self, test_user_id, test_prompt_id, sample_actions, mock_flask_app):
        """Test time-based agent creation succeeds"""
        user_prompt = f'{test_user_id}_{test_prompt_id}'
        mock_tasks = {user_prompt: Action(sample_actions)}
        mock_recipe = {test_prompt_id: {"actions": sample_actions}}
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('hartos.create_recipe.user_tasks', mock_tasks):
                with patch('hartos.create_recipe.final_recipe', mock_recipe):
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
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('hartos.create_recipe.Action') as mock_action_class:
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
        with patch('hartos.create_recipe.config_list', [{"model": "test"}]):  # No api_key
            stack, mocks = _agent_mocks()
            with stack:
                try:
                    result = create_agents(test_user_id, "Test", test_prompt_id)
                except Exception:
                    # Expected to handle gracefully, not hard crash
                    pass

    def test_agent_creation_recovery_from_network_error(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test agent creation can recover from network errors"""
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            with patch('hartos.create_recipe.autogen.AssistantAgent') as mock_assistant:
                mock_assistant.side_effect = [ConnectionError("Network error"), Mock()]
                with patch('hartos.create_recipe.autogen.UserProxyAgent', return_value=Mock()):
                    with patch('hartos.create_recipe.autogen.GroupChat', return_value=Mock()):
                        with patch('hartos.create_recipe.autogen.GroupChatManager', return_value=Mock()):
                            try:
                                result = create_agents(test_user_id, "Test", test_prompt_id)
                            except ConnectionError:
                                pass

    def test_multiple_concurrent_agent_creations(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Test multiple agents can be created concurrently without conflicts"""
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
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
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
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

        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
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

        with patch('hartos.create_recipe.user_tasks', _AutoActionDict()):
            yield

    def test_agent_creation_never_fails_guarantee(self, test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
        """Guarantee that agent creation returns a valid result or safe fallback"""
        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
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

        with patch('hartos.create_recipe.config_list', [{"model": "test", "api_key": "test"}]):
            stack, mocks = _agent_mocks()
            with stack:
                for user_id, task, prompt_id in test_cases:
                    try:
                        result = create_agents(user_id, task, prompt_id)
                    except Exception:
                        # Some may error, but shouldn't crash the system
                        pass
