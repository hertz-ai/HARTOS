"""A completion needs the action's own work, named by the verdict for it.

LIVE, installed build, 2026-09-27 (task #125):

CREATE daemon_255bd83f, agent 28345960934 -- actions 1 and 2, both
"execute_coding_task: ...", went COMPLETED at 14:26:20 and 14:27:42 with ZERO
execute_coding_task runs.  The only tool traffic was bookkeeping:
save_data_in_memory writes of {"status": "completed", ...} the model composed
itself.  The write that certified them was

    [TARGET] Action 1: status_verification_requested -> completed
             (auto-path: hook tracking lifecycle_hook_track_termination)

-- the TERMINATE that follows every verdict, walked through COMPLETED by
force_state_through_valid_path at the top of the next create-loop lap, before
the verdict pickup and its receipt check ever ran (the action was still
IN_PROGRESS, so that check answered 'allow').  Both were then TRACE-BANKED as
28345960934_0_1/_0_2: recipes of request_tools, get_saved_metadata,
search_long_term_memory, save_data_in_memory, save_to_long_term_memory.

REUSE daemon_goal_..._b18bba6f, 17:35:28 -- a StatusVerifier verdict for
action_id 2 ("System health check completed successfully") completed action 4.

And the canonical receipt check (_verifier_completion_has_conversation_evidence)
accepted any non-empty role='tool' message: a refusal ("Not able to perform
this action now ...", what an 'incomplete' computer-use run returns) or a
note saved to memory was a receipt.

These tests call the real lifecycle_hooks functions and the real banker.
"""
import json
import os
import sys
import unittest
from types import SimpleNamespace

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.constants import TOOL_FAILURE_RESULTS  # noqa: E402
from hartos import lifecycle_hooks as lh  # noqa: E402
from tests.unit.test_trace_action_banking import banked  # noqa: E402,F401

S = lh.ActionState
_CODING = 'execute_coding_task: add client state check and retry loop'
_DISPATCH = {'role': 'user', 'name': 'ChatInstructor',
             'content': f'Execute Action 1: {_CODING} ,Latest User message: fix it'}


def _call(call_id, name, arguments='{}'):
    return {'role': 'assistant', 'name': 'Assistant', 'content': '',
            'tool_calls': [{'id': call_id, 'type': 'function',
                            'function': {'name': name, 'arguments': arguments}}]}


def _result(call_id, content):
    return {'role': 'tool', 'name': 'Assistant', 'content': content,
            'tool_responses': [{'tool_call_id': call_id, 'role': 'tool',
                                'content': content}]}


# The live action-1 bookkeeping write, verbatim.
_SAVED_COMPLETED = (
    'Saved at current_action.status: {"status": "completed", "action": '
    '"add_client_state_check_and_retry_loop", "message": "Client state check '
    'and retry loop with client refresh successfully implemented in '
    'generate_reply function"}')
_CODING_RESULT = json.dumps({'success': True, 'tool': 'aider_native',
                             'output': 'Applied edit to llm_client.py'})


class _Ledger:
    """The flow's ledger: one ``action_<id>`` task per action, as
    add_actions_to_ledger builds it for CREATE and REUSE alike."""

    def __init__(self, description, actions=1):
        self.tasks = {f'action_{n}': SimpleNamespace(
            context={}, description=description if n == 1 else f'step {n}')
            for n in range(1, actions + 1)}

    def save(self):
        return True


class _Harness(unittest.TestCase):
    """Real state machine; the registries and the learning sinks faked."""

    # No '_': a session key without one resolves to no owner, so the ledger
    # registry's on-miss loader creates nothing on disk for it.
    UP = 'realworksession'

    def setUp(self):
        self._patches = []
        self.gc = SimpleNamespace(messages=[], agents=[])
        self.ledger = _Ledger(_CODING)

        def patch(obj, name, value):
            self._patches.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        self.patch = patch
        patch(lh, 'get_registered_groupchat', lambda _up: self.gc)
        patch(lh, 'get_registered_ledger', lambda _up: self.ledger)
        patch(lh, '_promote_verified_outcome', lambda *a, **k: None)
        lh.action_states.pop(self.UP, None)
        lh.retry_tracker.reset_count(self.UP, 1)

    def tearDown(self):
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)
        lh.action_states.pop(self.UP, None)
        lh.retry_tracker.reset_count(self.UP, 1)

    def _state(self, *path):
        for st in path:
            lh.set_action_state(self.UP, 1, st, 'test')

    def _log(self, *msgs):
        self.gc.messages = [_DISPATCH, *msgs]


