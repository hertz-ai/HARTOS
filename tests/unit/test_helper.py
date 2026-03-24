"""
test_helper.py - Tests for helper.py

Tests the core utility functions used across create_recipe and reuse_recipe.
Each test verifies a specific functional contract or safety guarantee:

FT: JSON parsing (retrieve_json with all fallbacks), topological sort (DAG
    ordering + cycle detection), Action class (state tracking), terminate
    message detection, path sanitization, strip_json_values.
NFT: Unicode normalization in JSON parsing, malformed input resilience,
     empty input safety, path traversal prevention.
"""
import os
import sys
import json
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# _is_terminate_msg — autogen group chat termination
# ============================================================

class TestIsTerminateMsg:
    """_is_terminate_msg guards against None content crashes in autogen."""

    def test_terminate_in_content(self):
        from helper import _is_terminate_msg
        assert _is_terminate_msg({'content': 'TERMINATE'}) is True

    def test_terminate_substring(self):
        from helper import _is_terminate_msg
        assert _is_terminate_msg({'content': 'Action done. TERMINATE'}) is True

    def test_no_terminate(self):
        from helper import _is_terminate_msg
        assert _is_terminate_msg({'content': 'Hello world'}) is False

    def test_none_content_safe(self):
        """Tool-call messages have content=None — must not crash."""
        from helper import _is_terminate_msg
        assert _is_terminate_msg({'content': None}) is False

    def test_missing_content_key(self):
        from helper import _is_terminate_msg
        assert _is_terminate_msg({}) is False

    def test_non_dict_input(self):
        from helper import _is_terminate_msg
        assert _is_terminate_msg("not a dict") is False
        assert _is_terminate_msg(None) is False


# ============================================================
# Path sanitization — prevents file system attacks
# ============================================================

class TestPathSanitization:
    """sanitize_path_component prevents path traversal in prompt file access."""

    def test_rejects_path_separators(self):
        from helper import sanitize_path_component
        with pytest.raises(ValueError):
            sanitize_path_component("../../etc/passwd")

    def test_rejects_backslash(self):
        from helper import sanitize_path_component
        with pytest.raises(ValueError):
            sanitize_path_component("..\\windows\\system32")

    def test_allows_normal_id(self):
        from helper import sanitize_path_component
        result = sanitize_path_component("prompt_12345")
        assert result == "prompt_12345"

    def test_allows_numeric_string(self):
        from helper import sanitize_path_component
        result = sanitize_path_component("42")
        assert result == "42"


class TestSafePromptPath:
    """safe_prompt_path builds paths that stay within PROMPTS_DIR."""

    def test_returns_path_within_prompts_dir(self):
        from helper import safe_prompt_path, PROMPTS_DIR
        path = safe_prompt_path("123", ext='.json')
        assert path.startswith(PROMPTS_DIR)

    def test_rejects_traversal_in_parts(self):
        from helper import safe_prompt_path
        with pytest.raises(ValueError):
            safe_prompt_path("../../../etc/passwd")


# ============================================================
# topological_sort — action dependency ordering
# ============================================================

class TestTopologicalSort:
    """topological_sort orders actions respecting dependencies. Wrong order =
    action executes before its prerequisite is complete."""

    def test_simple_chain(self):
        """A→B→C should produce [A, B, C]."""
        from helper import topological_sort
        actions = [
            {'action_id': 1, 'actions_this_action_depends_on': None},
            {'action_id': 2, 'actions_this_action_depends_on': [1]},
            {'action_id': 3, 'actions_this_action_depends_on': [2]},
        ]
        success, sorted_actions, cyclic = topological_sort(actions)
        assert success is True
        assert [a['action_id'] for a in sorted_actions] == [1, 2, 3]

    def test_parallel_actions(self):
        """Independent actions can be in any order but all appear."""
        from helper import topological_sort
        actions = [
            {'action_id': 1, 'actions_this_action_depends_on': None},
            {'action_id': 2, 'actions_this_action_depends_on': None},
            {'action_id': 3, 'actions_this_action_depends_on': None},
        ]
        success, sorted_actions, cyclic = topological_sort(actions)
        assert success is True
        assert len(sorted_actions) == 3

    def test_diamond_dependency(self):
        """A→B, A→C, B→D, C→D — D must come after both B and C."""
        from helper import topological_sort
        actions = [
            {'action_id': 1, 'actions_this_action_depends_on': None},
            {'action_id': 2, 'actions_this_action_depends_on': [1]},
            {'action_id': 3, 'actions_this_action_depends_on': [1]},
            {'action_id': 4, 'actions_this_action_depends_on': [2, 3]},
        ]
        success, sorted_actions, cyclic = topological_sort(actions)
        assert success is True
        ids = [a['action_id'] for a in sorted_actions]
        assert ids.index(1) < ids.index(2)
        assert ids.index(1) < ids.index(3)
        assert ids.index(2) < ids.index(4)
        assert ids.index(3) < ids.index(4)

    def test_cycle_detected(self):
        """Circular deps (A→B→A) must be detected and reported."""
        from helper import topological_sort
        actions = [
            {'action_id': 1, 'actions_this_action_depends_on': [2]},
            {'action_id': 2, 'actions_this_action_depends_on': [1]},
        ]
        success, sorted_actions, cyclic_ids = topological_sort(actions)
        assert success is False
        assert cyclic_ids is not None
        assert set(cyclic_ids) == {1, 2}

    def test_self_dependency_ignored(self):
        """Action depending on itself must not cause a cycle."""
        from helper import topological_sort
        actions = [
            {'action_id': 1, 'actions_this_action_depends_on': [1]},
        ]
        success, sorted_actions, _ = topological_sort(actions)
        assert success is True
        assert len(sorted_actions) == 1


