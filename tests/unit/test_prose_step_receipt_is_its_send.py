"""A step that names no tool is done by its written answer, or by the send
that delivered its output to the person, and by no other tool's result.

LIVE, installed desktop on the 4B, 2026-10-31 17:12-17:13 IST (HARTOS
b599e7f98 hot-swapped in): agent 45620673143 "Tutor-FirstSession-Setup", user
10202, turn msg_1793446973018_ex.  Its action 3, verbatim from
45620673143_0_recipe.json, is

    Confirm user's topic focus and preferred detail level before proceeding
    with guided discovery.

flagged 'yes', and its words name no tool.  The Assistant's output was one
send_message_to_user call (DPuAdrjjOQ6vI4Yo95I0jZRlrgFamxFf, 17:13:03),
answered "Message sent successfully ...", inside the step's dispatch window.
The verifier said completed and the step GAVE_UP: "[REUSE-VERIFY] ... no
canonical receipt in its dispatch window".  The receipt finder took its prose
path (no "receipt search ... found none" line: the named-tool branch never
ran), and a prose step was done by a written answer only.  Action 4 was never
reached.

The rule, from hartos-00's review on 31 Oct: lifecycle_hooks.user_output_receipt,
called by both projectors after their written-answer miss (CREATE's
derive_completion_evidence and REUSE's _reuse_completion_evidence).  It takes an
unseen, non-failed, non-placeholder send_message_to_user result in the step's
own dispatch window, and the one completion gate judges it afterwards.  No
other tool's result completes a prose step: a search result does not complete
"write X" (owner ruling 2026-09-14: never record a completion that did not
happen).  A REUSE step flagged 'no' is not done by its send either; that send
is its ask.

    python -m pytest tests/unit/test_prose_step_receipt_is_its_send.py -q
"""
import hashlib
import json
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.constants import HISTORICAL_TOOL_PLACEHOLDER  # noqa: E402
from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
# DRY (Gate 2): the gate's message shapes and harness live with its own tests;
# the real REUSE walk is the #161 fixture.
from tests.unit.test_completion_needs_real_work import (  # noqa: E402
    _Harness, _Ledger, _call, _result)
from tests.unit.test_create_completion_finds_its_own_receipt import (  # noqa: E402
    _Peer, _agent, _assistant)
from tests.unit.test_reuse_reply_is_evidence import (  # noqa: E402,F401
    _completed, _task, speech)

S = lh.ActionState

_ACTION = ("Confirm user's topic focus and preferred detail level before "
           "proceeding with guided discovery.")
# The live call's text without its closing question.  The live send ended
# "Ready to begin?": a question, which the owner ruled is never the step
# done (tests/unit/test_a_send_that_asks_never_completes.py holds it as
# written).  Every send here is a delivery, declared expects_reply=false.
_SEND_TEXT = ("Thanks for sharing your topic preference on basic algebra and "
              "your beginner level. I'll now prepare some guiding questions to "
              "help you discover the concepts yourself before revealing the "
              "solutions. This approach will make learning more engaging and "
              "memorable.")
_SENT = 'Message sent successfully to user with request_id: msg_1793446973018_ex'
_CALL_ID = 'DPuAdrjjOQ6vI4Yo95I0jZRlrgFamxFf'
_SERVED = ('get_data_by_key', 'send_message_to_user', 'google_search',
           'save_data_in_memory')
_LESSON = ('Algebra uses letters for numbers we do not know yet. If x + 3 = 5, '
           'x is 2, because 2 + 3 = 5. Check question: what is x in x + 1 = 4?')
_NAMED = 'get_data_by_key: read key tutor.progress for the topic being learned'


def _send(call_id=_CALL_ID, reply=_SENT, text=_SEND_TEXT):
    """A send_message_to_user call declared as delivery, and its result."""
    return [_call(call_id, 'send_message_to_user',
                  json.dumps({'text': text, 'expects_reply': False})),
            _result(call_id, reply)]


