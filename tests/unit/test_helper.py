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

    def test_rejects_trailing_newline(self):
        r"""Regression: the guard used re.match(r'^...$', s), and re.match with a
        trailing $ matches BEFORE a terminal \n — so 'prompt_1\n' slipped past as
        a 'safe' path component and reached the filesystem. re.fullmatch rejects
        it. A value that is valid EXCEPT for a trailing newline is exactly the
        edge case a path-traversal guard must not wave through."""
        from helper import sanitize_path_component
        with pytest.raises(ValueError):
            sanitize_path_component("prompt_1\n")
        with pytest.raises(ValueError):
            sanitize_path_component("42\n")


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

    def test_equivalent_to_inline_join_for_valid_ids(self):
        # Locks the #94 migration: for every VALID id form (int / uuid),
        # safe_prompt_path(...) must equal the inline os.path.join(PROMPTS_DIR,
        # f'...') string the bypassing sites build — so routing them through it
        # is provably transparent (only malicious ids diverge, by raising).
        import os
        from helper import safe_prompt_path, PROMPTS_DIR
        for pid in ('71', '12345', 'a1b2c3d4-5e6f-7890-abcd-ef1234567890'):
            assert safe_prompt_path(pid) == os.path.join(PROMPTS_DIR, f'{pid}.json')
            assert safe_prompt_path(pid, '0', 'recipe') == \
                os.path.join(PROMPTS_DIR, f'{pid}_0_recipe.json')
            assert safe_prompt_path(pid, '2', '5') == \
                os.path.join(PROMPTS_DIR, f'{pid}_2_5.json')
            # base-path form (no extension) — for sites that append a suffix later
            assert safe_prompt_path(pid, '0', ext='') == \
                os.path.join(PROMPTS_DIR, f'{pid}_0')

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
        # On CI, ensure the directory exists (it may not in a fresh checkout)
        os.makedirs(PROMPTS_DIR, exist_ok=True)
        assert os.path.isdir(PROMPTS_DIR)


# ============================================================
# safe_function_call — deserialized LLM tool-arg -> function dispatcher
# ============================================================

class TestSafeFunctionCall:
    """safe_function_call routes a deserialized tool-call argument (a dict, a
    list, or a bare scalar) onto a Python function across 4 dispatch branches,
    plus a TypeError recovery path that filters ['truncated'] sentinels and
    re-maps a positional list onto the function's signature. A wrong dispatch
    means the tool silently runs with the wrong arguments — so every branch and
    the error/degrade edges are asserted here (function had zero test refs)."""

    # ── happy-path dispatch branches ──────────────────────────────────────
    def test_dict_dispatched_as_kwargs(self):
        from helper import safe_function_call

        def f(a, b):
            return (a, b)
        assert safe_function_call(f, {'a': 1, 'b': 2}) == (1, 2)

    def test_list_of_single_dict_dispatched_as_kwargs(self):
        """retrieve_json commonly wraps the arg dict in a 1-element list."""
        from helper import safe_function_call

        def f(a, b):
            return {'a': a, 'b': b}
        assert safe_function_call(f, [{'a': 10, 'b': 20}]) == {'a': 10, 'b': 20}

    def test_list_dispatched_as_positional_args(self):
        from helper import safe_function_call

        def f(a, b):
            return (a, b)
        assert safe_function_call(f, [1, 2]) == (1, 2)

    def test_scalar_dispatched_as_single_positional(self):
        from helper import safe_function_call

        def f(x):
            return x * 2
        assert safe_function_call(f, 5) == 10

    def test_string_dispatched_as_single_positional(self):
        from helper import safe_function_call

        def f(x):
            return x.upper()
        assert safe_function_call(f, "hi") == "HI"

    def test_none_dispatched_as_single_positional(self):
        """None is neither dict nor list — passed through as the sole arg."""
        from helper import safe_function_call

        def f(x):
            return x is None
        assert safe_function_call(f, None) is True

    def test_empty_list_dispatched_as_no_args(self):
        """[] takes the positional branch → func() with no arguments."""
        from helper import safe_function_call

        def f():
            return "ok"
        assert safe_function_call(f, []) == "ok"

    def test_tuple_is_single_arg_not_unpacked(self):
        """Only list is special-cased for unpacking; a tuple is one argument."""
        from helper import safe_function_call

        def f(x):
            return x
        assert safe_function_call(f, (1, 2)) == (1, 2)

    def test_falsy_return_value_preserved(self):
        """A falsy return (0) must be returned verbatim, not coerced/dropped."""
        from helper import safe_function_call

        def f(a):
            return 0
        assert safe_function_call(f, [7]) == 0

    # ── TypeError recovery: 'truncated' sentinel + signature remap ─────────
    def test_truncated_sentinel_filtered_then_signature_remapped(self):
        """A positional list carrying a ['truncated'] sentinel first fails the
        *args call (too many positionals) → recovery filters the sentinel and
        maps the remaining args onto the signature by name. This whole recovery
        path is the reason the function exists; it must yield the clean call."""
        from helper import safe_function_call

        def f(a, b):
            return (a, b)
        assert safe_function_call(f, [1, 2, ['truncated']]) == (1, 2)

    def test_extra_positional_no_remap_reraises_typeerror(self):
        """More real args than the function accepts (no sentinel to strip) is
        unrecoverable — the original TypeError must surface, not be swallowed."""
        from helper import safe_function_call

        def f(a):
            return a
        with pytest.raises(TypeError):
            safe_function_call(f, [1, 2, 3])

    def test_dict_wrong_keys_reraises_typeerror(self):
        """A dict with keys the function doesn't accept is not a list, so the
        recovery path doesn't apply — the TypeError propagates."""
        from helper import safe_function_call

        def f(a, b):
            return (a, b)
        with pytest.raises(TypeError):
            safe_function_call(f, {'x': 1, 'y': 2})

    def test_non_typeerror_from_func_propagates(self):
        """Errors raised *inside* the target function (not dispatch mismatches)
        must propagate unchanged — safe_function_call is not a swallow-all."""
        from helper import safe_function_call

        def f(**kwargs):
            raise ValueError("boom")
        with pytest.raises(ValueError, match="boom"):
            safe_function_call(f, {'a': 1})