class TestReceiptMustBeTheActionsWork(_Harness):
    def _commit(self, index=2, **kw):
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)
        return lh.commit_verified_action_completion(
            self.UP, 1, {'message_index': index, 'kind': 'tool_receipt'}, **kw)

    def test_a_note_saved_to_memory_is_not_a_receipt(self):
        """The live action-1 receipt."""
        self._log(_call('s', 'save_data_in_memory'), _result('s', _SAVED_COMPLETED))
        self.assertFalse(self._commit())
        self.assertEqual(lh.get_action_state(self.UP, 1),
                         S.STATUS_VERIFICATION_REQUESTED)

    def test_a_failed_call_is_not_a_receipt(self):
        """An 'incomplete' computer-use run returns TOOL_FAILURE_RESULTS."""
        refusal = TOOL_FAILURE_RESULTS[0] + '\nThe loop stopped: max_iterations'
        self._log(_call('v', 'execute_windows_or_android_command'),
                  _result('v', refusal))
        self.ledger = _Ledger('execute_windows_or_android_command: open it')
        self.assertFalse(self._commit())

    def test_the_actions_own_tool_result_is_a_receipt(self):
        """Control: real work still completes."""
        self._log(_call('c', 'execute_coding_task'), _result('c', _CODING_RESULT))
        self.assertTrue(self._commit())
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_bookkeeping_the_action_names_is_its_work(self):
        """'save the preference in memory' is done by save_data_in_memory."""
        self.ledger = _Ledger('save_data_in_memory: remember the colour')
        self._log(_call('s', 'save_data_in_memory'),
                  _result('s', 'Saved at user.colour: "teal"'))
        self.assertTrue(self._commit())

    def test_one_real_result_in_an_aggregate_message_is_enough(self):
        both = {'role': 'tool', 'name': 'Assistant', 'content': '...',
                'tool_responses': [
                    {'tool_call_id': 's', 'role': 'tool', 'content': _SAVED_COMPLETED},
                    {'tool_call_id': 'c', 'role': 'tool', 'content': _CODING_RESULT}]}
        call = {'role': 'assistant', 'name': 'Assistant', 'content': '',
                'tool_calls': [
                    {'id': 's', 'function': {'name': 'save_data_in_memory'}},
                    {'id': 'c', 'function': {'name': 'execute_coding_task'}}]}
        self._log(call, both)
        self.assertTrue(self._commit())


class TestAVerdictCompletesOnlyTheActionItNames(_Harness):
    """A verdict that names ANOTHER ACTION OF THE FLOW completes nothing.
    REUSE 17:35:28 ran a flow of four, so the flow here has the other
    actions a verdict can name."""

    def setUp(self):
        super().setUp()
        self.ledger = _Ledger(_CODING, actions=4)
        self._log(_call('c', 'execute_coding_task'), _result('c', _CODING_RESULT))
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)

    def _commit(self, claimed):
        return lh.commit_verified_action_completion(
            self.UP, 1, {'message_index': 2, 'kind': 'tool_receipt'},
            claimed_action_id=claimed)

    def test_a_verdict_naming_another_action_completes_nothing(self):
        """REUSE 17:35:28: action_id 2 completed action 4."""
        self.assertFalse(self._commit(2))
        self.assertEqual(lh.get_action_state(self.UP, 1),
                         S.STATUS_VERIFICATION_REQUESTED)

    def test_a_verdict_naming_this_action_completes_it(self):
        self.assertTrue(self._commit('1'))

    def test_a_verdict_without_an_action_id_is_not_contradicting(self):
        self.assertTrue(self._commit(None))