def _dispatch(action_id=1, words=_ACTION):
    return {'role': 'assistant', 'name': 'ChatInstructor',
            'content': f'Execute Action {action_id}: {words} ,Latest User '
                       'message: basic algebra, beginner level'}


class _BothProjectors(_Harness):
    """One log, read by CREATE's projector and by REUSE's.  The real gate and
    state machine; only the registries and the learning sinks are faked."""

    def setUp(self):
        super().setUp()
        self.gc.agents = [_agent(*_SERVED)]
        self._flow(_ACTION)

    def _flow(self, *words, flag='yes'):
        self.ledger = _Ledger(words[0], actions=len(words))
        for n, text in enumerate(words[1:], start=2):
            self.ledger.tasks[f'action_{n}'].description = text
        actions = [{'action': text, 'action_id': n, 'recipe': [],
                    'can_perform_without_user_input': flag}
                   for n, text in enumerate(words, start=1)]
        self.addCleanup(rr.user_tasks.pop, self.UP, None)
        self.addCleanup(rr.recipes.pop, self.UP, None)
        rr.user_tasks[self.UP] = rr.Action(actions)
        rr.recipes[self.UP] = {'actions': actions}

    def _set_log(self, *msgs):
        self.gc.messages = list(msgs)

    def _both(self, action_id=1):
        return (lh.derive_completion_evidence(self.UP, action_id),
                rr._reuse_completion_evidence(self.UP, action_id, self.gc))

    def _assert_both(self, expected, action_id=1):
        create, reuse = self._both(action_id)
        self.assertEqual(create, expected, 'CREATE')
        self.assertEqual(reuse, expected, 'REUSE')


class TestTheSendThatDeliveredTheStepIsItsReceipt(_BothProjectors):
    def test_the_live_send_is_the_receipt_in_both_projectors(self):
        """THE LIVE FAILURE: REUSE returned None and the step GAVE_UP."""
        self._set_log(_dispatch(), *_send())
        self._assert_both({'message_index': 2, 'kind': 'tool_receipt'})

    def test_the_create_verdict_completes_the_step_on_it(self):
        """The verdict cites a message that is no receipt; the pipeline finds
        the send and the same gate commits it."""
        self._set_log(_dispatch(), *_send())
        self._state(S.IN_PROGRESS)
        result = lh.lifecycle_hook_process_verifier_response(
            self.UP, {'status': 'completed', 'action': _ACTION[:40],
                      'action_id': 1,
                      'evidence': {'message_index': 0,
                                   'kind': 'user_visible_result'}},
            SimpleNamespace(current_action=1))
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        record = self.ledger.tasks['action_1'].context[
            'verification_evidence'][0]
        self.assertEqual(record['evidence'],
                         {'message_index': 2, 'kind': 'tool_receipt'})
        self.assertEqual(record['receipt_sha256'],
                         hashlib.sha256(_SENT.encode('utf-8')).hexdigest())

    def test_the_reuse_receipt_commits_through_the_shared_gate(self):
        self._set_log(_dispatch(), *_send())
        evidence = rr._reuse_completion_evidence(self.UP, 1, self.gc)
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)
        self.assertTrue(lh.commit_verified_action_completion(self.UP, 1, evidence))
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_a_send_held_only_in_a_buffer_is_found_and_read_back(self):
        holder = _agent(*_SERVED, name='Assistant', _oai_messages={
            _Peer('ChatInstructor'): [_dispatch(), *_send()]})
        self.gc.agents = [holder]
        self._set_log(_dispatch())
        expected = {'source': 'buffer', 'agent': 'Assistant',
                    'peer': 'ChatInstructor', 'message_index': 2,
                    'kind': 'tool_receipt'}
        self._assert_both(expected)
        self.assertEqual(lh.resolve_receipt(self.UP, expected)[2]['content'], _SENT)

    def test_the_newest_send_of_the_window_is_the_receipt(self):
        self._set_log(_dispatch(), *_send('s1'), *_send('s2'))
        self._assert_both({'message_index': 4, 'kind': 'tool_receipt'})

    def test_the_search_ends_at_the_previous_actions_work(self):
        """NFT (hartos-00's cost note): when the action's own window holds no
        send, a long session's older sends are not each read and put to the
        gate; the search of a list ends at its first result outside the
        window."""
        self._flow(_ACTION, 'Prepare the session from the topic and level.')
        older = [m for n in range(20) for m in _send(f'old{n}')]
        self._set_log(_dispatch(1), *older,
                      _dispatch(2, 'Prepare the session from the topic and level.'))
        asked, windows = [], []
        real_gate = lh._verifier_completion_has_conversation_evidence
        real_window = lh.latest_dispatch_before
        self.patch(lh, '_verifier_completion_has_conversation_evidence',
                   lambda *a, **k: asked.append(a) or real_gate(*a, **k))
        self.patch(lh, 'latest_dispatch_before',
                   lambda *a, **k: windows.append(a) or real_window(*a, **k))
        self.assertIsNone(lh.user_output_receipt(self.UP, 2, self.gc))
        self.assertEqual(asked, [])
        # Not one window read per older send: the search stops at the first.
        self.assertLess(len(windows), 5)

    def test_a_written_answer_comes_before_the_send(self):
        self._set_log(_dispatch(), _assistant(_LESSON), *_send())
        self._assert_both({'message_index': 1, 'kind': 'user_visible_result'})