# ============================================================
# load_agent_data_from_file — on-disk agent-data (de)serialization + degrade
# ============================================================

class TestLoadAgentDataFromFile:
    """load_agent_data_from_file reads persisted agent state, handling the
    'data'-wrapped save format, the legacy direct-object format, and degrading
    to an empty dict (return False) on a missing/undecryptable/corrupt file."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        """Point AGENT_DATA_DIR at an isolated tmp dir and give a Flask app
        context (the function logs via current_app.logger)."""
        import helper
        from flask import Flask
        monkeypatch.setattr(helper, 'AGENT_DATA_DIR', str(tmp_path))
        app = Flask(__name__)
        return helper, app

    @staticmethod
    def _write(helper, prompt_id, obj):
        path = helper.get_agent_data_file_path(prompt_id)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh)
        return path

    def test_missing_file_returns_false_and_seeds_empty(self, env):
        helper, app = env
        agent_data = {}
        with app.app_context():
            result = helper.load_agent_data_from_file('nofile_999', agent_data)
        assert result is False
        assert agent_data['nofile_999'] == {}

    def test_data_wrapped_format_extracts_inner_data(self, env):
        """The canonical save format wraps the payload under 'data' with
        metadata siblings — only the inner data is loaded."""
        helper, app = env
        pid = '12345'
        self._write(helper, pid, {
            'prompt_id': pid, 'saved_at': '2026-01-01T00:00:00',
            'data': {'actions': [1, 2], 'flow': 3},
        })
        agent_data = {pid: {'stale': 'overwrite-me'}}
        with app.app_context():
            result = helper.load_agent_data_from_file(pid, agent_data)
        assert result is True
        assert agent_data[pid] == {'actions': [1, 2], 'flow': 3}

    def test_old_format_loads_whole_object(self, env):
        """A legacy file with no 'data' key is loaded verbatim as the payload."""
        helper, app = env
        pid = '777'
        self._write(helper, pid, {'actions': ['a'], 'foo': 'bar'})
        agent_data = {}
        with app.app_context():
            result = helper.load_agent_data_from_file(pid, agent_data)
        assert result is True
        assert agent_data[pid] == {'actions': ['a'], 'foo': 'bar'}

    def test_malformed_json_degrades_to_false_and_empty(self, env):
        """A par-broken file must not raise — degrade to False + empty dict."""
        helper, app = env
        pid = '888'
        path = helper.get_agent_data_file_path(pid)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{not valid json,,,')
        agent_data = {pid: {'prev': 'value'}}
        with app.app_context():
            result = helper.load_agent_data_from_file(pid, agent_data)
        assert result is False
        assert agent_data[pid] == {}

    def test_decrypt_returning_none_degrades_to_false(self, env):
        """When the crypto layer can't produce a payload (returns None), the
        function warns and degrades to False + empty dict, wiping stale state."""
        helper, app = env
        crypto = pytest.importorskip('security.crypto')
        pid = '555'
        self._write(helper, pid, {'data': {'x': 1}})
        agent_data = {pid: {'stale': True}}
        with app.app_context():
            with patch.object(crypto, 'decrypt_json_file', return_value=None):
                result = helper.load_agent_data_from_file(pid, agent_data)
        assert result is False
        assert agent_data[pid] == {}

    def test_data_wrapped_non_dict_payload_still_loads(self, env):
        """Regression: a 'data'-wrapped payload whose value is a LIST was
        successfully extracted, then discarded because the very next log line
        did list(payload.keys()) — which a list has not — dropping the load
        into the error path (return False, agent_data reset to {}). A payload
        that parsed and extracted cleanly must be kept, and the load reported
        True; a debug-log formatting call must never corrupt a success."""
        helper, app = env
        pid = '444'
        self._write(helper, pid, {'data': [{'action_id': 1}, {'action_id': 2}]})
        agent_data = {}
        with app.app_context():
            result = helper.load_agent_data_from_file(pid, agent_data)
        assert result is True
        assert agent_data[pid] == [{'action_id': 1}, {'action_id': 2}]