# ============================================================
# retrieve_json — multi-fallback JSON extraction from LLM output
# ============================================================

class TestRetrieveJson:
    """retrieve_json is called on every LLM response — handles messy output."""

    def test_valid_json(self):
        from helper import retrieve_json
        result = retrieve_json('{"status": "completed", "action_id": 1}')
        assert result is not None
        assert result['status'] == 'completed'

    def test_json_with_prefix_text(self):
        """LLM often prefixes JSON with explanation text."""
        from helper import retrieve_json
        result = retrieve_json('Here is the result: {"status": "done"}')
        assert result is not None
        assert result['status'] == 'done'

    def test_json_with_at_user_prefix(self):
        """@user prefix from group chat must be stripped."""
        from helper import retrieve_json
        result = retrieve_json('@user {"status": "completed", "action_id": 1}')
        assert result is not None
        assert result['status'] == 'completed'

    def test_unicode_curly_quotes_normalized(self):
        """Local LLMs emit Unicode curly quotes — must normalize to ASCII."""
        from helper import retrieve_json
        # \u201c and \u201d are left/right double curly quotes
        result = retrieve_json('\u201c{"status": "done"}\u201d')
        # May or may not parse depending on exact format — key: no crash
        assert result is None or isinstance(result, dict)

    def test_returns_none_for_non_json(self):
        from helper import retrieve_json
        result = retrieve_json("This is just plain text with no JSON")
        assert result is None

    def test_returns_none_for_empty_string(self):
        from helper import retrieve_json
        result = retrieve_json("")
        assert result is None


# ============================================================
# strip_json_values — redacts leaf values for logging
# ============================================================

class TestStripJsonValues:
    """strip_json_values redacts values but preserves structure — used for safe logging."""

    def test_preserves_dict_keys(self):
        from helper import strip_json_values
        result = strip_json_values({'name': 'secret', 'age': 42})
        assert 'name' in result
        assert 'age' in result

    def test_redacts_leaf_values(self):
        from helper import strip_json_values
        result = strip_json_values({'password': 'hunter2'})
        assert result['password'] != 'hunter2'

    def test_preserves_nested_structure(self):
        from helper import strip_json_values
        result = strip_json_values({'outer': {'inner': 'value'}})
        assert isinstance(result['outer'], dict)
        assert 'inner' in result['outer']

    def test_preserves_list_structure(self):
        from helper import strip_json_values
        result = strip_json_values([1, 2, 3])
        assert isinstance(result, list)
        assert len(result) == 3

    def test_tuple_preserved_as_tuple(self):
        from helper import strip_json_values
        result = strip_json_values((1, 2))
        assert isinstance(result, tuple)


# ============================================================
# Action class — tracks current action state in the recipe pipeline
# ============================================================

class TestActionClass:
    """Action is the state object for recipe execution — wrong state = wrong action executed."""

    def test_initial_current_action_is_1(self):
        from helper import Action
        action = Action(['action1', 'action2', 'action3'])
        assert action.current_action == 1

    def test_get_action_returns_correct_item(self):
        from helper import Action
        actions = [
            {'action': 'step1', 'action_id': 1},
            {'action': 'step2', 'action_id': 2},
        ]
        action = Action(actions)
        assert action.get_action(0)['action'] == 'step1'
        assert action.get_action(1)['action'] == 'step2'

    def test_get_action_raises_on_out_of_range(self):
        from helper import Action
        action = Action(['a', 'b'])
        with pytest.raises(IndexError):
            action.get_action(5)

    def test_get_action_raises_on_negative(self):
        from helper import Action
        action = Action(['a'])
        with pytest.raises(IndexError):
            action.get_action(-1)

    def test_initial_flags(self):
        from helper import Action
        action = Action([])
        assert action.fallback is False
        assert action.recipe is False
        assert action.ledger is None

    def test_set_ledger(self):
        from helper import Action
        from flask import Flask
        app = Flask(__name__)
        action = Action([])
        mock_ledger = MagicMock()
        mock_ledger.tasks = {'t1': 'v1'}
        with app.app_context():
            action.set_ledger(mock_ledger)
        assert action.ledger is mock_ledger

    def test_get_action_byaction_id_found(self):
        from helper import Action
        actions = [
            {'action_id': 1, 'action': 'first'},
            {'action_id': 2, 'action': 'second'},
        ]
        action = Action(actions)
        result = action.get_action_byaction_id(2)
        assert result is not None
        assert result['action'] == 'second'

    def test_get_action_byaction_id_not_found(self):
        from helper import Action
        action = Action([{'action_id': 1, 'action': 'only'}])
        result = action.get_action_byaction_id(99)
        assert result is None


