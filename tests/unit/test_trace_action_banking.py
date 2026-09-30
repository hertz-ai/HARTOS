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
    src = open(os.path.join(_ROOT, 'hartos/create_recipe.py'), encoding='utf-8').read()
    block = re.search(
        r'def _bank_action_recipe_from_trace.*?\n        return False\n',
        src, re.DOTALL).group(0)
    helper_fun = SimpleNamespace(
        safe_prompt_path=lambda pid, flow, aid: str(
            tmp_path / f'{pid}_{flow}_{aid}.json'))
    # create_recipe now banks the recipe via the canonical crash-safe writer
    # (core.file_cache.atomic_json_write) instead of a bare open()+json.dump, so
    # the extracted function needs that collaborator injected too — otherwise it
    # NameErrors on the write and banking silently returns False.
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from core.file_cache import atomic_json_write
    ns = {
        'json': json,
        'atomic_json_write': atomic_json_write,
        'user_tasks': {'u_test': _FakeTask()},
        'helper_fun': helper_fun,
        'current_app': SimpleNamespace(logger=MagicMock()),
    }
    exec(block, ns)
    return ns['_bank_action_recipe_from_trace'], ns


@pytest.fixture()
def banked(tmp_path):
    fn, ns = _load_bank_fn(tmp_path)

    def run(messages, action_id=2, user_prompt='u_test', agents=None):
        gc = SimpleNamespace(messages=messages, agents=agents or [])
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

    def test_a_re_posted_action_keeps_the_work_of_its_earlier_window(self, banked):
        """Central 2026-09-13 (Compute Recruiter, action 2): the searches ran
        after the first "Execute Action 2:", the ChatInstructor re-posted the
        same action at 20:17:13 to wrap the round, and banking only the last
        window (a closing verdict, no tool call) saved "no-op" for real work."""
        ok, data, _ = banked([
            {'content': 'Execute Action 2: google_search: recent GPU cost posts'},
            {'tool_calls': [{'function': {'name': 'google_search',
                                          'arguments': '{"query": "idle GPU"}'}}]},
            {'content': 'Strong material coming in from Hacker News.'},
            {'content': 'Execute Action 2: google_search: recent GPU cost posts'},
            {'content': '{"status": "completed", "action_id": 2}'},
        ])
        assert ok is True
        names = [s['tool_name'] for s in data['recipe']]
        assert names == ['google_search'], data['recipe']

    def test_a_retry_that_did_work_supersedes_the_earlier_attempt(self, banked):
        ok, data, _ = banked([
            {'content': 'Execute Action 2: synthesize'},
            {'tool_calls': [{'function': {'name': 'first_try',
                                          'arguments': '{}'}}]},
            {'content': 'Execute Action 2: synthesize'},
            {'tool_calls': [{'function': {'name': 'second_try',
                                          'arguments': '{}'}}]},
        ])
        assert ok is True
        names = [s['tool_name'] for s in data['recipe']]
        assert names == ['second_try'], names

    def test_an_action_this_run_never_dispatched_is_not_banked(self, banked):
        """Resuming after a restart, a COMPLETED action whose recipe is missing
        reaches this function with a fresh group chat: its work ran in an
        earlier process and none of it is in the trace.  Banking then wrote a
        no-op recipe for work that really happened (central 2026-09-13, #90:
        23 of 23 actions of one agent), and the flow-recipe reconciler built
        an agent out of them that replays nothing."""
        ok, data, _ = banked([
            {'content': 'Execute Action 3: a later action'},
            {'tool_calls': [{'function': {'name': 'action3_tool',
                                          'arguments': '{}'}}]},
        ], action_id=2)
        assert ok is False
        assert data is None, 'a recipe was written for an action this run never ran'

    def test_an_empty_trace_banks_nothing(self, banked):
        ok, data, _ = banked([], action_id=1)
        assert ok is False
        assert data is None

    def test_failure_returns_false_never_raises(self, tmp_path):
        fn, ns = _load_bank_fn(tmp_path)
        ns['helper_fun'].safe_prompt_path = (
            lambda *a: (_ for _ in ()).throw(OSError('disk gone')))
        gc = SimpleNamespace(messages=[{'content': 'Execute Action 2: x'}])
        assert fn('u_test', '999', 0, 2, gc) is False

    def test_a_plain_text_action_banks(self, tmp_path):
        """The create flow stores each action as its plain text: every action
        of all three hive agents on central was a str (2026-09-13).  .get on
        that text raised "'str' object has no attribute 'get'", so no action
        was ever banked from its trace and each restart re-walked the flow
        from action 1."""
        fn, ns = _load_bank_fn(tmp_path)
        text = ('create_scheduled_jobs to schedule a recurring 6 hour job '
                'for continuous privacy safe threat monitoring')
        ns['user_tasks']['u_str'] = SimpleNamespace(get_action=lambda idx: text)
        gc = SimpleNamespace(messages=[
            {'content': 'Execute Action 1: ' + text},
            {'tool_calls': [{'function': {'name': 'create_scheduled_jobs',
                                          'arguments': '{"every_hours": 6}'}}]},
        ])
        assert fn('u_str', '999', 0, 1, gc) is True
        data = json.load(open(tmp_path / '999_0_1.json'))
        assert data['action'] == text
        assert data['fallback_action'] == ''
        assert data['recipe'][0]['tool_name'] == 'create_scheduled_jobs'