class TestNoOtherResultCompletesAProseStep(_BothProjectors):
    def _refused(self, *msgs, action_id=1):
        self._set_log(*msgs)
        self._assert_both(None, action_id)

    def test_a_search_result_is_not_the_steps_receipt(self):
        """A search result does not complete "write X"."""
        self._refused(_dispatch(), _call('g1', 'google_search'),
                      _result('g1', 'Algebra basics for beginners (3 sources)'))

    def test_a_note_saved_to_memory_is_not_the_steps_receipt(self):
        self._refused(_dispatch(), _call('m1', 'save_data_in_memory'),
                      _result('m1', 'Saved at tutor.topic: basic algebra'))

    def test_a_failed_send_is_no_receipt(self):
        self._refused(_dispatch(), *_send(
            reply='Error: send_message_to_user failed: no delivery channel'))

    def test_a_placeholder_send_is_no_receipt(self):
        self._refused(_dispatch(), *_send(reply=HISTORICAL_TOOL_PLACEHOLDER))

    def test_a_bad_send_beside_another_tools_reply_is_no_receipt(self):
        """One aggregate message: the send failed (or is the placeholder) and
        a search beside it succeeded.  The gate accepts the message for the
        search; the send is what is judged here, per result."""
        for bad in ('Error: send_message_to_user failed: no delivery channel',
                    HISTORICAL_TOOL_PLACEHOLDER):
            with self.subTest(bad=bad[:20]):
                calls = {'role': 'assistant', 'name': 'Assistant', 'content': '',
                         'tool_calls': [
                             {'id': 's1', 'type': 'function', 'function': {
                                 'name': 'send_message_to_user',
                                 'arguments': json.dumps({'text': _SEND_TEXT,
                                                          'expects_reply': False})}},
                             {'id': 'g1', 'type': 'function', 'function': {
                                 'name': 'google_search', 'arguments': '{}'}}]}
                both = {'role': 'tool', 'name': 'Assistant', 'content': bad,
                        'tool_responses': [
                            {'tool_call_id': 's1', 'role': 'tool', 'content': bad},
                            {'tool_call_id': 'g1', 'role': 'tool',
                             'content': 'Algebra basics for beginners (3 sources)'}]}
                self._refused(_dispatch(), calls, both)

    def test_a_send_before_the_steps_dispatch_is_no_receipt(self):
        self._refused(*_send(), _dispatch())

    def test_the_previous_steps_send_is_not_the_next_steps(self):
        """Live action 4 must not complete on action 3's send."""
        self._flow(_ACTION, 'Prepare the session from the topic and level.')
        self._refused(_dispatch(1), *_send(),
                      _dispatch(2, 'Prepare the session from the topic and level.'),
                      action_id=2)

    def test_a_step_that_names_a_tool_is_not_done_by_a_send(self):
        self._flow(_NAMED)
        self._refused(_dispatch(1, _NAMED), *_send())
        # The rule itself, asked directly: the gate refuses the send, since
        # this step's receipt is its named tool's work.
        self.assertIsNone(lh.user_output_receipt(self.UP, 1, self.gc))

    def test_a_send_that_existed_when_the_step_was_dispatched_is_not_its_receipt(self):
        """REUSE's watermark: a call id seen at dispatch is earlier work."""
        self._set_log(_dispatch(), *_send())
        rr.user_tasks[self.UP].evidence_seen_call_ids = {_CALL_ID}
        self.assertIsNone(rr._reuse_completion_evidence(self.UP, 1, self.gc))

    def test_a_step_flagged_no_is_not_done_by_its_send(self):
        """Its send is its ask.  On a person's turn the ask branch holds it
        (#161); on any other turn it is still not the step's output."""
        self._flow(_ACTION, flag='no')
        self._set_log(_dispatch(), *_send())
        self.assertFalse(rr._reuse_person_turn(self.UP))
        self.assertIsNone(rr._reuse_completion_evidence(self.UP, 1, self.gc))

    def test_a_create_verdict_that_says_the_step_waits_is_not_done_by_its_send(self):
        """CREATE agrees (hartos-00's review): a completed verdict that also
        says the step needs the person (the 4B's #158 shape) reaches the
        receipt search with no park first, and the step's question went out
        as a send.  The send is not its completion, so a banked step is never
        "done by its send" in CREATE while REUSE reads it as an ask."""
        self._set_log(_dispatch(), *_send())
        self._state(S.IN_PROGRESS)
        result = lh.lifecycle_hook_process_verifier_response(
            self.UP, {'status': 'completed', 'action': _ACTION[:40],
                      'action_id': 1,
                      'can_perform_without_user_input': 'no - needs the '
                                                        "person's topic"},
            SimpleNamespace(current_action=1))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        self.assertEqual(self.ledger.tasks['action_1'].context.get(
            'verification_evidence', []), [])