class TestAVerdictNamingNoActionOfTheFlowIsNotContradicting(_Harness):
    """LIVE, installed desktop, 2026-10-06: agent 54 ("Personalised
    Learning") has ONE action, and the turn traced at 03:07:55 (learner
    777081) went

        Refusing completion of action 1 in 777081_54: the verdict names action 2
        [REUSE] Cannot advance action 1: state is status_verification_requested
        [SYNTHESIS] ... unfinished=['action 1 did not finish']

    and the learner got a poor reply 55-67 s later.  The 4B verifier numbers
    its verdicts as it likes: 18 such refusal lines in the two retained logs,
    the ids 2, 4 and 6, four learners, every one of them agent 54.

    The refusal is the rule above: a verdict completes only the action it
    names, because a stale verdict for action 2 completed action 4.  That is
    a verdict about ANOTHER ACTION OF THE FLOW.  An id the flow does not have
    names no action, so it cannot be a stale verdict for one; what still has
    to hold is the receipt in the posted action's own dispatch window.
    """

    def _flow(self, actions):
        """A fresh action 1 awaiting its verdict, in a flow of ``actions``."""
        lh.action_states.pop(self.UP, None)
        lh.retry_tracker.reset_count(self.UP, 1)
        self.ledger = _Ledger(_CODING, actions=actions)
        self._log(_call('c', 'execute_coding_task'),
                  _result('c', _CODING_RESULT))
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)

    def _commit(self, claimed):
        return lh.commit_verified_action_completion(
            self.UP, 1, {'message_index': 2, 'kind': 'tool_receipt'},
            claimed_action_id=claimed)

    def test_the_ids_the_live_verifier_wrote_complete_a_flow_of_one(self):
        for claimed in (2, 4, 6):
            with self.subTest(claimed=claimed):
                self._flow(1)
                self.assertTrue(self._commit(claimed))
                self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_any_spelling_of_an_id_the_flow_lacks_is_the_same(self):
        for claimed in ('2', 2.0, '2.0', 24, 0, -1):
            with self.subTest(claimed=claimed):
                self._flow(1)
                self.assertTrue(self._commit(claimed))

    def test_a_flow_that_has_the_named_action_still_refuses_it(self):
        self._flow(2)
        self.assertFalse(self._commit(2))
        self.assertEqual(lh.get_action_state(self.UP, 1),
                         S.STATUS_VERIFICATION_REQUESTED)

    def test_the_same_flow_accepts_an_id_it_does_not_have(self):
        self._flow(2)
        self.assertTrue(self._commit(3))

    def test_the_receipt_is_still_required(self):
        """The id check relaxes nothing else: a note saved to memory is no
        receipt, whatever number the verdict carries."""
        self._flow(1)
        self._log(_call('s', 'save_data_in_memory'),
                  _result('s', _SAVED_COMPLETED))
        self.assertFalse(self._commit(2))
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_no_ledger_completes_nothing(self):
        """Boundary: with no ledger to record the proof in, no verdict
        completes an action, whatever it names."""
        self._flow(1)
        self.ledger = None
        for claimed in (None, 1, 2):
            with self.subTest(claimed=claimed):
                self.assertFalse(self._commit(claimed))
                self.assertNotEqual(lh.get_action_state(self.UP, 1),
                                    S.COMPLETED)