# ---------------------------------------------------------------------------
# CR3 (live drive 2026-09-25): the banker must record the work that SUCCEEDED.
#
# The executor answers a tool call it cannot run with
# "Error: Function <X> not found." (hartos/helper.py enhanced_execute_function,
# the patched ConversableAgent.execute_function), and HARTOS's wrapped tools
# answer an exception with core.tool_logging's "Tool execution failed: {...}"
# envelope.  Code work is not a tool call at all: the Assistant posts a
# ```python block and the Executor replies "exitcode: 0 (execution
# succeeded)\nCode output: ...".  The banker used to read only m['tool_calls']
# and hard-code generalized_functions='', so it banked the failed call as the
# action's recipe and dropped the code that actually did the work.
# ---------------------------------------------------------------------------

_HASH_CODE = ("```python\nimport hashlib\n"
              "print(hashlib.sha256(b'livetest_cr3_verify').hexdigest())\n```")


def _failed_call(call_id, name='save_data_in_memory'):
    return [
        {'content': '@Helper doing it', 'name': 'Assistant',
         'tool_calls': [{'id': call_id, 'function': {
             'name': name, 'arguments': '{"key": "user.current_goal"}'}}]},
        {'content': f'Error: Function {name} not found.', 'role': 'tool',
         'name': 'Helper',
         'tool_responses': [{'tool_call_id': call_id, 'role': 'tool',
                             'content': f'Error: Function {name} not found.'}]},
    ]


def _code_run(code=_HASH_CODE, exitcode=0):
    verdict = 'execution succeeded' if exitcode == 0 else 'execution failed'
    return [
        {'content': 'Let me write it.\n\n' + code, 'name': 'Assistant'},
        {'content': f'exitcode: {exitcode} ({verdict})\nCode output: \n'
                    + 'f' * 64 + '\n', 'role': 'user', 'name': 'Executor'},
    ]