# ── the live shape through the real REUSE walk ─────────────────────────────

_NEXT = 'Write one encouraging line about the topic the person chose.'
_LIVE_FLOW = [
    {'action': _ACTION, 'action_id': 1, 'recipe': [],
     'can_perform_without_user_input': 'yes'},
    {'action': _NEXT, 'action_id': 2, 'recipe': [],
     'can_perform_without_user_input': 'yes'},
]


def _sends_its_output(state, aid, content):
    """Step 1's output goes out as a send declared as delivery, every time
    it is asked; step 2 writes its line."""
    if aid == 1:
        return {'content': '', 'tool_calls': [{
            'id': f'send_{len(state["sends"])}', 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': json.dumps({'text': _SEND_TEXT,
                                                  'expects_reply': False})}}]}
    return 'Basic algebra is a great place to start; you will do well.'


def test_the_step_completes_on_its_send_and_the_walk_goes_on(speech):
    """Through the real chat_agent, ledger, state machine and gate: the step
    whose output went out as a send completes on that send, once, and the walk
    reaches the next step instead of giving up."""
    from agent_ledger.core import TaskStatus
    run = speech(actions=_LIVE_FLOW, assistant_script=_sends_its_output,
                 verdict_script=_completed)
    executor = next(a for a in run.gc.agents if a.name == 'Executor')

    def send_message_to_user(text: str, expects_reply: bool) -> str:
        run.state['sends'].append(text)
        return 'Message sent successfully to user with request_id: r'

    executor.register_for_execution(name='send_message_to_user')(send_message_to_user)
    run.turn('basic algebra, beginner level', 'request-1')
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED), (
        f'{_task(run, 1).status} / {_task(run, 1).blocked_reason}')
    assert run.state['sends'] == [_SEND_TEXT]
    assert _task(run, 2).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