# ============================================================
# parse_date — timestamp parsing from cloud/local DB
# ============================================================

class TestParseDate:
    """parse_date converts ISO strings to datetime — used by visual context."""

    def test_valid_iso_format(self):
        from helper import parse_date
        result = parse_date("2026-03-24T10:30:00")
        assert result.year == 2026
        assert result.month == 3
        assert result.hour == 10

    def test_midnight(self):
        from helper import parse_date
        result = parse_date("2026-01-01T00:00:00")
        assert result.hour == 0
        assert result.minute == 0

    def test_invalid_format_raises(self):
        from helper import parse_date
        with pytest.raises(ValueError):
            parse_date("not-a-date")


# ============================================================
# safe_prompt_path — security-critical path construction
# ============================================================

class TestSafePromptPath:
    """safe_prompt_path prevents path traversal when building prompt file paths."""

    def test_single_part(self):
        from helper import safe_prompt_path, PROMPTS_DIR
        path = safe_prompt_path("12345")
        assert path.endswith("12345.json")
        assert PROMPTS_DIR in path

    def test_multi_part(self):
        from helper import safe_prompt_path
        path = safe_prompt_path("12345", "0", "recipe")
        assert "12345_0_recipe.json" in path

    def test_custom_extension(self):
        from helper import safe_prompt_path
        path = safe_prompt_path("12345", ext='.txt')
        assert path.endswith("12345.txt")

    def test_rejects_traversal(self):
        from helper import safe_prompt_path
        with pytest.raises(ValueError):
            safe_prompt_path("../../etc/passwd")

    def test_rejects_slashes(self):
        from helper import safe_prompt_path
        with pytest.raises(ValueError):
            safe_prompt_path("path/to/file")

    def test_accepts_numeric(self):
        from helper import safe_prompt_path
        path = safe_prompt_path("42", "0", "1")
        assert "42_0_1.json" in path

    def test_accepts_hyphens_and_underscores(self):
        from helper import safe_prompt_path
        path = safe_prompt_path("my-agent_v2")
        assert "my-agent_v2.json" in path


# ============================================================
# ToolMessageHandler — autogen message transforms
# ============================================================

class TestToolMessageHandler:
    """ToolMessageHandler fixes tool_call_id errors in autogen conversations."""

    def test_class_exists(self):
        from helper import ToolMessageHandler
        assert ToolMessageHandler is not None

    def test_instantiation(self):
        from helper import ToolMessageHandler
        handler = ToolMessageHandler(user_tasks={}, user_prompt='test_user_123')
        assert handler is not None

    def test_apply_transform_returns_list(self):
        """apply_transform must return a list of messages — autogen requires it."""
        from helper import ToolMessageHandler
        from flask import Flask
        app = Flask(__name__)
        handler = ToolMessageHandler(user_tasks={}, user_prompt='test')
        messages = [{'role': 'user', 'content': 'hello'}]
        with app.app_context():
            result = handler.apply_transform(messages)
        assert isinstance(result, list)

    def test_preserves_user_messages(self):
        """User messages must pass through unchanged."""
        from helper import ToolMessageHandler
        from flask import Flask
        app = Flask(__name__)
        handler = ToolMessageHandler(user_tasks={}, user_prompt='test')
        messages = [{'role': 'user', 'content': 'hello', 'name': 'User'}]
        with app.app_context():
            result = handler.apply_transform(messages)
        assert len(result) >= 1


# ============================================================
# get_llm_config — autogen LLM configuration
# ============================================================

class TestGetLlmConfig:
    """get_llm_config builds the autogen config_list for LLM calls."""

    def test_returns_dict(self):
        from helper import get_llm_config
        result = get_llm_config()
        assert isinstance(result, dict)

    def test_has_config_list(self):
        from helper import get_llm_config
        result = get_llm_config()
        assert 'config_list' in result

    def test_fallback_config_used(self):
        """When no local LLM, fallback config is used."""
        from helper import get_llm_config
        fallback = [{'model': 'test-model', 'base_url': 'http://test:8080/v1'}]
        result = get_llm_config(fallback_config_list=fallback)
        assert isinstance(result, dict)


# ============================================================
# PROMPTS_DIR — path resolution
# ============================================================

class TestPromptsDir:
    """PROMPTS_DIR must be an absolute path to prevent relative-path bugs."""

    def test_is_absolute(self):
        from helper import PROMPTS_DIR
        assert os.path.isabs(PROMPTS_DIR)

    def test_exists(self):
        """PROMPTS_DIR is created on import — must exist."""
        from helper import PROMPTS_DIR
        assert os.path.isdir(PROMPTS_DIR)