class TestTraceBankingRecordsWhatSucceeded:
    def test_the_live_cr3_trace_banks_the_code_not_the_failed_call(self, banked):
        """The exact shape measured live: a failed tool call, then a code block
        the Executor ran with exitcode 0, then the completed verdict."""
        ok, data, _ = banked(
            [{'content': 'Execute Action 1: execute_coding_task: compute sha256',
              'name': 'ChatInstructor'}]
            + _failed_call('a')
            + _code_run()
            + [{'content': '{"status":"completed","action_id":1}',
                'name': 'Assistant'}],
            action_id=1)
        assert ok is True
        steps = data['recipe']
        assert [s['tool_name'] for s in steps] == [''], steps
        assert 'hashlib.sha256' in steps[0]['generalized_functions'], steps
        assert steps[0]['agent_to_perform_this_action'] == 'Executor', steps

    def test_a_code_only_action_is_not_banked_as_noop(self, banked):
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: compute a hash'}] + _code_run())
        assert ok is True
        assert len(data['recipe']) == 1, data['recipe']
        assert 'no-op' not in data['recipe'][0]['steps']
        assert 'import hashlib' in data['recipe'][0]['generalized_functions']

    def test_code_that_failed_to_run_is_not_banked(self, banked):
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: compute a hash'}]
            + _code_run(exitcode=1))
        assert ok is True
        assert data['recipe'][0]['generalized_functions'] == ''
        assert 'no-op' in data['recipe'][0]['steps']

    def test_a_call_whose_only_reply_is_an_error_banks_noop(self, banked):
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: remember the goal'}]
            + _failed_call('a'))
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == [''], data['recipe']
        assert 'no-op' in data['recipe'][0]['steps']

    def test_only_the_failed_call_is_dropped_paired_by_call_id(self, banked):
        """Two calls in one message, answered in one tool message: each reply
        is matched to its call by tool_call_id, never by position."""
        ok, data, _ = banked([
            {'content': 'Execute Action 2: search then save'},
            {'content': '', 'tool_calls': [
                {'id': 'ok1', 'function': {'name': 'google_search',
                                           'arguments': '{"query": "gpu"}'}},
                {'id': 'bad1', 'function': {'name': 'save_data_in_memory',
                                            'arguments': '{}'}}]},
            {'content': '...', 'role': 'tool', 'tool_responses': [
                {'tool_call_id': 'bad1', 'role': 'tool',
                 'content': 'Error: Function save_data_in_memory not found.'},
                {'tool_call_id': 'ok1', 'role': 'tool',
                 'content': 'three results'}]},
        ])
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == ['google_search']

    def test_the_tool_logging_failure_envelope_counts_as_failed(self, banked):
        """The failure text is taken from its real producer, so a change to the
        envelope's wording fails here instead of silently banking failures."""
        from core.tool_logging import _error_envelope
        envelope = _error_envelope('google_search', RuntimeError('quota'))
        ok, data, _ = banked([
            {'content': 'Execute Action 2: search'},
            {'content': '', 'tool_calls': [{'id': 'x', 'function': {
                'name': 'google_search', 'arguments': '{"query": "gpu"}'}}]},
            {'content': envelope, 'role': 'tool', 'tool_responses': [
                {'tool_call_id': 'x', 'role': 'tool', 'content': envelope}]},
        ])
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == [''], data['recipe']

    def test_tool_calls_and_code_keep_their_order(self, banked):
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: search then compute'},
             {'content': '', 'tool_calls': [{'id': 's', 'function': {
                 'name': 'google_search', 'arguments': '{"query": "gpu"}'}}]},
             {'content': 'results', 'role': 'tool', 'tool_responses': [
                 {'tool_call_id': 's', 'role': 'tool', 'content': 'results'}]}]
            + _code_run())
        assert ok is True
        steps = data['recipe']
        assert [s['agent_to_perform_this_action'] for s in steps] == [
            'Helper', 'Executor'], steps

    def test_a_secret_in_banked_code_is_redacted(self, banked):
        key = 'sk-proj-' + 'A' * 48
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: call the api'}]
            + _code_run(code=f"```python\nKEY = '{key}'\nprint(1)\n```"))
        assert ok is True
        banked_code = data['recipe'][0]['generalized_functions']
        assert 'print(1)' in banked_code
        assert key not in banked_code

    def test_a_retry_banks_the_code_that_ran_clean_not_the_failed_attempt(
            self, banked):
        """Review of the CR3 fix: the Assistant's first block exits 1, its
        corrected block exits 0.  Only the corrected block did the work."""
        first = "```python\nimport hashlib\nprint(hashlib.md5(b'x'))\n```"
        fixed = "```python\nimport hashlib\nprint(hashlib.sha256(b'x'))\n```"
        ok, data, _ = banked(
            [{'content': 'Execute Action 2: compute a hash'}]
            + _code_run(code=first, exitcode=1)
            + _code_run(code=fixed, exitcode=0))
        assert ok is True
        code = [s['generalized_functions'] for s in data['recipe']]
        assert len(code) == 1, data['recipe']
        assert 'sha256' in code[0] and 'md5' not in code[0], code


class TestTraceBankingIsNotBoundedByLastNMessages:
    """Review of ff929cd08, probed with the real autogen Executor
    (last_n_messages=2 plus CREATE's transform chain): ToolMessageHandler
    merges consecutive user turns, so the block in the dispatch, three group
    messages back, DID run.  Bounding the scan by group-message count banked
    a false no-op for it."""

    def test_a_block_three_group_messages_back_that_ran_is_banked(self, banked):
        ok, data, _ = banked([
            {'content': 'Execute Action 2: compute\n```python\nprint(42)\n```'},
            {'content': 'Working on it.', 'name': 'Assistant'},
            {'content': 'Still thinking.', 'name': 'Assistant'},
            {'content': 'exitcode: 0 (execution succeeded)\nCode output: \n42\n',
             'role': 'user', 'name': 'Executor'},
        ])
        assert ok is True
        assert 'print(42)' in data['recipe'][0]['generalized_functions'], data
