"""An action that names a tool is done by that tool's work.

LIVE, installed build: prompts/26251890627_0_recipe.json, the recipe the coding
daemon replays for self-heal goal cff5c272 (cosyvoice3 probe failure), was
banked 2026-09-15 as status "completed" with every action "done".  Its plan is
five "execute_coding_task: read <file>" actions; every banked step is a
send_message_to_user saying "Please wait while I read the file" -- nothing was
read.  Replayed 2026-10-05 16:08 IST: execute_coding_task failed honestly and
the agent asked the absent user to paste the log.

Reproduced on the real completion gate (scratchpad repro_147.py, 2026-10-05):
for the action "execute_coding_task: read error log file ...", both a
send_message_to_user reply ("Message sent successfully ...") and the
Assistant's promise "I will read the error log file and share what I find"
were accepted as the action's receipt.  And CREATE's trace banker banks a
window of messages as the recipe of an action that names a coding tool.

The REUSE fabrication gate already holds the rule: an action that names a
registered tool is unrun until that tool returns a real result
(reuse_recipe._reuse_fabricated_tools).  The completion gate and the trace
banker now ask the same question by the same derivation
(lifecycle_hooks.action_named_tools), so the three cannot disagree.
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import lifecycle_hooks as lh  # noqa: E402
from tests.unit.test_completion_needs_real_work import (  # noqa: E402
    _CODING_RESULT, _Harness, _Ledger, _call, _result)
from tests.unit.test_trace_action_banking import banked  # noqa: E402,F401

S = lh.ActionState

# The live action text and the live steps, verbatim from the banked recipe.
_LIVE_ACTION = ('execute_coding_task: read error log file C:\\Users\\sathi\\Documents'
                '\\Nunba\\logs\\tts_cosyvoice3.err to get full traceback')
_SENT = 'Message sent successfully to user with request_id: daemon_cff5c272'
_PROMISE = 'I will read the error log file and share what I find.'


def _agent(*tools):
    """A group-chat participant that serves ``tools`` (its function map)."""
    return SimpleNamespace(name='Helper', llm_config=None,
                           _function_map={t: (lambda **_kw: None) for t in tools})


_SERVED = ('execute_coding_task', 'send_message_to_user',
           'execute_windows_or_android_command', 'save_data_in_memory')


class TestTheCompletionGate(_Harness):
    def setUp(self):
        super().setUp()
        self.gc.agents = [_agent(*_SERVED)]
        self.ledger = _Ledger(_LIVE_ACTION)

    def _commit(self, index, kind='tool_receipt'):
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)
        return lh.commit_verified_action_completion(
            self.UP, 1, {'message_index': index, 'kind': kind})

    def test_a_message_to_the_person_is_not_the_coding_work(self):
        self._log(_call('m', 'send_message_to_user',
                        '{"text": "Please wait while I read the file!"}'),
                  _result('m', _SENT))
        self.assertFalse(self._commit(2))
        self.assertEqual(lh.get_action_state(self.UP, 1),
                         S.STATUS_VERIFICATION_REQUESTED)

    def test_a_promise_is_not_the_coding_work(self):
        self._log({'role': 'assistant', 'name': 'Assistant', 'content': _PROMISE})
        self.assertFalse(self._commit(1, kind='user_visible_result'))

    def test_the_named_tools_own_result_completes_it(self):
        """Control: the work the action names still completes it."""
        self._log(_call('c', 'execute_coding_task'), _result('c', _CODING_RESULT))
        self.assertTrue(self._commit(2))
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_a_written_answer_completes_an_action_that_names_no_tool(self):
        """Unchanged: a prose action is done by its answer."""
        self.ledger = _Ledger('Summarize the findings for the person')
        self._log({'role': 'assistant', 'name': 'Assistant',
                   'content': 'The probe failed because the worker timed out.'})
        self.assertTrue(self._commit(1, kind='user_visible_result'))

    def test_a_message_completes_an_action_that_names_messaging(self):
        self.ledger = _Ledger('send_message_to_user: tell the owner the backup finished')
        self._log(_call('m', 'send_message_to_user'), _result('m', _SENT))
        self.assertTrue(self._commit(2))

    def test_a_result_no_call_names_is_not_the_named_tools_work(self):
        """A tool result whose call id resolves to no proposed call cannot be
        shown to be execute_coding_task's work, the way the REUSE gate counts
        only a result it resolves to a tool the action names."""
        self._log({'role': 'assistant', 'name': 'Assistant', 'content': 'ok'},
                  _result('orphan', _CODING_RESULT))
        self.assertFalse(self._commit(2))

    def test_a_result_no_call_names_still_completes_a_prose_action(self):
        """Unchanged: for an action that names no tool such a result is
        judged on its content alone."""
        self.ledger = _Ledger('Summarize the findings for the person')
        self._log({'role': 'assistant', 'name': 'Assistant', 'content': 'ok'},
                  _result('orphan', _CODING_RESULT))
        self.assertTrue(self._commit(2))


class TestTheTraceBanker:
    @pytest.fixture(autouse=True)
    def _live_action(self, monkeypatch):
        from tests.unit import test_trace_action_banking as tab
        monkeypatch.setattr(tab._FakeTask, 'get_action', lambda self, i: {
            'action': _LIVE_ACTION, 'fallback_action': ''})

    @staticmethod
    def _trace(calls):
        msgs = [{'content': f'Execute Action 2: {_LIVE_ACTION}'}]
        for i, (name, body) in enumerate(calls):
            msgs.append({'content': '', 'tool_calls': [{'id': f'c{i}', 'function': {
                'name': name, 'arguments': '{}'}}]})
            msgs.append({'content': body, 'role': 'tool', 'tool_responses': [
                {'tool_call_id': f'c{i}', 'role': 'tool', 'content': body}]})
        return msgs

    def test_the_live_window_of_messages_is_not_banked(self, banked):
        """The 09-15 recipe's steps: three messages, no read."""
        ok, data, _ = banked(self._trace([('send_message_to_user', _SENT)] * 3),
                             agents=[_agent(*_SERVED)])
        assert ok is False and data is None, data

    def test_the_coding_tools_run_is_banked(self, banked):
        ok, data, _ = banked(self._trace([('send_message_to_user', _SENT),
                                          ('execute_coding_task', _CODING_RESULT)]),
                             agents=[_agent(*_SERVED)])
        assert ok is True
        assert 'execute_coding_task' in [s['tool_name'] for s in data['recipe']]

    def test_no_workless_marker_for_an_action_that_names_a_tool(self, banked):
        """"no-op: action completed without tool execution" is a recipe that
        replays nothing; for an action that names a tool it is a phantom."""
        ok, data, _ = banked([{'content': f'Execute Action 2: {_LIVE_ACTION}'},
                              {'content': _PROMISE}],
                             agents=[_agent(*_SERVED)])
        assert ok is False and data is None, data


def test_reuse_and_completion_name_tools_by_one_rule():
    """The REUSE fabrication gate's derivation and the completion gate's are
    one function, so the two ends cannot disagree about what an action
    names."""
    from hartos import reuse_recipe
    agents = [_agent(*_SERVED)]
    for text in (_LIVE_ACTION, 'Summarize the findings',
                 'send_message_to_user: tell the owner'):
        assert (reuse_recipe._reuse_registered_and_referenced_tools(agents, text)
                == lh.action_named_tools(agents, text)), text
    assert lh.action_named_tools(agents, _LIVE_ACTION)[1] == ['execute_coding_task']