class TestTheVerdictPickupJudgesAnInProgressAction(_Harness):
    """CREATE routes the Assistant's turn to the verifier without moving the
    state, so the verdict arrives for an IN_PROGRESS action."""

    def _verdict(self, action_id=1, index=2):
        return {'status': 'completed', 'action_id': action_id,
                'evidence': {'message_index': index, 'kind': 'tool_receipt'}}

    def _hook(self, verdict):
        return lh.lifecycle_hook_process_verifier_response(
            self.UP, verdict, SimpleNamespace(current_action=1))

    def test_a_grounded_verdict_completes_it(self):
        self._log(_call('c', 'execute_coding_task'), _result('c', _CODING_RESULT))
        self._state(S.IN_PROGRESS)
        self.assertEqual(self._hook(self._verdict())['action'], 'force_fallback')
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_a_verdict_naming_no_action_of_the_flow_completes_the_posted_one(self):
        """The live shape: action_id 2 on a flow of one (agent 54)."""
        self._log(_call('c', 'execute_coding_task'), _result('c', _CODING_RESULT))
        self._state(S.IN_PROGRESS)
        self.assertEqual(self._hook(self._verdict(action_id=2))['action'],
                         'force_fallback')
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_a_verdict_naming_another_action_of_the_flow_is_steered(self):
        self.ledger = _Ledger(_CODING, actions=2)
        self._log(_call('c', 'execute_coding_task'), _result('c', _CODING_RESULT))
        self._state(S.IN_PROGRESS)
        self.assertEqual(self._hook(self._verdict(action_id=2))['action'],
                         'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_the_live_bookkeeping_verdict_is_refused_and_steered(self):
        self._log(_call('s', 'save_data_in_memory'), _result('s', _SAVED_COMPLETED))
        self._state(S.IN_PROGRESS)
        result = self._hook(self._verdict())
        self.assertEqual(result['action'], 'force_completion')
        self.assertIn('action_id 1', result['message'])
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_repeated_ungrounded_claims_end_gave_up_not_completed(self):
        self._log(_call('s', 'save_data_in_memory'), _result('s', _SAVED_COMPLETED))
        self._state(S.IN_PROGRESS)
        actions = [self._hook(self._verdict())['action'] for _ in range(4)]
        self.assertEqual(actions, ['force_completion'] * 3 + ['gave_up'])
        self.assertEqual(lh.get_action_state(self.UP, 1), S.GAVE_UP)


class TestTerminateNeverCertifies(_Harness):
    """The fabricating writer: auto-path: hook tracking
    lifecycle_hook_track_termination."""

    def _terminate(self):
        self.gc.messages = [_DISPATCH, {'role': 'user', 'name': 'StatusVerifier',
                                        'content': '{"status": "completed"}'},
                            {'role': 'user', 'name': 'ChatInstructor',
                             'content': 'TERMINATE'}]
        return lh.lifecycle_hook_track_termination(
            self.UP, SimpleNamespace(current_action=1), self.gc)

    def test_an_unverified_action_is_left_open(self):
        self._state(S.IN_PROGRESS)
        self.assertFalse(self._terminate())
        self.assertEqual(lh.get_action_state(self.UP, 1), S.IN_PROGRESS)

    def test_a_verified_action_still_terminates(self):
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED, S.COMPLETED)
        self.assertTrue(self._terminate())
        self.assertEqual(lh.get_action_state(self.UP, 1), S.TERMINATED)


class TestTheBankerBanksWorkNotNotes:
    # The live 28345960934_0_1 trace, as tool calls.
    _LIVE_NOTES = [
        ('request_tools', '{"need": "client state check and retry loop"}'),
        ('get_saved_metadata', '{}'),
        ('search_long_term_memory', '{"query": "generate_reply"}'),
        ('save_data_in_memory', '{"key": "current_action.status", "value": '
                                '{"status": "completed"}}'),
        ('save_to_long_term_memory', '{"content": "implemented", "speaker": "System"}'),
    ]

    def _trace(self, calls, action='Execute Action 2: compute'):
        msgs = [{'content': action}]
        for i, (name, args) in enumerate(calls):
            msgs.append({'content': '', 'tool_calls': [{'id': f'c{i}', 'function': {
                'name': name, 'arguments': args}}]})
            msgs.append({'content': 'ok', 'role': 'tool', 'tool_responses': [
                {'tool_call_id': f'c{i}', 'role': 'tool', 'content': 'ok'}]})
        return msgs

    def test_a_window_of_only_notes_is_not_banked(self, banked):
        ok, data, _ = banked(self._trace(self._LIVE_NOTES))
        assert ok is False and data is None

    def test_real_work_among_the_notes_is_banked(self, banked):
        ok, data, _ = banked(self._trace(
            self._LIVE_NOTES + [('execute_coding_task', '{"task": "add retry"}')]))
        assert ok is True
        assert 'execute_coding_task' in [s['tool_name'] for s in data['recipe']]

    def test_a_note_the_action_names_is_its_work(self, banked):
        """The fixture's action text is 'synthesize weekly recap'; one that
        names the tool banks it."""
        from tests.unit import test_trace_action_banking as tab
        saved = tab._FakeTask.get_action
        tab._FakeTask.get_action = lambda self, i: {
            'action': 'save_data_in_memory: remember the recap', 'fallback_action': ''}
        try:
            ok, data, _ = banked(self._trace([('save_data_in_memory', '{}')]))
        finally:
            tab._FakeTask.get_action = saved
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == ['save_data_in_memory']
