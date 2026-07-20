"""_bank_action_recipe_from_trace — actions must bank progress (#143).

The 4B frequently completes actions without emitting a recipe payload (the
#128 recovery edges advance them anyway), so flows walked deep but banked
nothing and re-walked from Action 1 every restart (goal 60834540771: one
action recipe in 3 weeks). The fix derives the action recipe from the tool
calls that ACTUALLY executed in the live group chat — never fabricated steps;
a workless action banks an explicit no-op marker.

Behavioural via extract-and-exec: importing create_recipe hangs in a bare
pytest env (import-time side effects wait on live services — verified
rc=124 after 590s), so the REAL function source is extracted and exec'd with
its boundary collaborators injected (user_tasks, helper_fun, current_app,
json); real file writes to tmp; observable JSON asserted.
"""
import json
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class _FakeTask:
    def get_action(self, idx):
        return {'action': 'synthesize weekly recap',
                'fallback_action': 'requery individually'}


def _load_bank_fn(tmp_path):
    src = open(os.path.join(_ROOT, 'create_recipe.py'), encoding='utf-8').read()
    block = re.search(
        r'def _bank_action_recipe_from_trace.*?\n        return False\n',
        src, re.DOTALL).group(0)
    helper_fun = SimpleNamespace(
        safe_prompt_path=lambda pid, flow, aid: str(
            tmp_path / f'{pid}_{flow}_{aid}.json'))
    ns = {
        'json': json,
        'user_tasks': {'u_test': _FakeTask()},
        'helper_fun': helper_fun,
        'current_app': SimpleNamespace(logger=MagicMock()),
    }
    exec(block, ns)
    return ns['_bank_action_recipe_from_trace'], ns


@pytest.fixture()
def banked(tmp_path):
    fn, ns = _load_bank_fn(tmp_path)

    def run(messages, action_id=2, user_prompt='u_test'):
        gc = SimpleNamespace(messages=messages)
        ok = fn(user_prompt, '999', 0, action_id, gc)
        path = tmp_path / f'999_0_{action_id}.json'
        data = json.load(open(path)) if path.exists() else None
        return ok, data, ns

    return run


class TestTraceBanking:
    def test_banks_executed_tool_calls(self, banked):
        ok, data, _ = banked([
            {'content': 'Execute Action 2: synthesize weekly recap'},
            {'tool_calls': [{'function': {
                'name': 'execute_windows_or_android_command',
                'arguments': '{"instructions": "query revenue"}'}}]},
            {'content': 'numbers retrieved'},
        ])
        assert ok is True
        assert data['action_id'] == 2
        assert data['status'] == 'done'
        assert data['recipe_source'] == 'execution_trace'
        assert data['recipe'][0]['tool_name'] == 'execute_windows_or_android_command'
        assert 'query revenue' in data['recipe'][0]['steps']
        assert data['action'] == 'synthesize weekly recap'

    def test_only_this_actions_window_counts(self, banked):
        """Tool calls BEFORE the action's Execute message belong to earlier
        actions and must not leak into this action's recipe."""
        ok, data, _ = banked([
            {'tool_calls': [{'function': {'name': 'earlier_tool',
                                          'arguments': '{}'}}]},
            {'content': 'Execute Action 2: synthesize'},
            {'tool_calls': [{'function': {'name': 'right_tool',
                                          'arguments': '{}'}}]},
        ])
        assert ok is True
        names = [s['tool_name'] for s in data['recipe']]
        assert 'right_tool' in names and 'earlier_tool' not in names

    def test_double_digit_action_not_matched_by_substring(self, banked):
        """For action_id=2, 'Execute Action 20:' is a LATER action, not part of
        action 2's window. Without the ':' delimiter the substring 'Execute
        Action 2' also matched 'Execute Action 20:' and (keeping the last match)
        banked action 20's tools onto action 2 — wrong for any flow with >=10
        actions (CREATE routinely decomposes into 11-23)."""
        ok, data, _ = banked([
            {'content': 'Execute Action 2: synthesize'},
            {'tool_calls': [{'function': {'name': 'action2_tool',
                                          'arguments': '{}'}}]},
            {'content': 'Execute Action 20: a later, unrelated action'},
            {'tool_calls': [{'function': {'name': 'action20_tool',
                                          'arguments': '{}'}}]},
        ], action_id=2)
        assert ok is True
        names = [s['tool_name'] for s in data['recipe']]
        assert 'action2_tool' in names, f"action 2's own tool missing: {names}"
        assert 'action20_tool' not in names, \
            f"action 20's tool leaked into action 2's recipe: {names}"

    def test_workless_action_banks_noop_marker_not_fabrication(self, banked):
        ok, data, _ = banked([
            {'content': 'Execute Action 2: think about things'},
            {'content': 'a plain reply, no tools'},
        ])
        assert ok is True
        assert len(data['recipe']) == 1
        assert data['recipe'][0]['tool_name'] == ''
        assert 'no-op' in data['recipe'][0]['steps']

    def test_failure_returns_false_never_raises(self, tmp_path):
        fn, ns = _load_bank_fn(tmp_path)
        ns['helper_fun'].safe_prompt_path = (
            lambda *a: (_ for _ in ()).throw(OSError('disk gone')))
        gc = SimpleNamespace(messages=[{'content': 'Execute Action 2: x'}])
        assert fn('u_test', '999', 0, 2, gc) is False
