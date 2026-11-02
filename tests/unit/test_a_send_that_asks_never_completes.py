"""A message that asks the person something never completes a step: the
step waits for their answer, and the answer is its receipt.

The owner, on 476bbd5d8 (31 Oct): "when the output is a question to get the
job done, how can that be counted as that step is done?"  and "the output
should be in the completed state json format".  LIVE, installed desktop,
2026-10-31 17:13 IST, agent 45620673143 action 3 ("Confirm user's topic
focus and preferred detail level before proceeding with guided
discovery.", banked 'yes'): its only output was a send ending "Ready to
begin?".  476bbd5d8 read every send in a no-tool step as delivery, so it
would have closed action 3 on its own question and walked into action 4
with the person's "yes" taken as action 4's answer.

What decides is the ACT, never the words: send_message_to_user takes a
required expects_reply.  One predicate, lifecycle_hooks.send_is_ask, read
by every reader of a send:

  * a send declared expects_reply=false is delivery: evidence for the
    verdict, as 476bbd5d8 had it;
  * a send declared true, or one that declared nothing (a call made before
    the argument existed), is an ask: never a receipt, whatever the
    verifier wrote; on a person's turn the step waits through the existing
    transition (mark_action_waiting_for_user, ledger BLOCKED
    input_required, ActionState PENDING), and the reply is the user_reply
    receipt (#161);
  * this run's verdict saying the step waits on the person (status
    'pending', or can_perform_without_user_input 'no') makes its send an
    ask too for the receipt readers and the under-report escape, in REUSE
    as in CREATE;
  * the drive decision for a 'yes' step (the autonomy nudge and the
    no-driver break, reuse_recipe._reuse_runs_alone) reads the declaration
    alone: 'pending' is also a step's plain "not done yet", and a declared
    progress line is not a question (review of 31ec05d14, option A);
  * the person's answer closes the questions before it
    (lifecycle_hooks.window_asked), so a step's output written after the
    answer is its output.

No new state: expects_reply selects an existing transition.

    python -m pytest tests/unit/test_a_send_that_asks_never_completes.py -q
"""
import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
from tests.unit.test_completion_needs_real_work import _call, _result  # noqa: E402
from tests.unit.test_prose_step_receipt_is_its_send import (  # noqa: E402
    _ACTION, _BothProjectors, _LESSON, _SENT, _dispatch)
from tests.unit.test_create_completion_finds_its_own_receipt import (  # noqa: E402
    _assistant)
from tests.unit.test_reuse_reply_is_evidence import (  # noqa: E402,F401
    _completed, _task, speech)
from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401  (fixture)
    PROMPT_ID, create_env)
from tests.unit.test_reuse_scheduler_stays_gated import (  # noqa: E402,F401  (fixture)
    reuse_agents)

S = lh.ActionState

# The live send, verbatim: a confirmation that ends in a question.
_LIVE_TEXT = ("Thanks for sharing your topic preference on basic algebra and "
              "your beginner level. I'll now prepare some guiding questions to "
              "help you discover the concepts yourself before revealing the "
              "solutions. This approach will make learning more engaging and "
              "memorable. Ready to begin?")
_TELLS = ("Your topic is basic algebra at a beginner level; the next step "
          "prepares guiding questions on it.")


def _send(call_id='s1', expects_reply=False, text=_TELLS, reply=_SENT):
    """A send as the model proposes it and the executor answers it.
    ``expects_reply=None`` leaves the argument out (a call made before it
    existed)."""
    args = {'text': text}
    if expects_reply is not None:
        args['expects_reply'] = expects_reply
    return [_call(call_id, 'send_message_to_user', json.dumps(args)),
            _result(call_id, reply)]


# ── the tool ─────────────────────────────────────────────────────────────

def _tool(send1):
    """The send tool as both recipe paths build it: wrapped by
    core.tool_logging (create_recipe.py and reuse_recipe.py pass it in ctx),
    whose error envelope is what the model reads for an omission."""
    from core.agent_tools import build_core_tool_closures
    from core.tool_logging import log_tool_execution
    ctx = {
        'log_tool_execution': log_tool_execution,
        'user_id': 'u1', 'prompt_id': 'p1', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': 'u1_p1',
        'request_id_list': {'u1_p1': 'r1'}, 'recent_file_id': {},
        'scheduler': MagicMock(), 'send_message_to_user1': send1,
        'retrieve_json': lambda v: v, 'strip_json_values': lambda v: v,
        'save_conversation_db': MagicMock(),
    }
    tools = {n: (d, f) for n, d, f in build_core_tool_closures(ctx)}
    return tools['send_message_to_user']


def test_a_send_that_does_not_say_whether_it_asks_sends_nothing():
    """Option (i), hartos-00: the argument is required.  An omission is a
    failed call naming the argument, and nothing reaches the person."""
    send1 = MagicMock(return_value='Message sent successfully to user with request_id: r1')
    _desc, send = _tool(send1)
    out = send(text='Ready to begin?')
    send1.assert_not_called()
    assert 'expects_reply' in str(out), out


def test_a_declared_send_is_delivered_either_way():
    for expects in (True, False):
        send1 = MagicMock(return_value='Message sent successfully to user with request_id: r1')
        _desc, send = _tool(send1)
        assert send(text='Ready to begin?', expects_reply=expects).startswith('Message sent')
        send1.assert_called_once()


def test_the_tool_teaches_the_act_not_asking_through_delivery():
    """The old description taught "You can use this if you want to ask a
    question" through the same act as delivering."""
    desc, _send_fn = _tool(MagicMock())
    assert 'expects_reply' in desc and 'ask a question' not in desc


# ── the one predicate ────────────────────────────────────────────────────

def test_the_declaration_is_read_off_the_proposing_call():
    msgs = [*_send('t', True), *_send('f', False), *_send('n', None)]
    assert lh.send_expects_reply(msgs, 't') is True
    assert lh.send_expects_reply(msgs, 'f') is False
    assert lh.send_expects_reply(msgs, 'n') is None
    assert lh.send_expects_reply(msgs, 'missing') is None
    assert lh.send_is_ask(msgs, 't') and lh.send_is_ask(msgs, 'n')
    assert not lh.send_is_ask(msgs, 'f')


def test_a_string_declaration_is_read_as_its_value():
    """Models send JSON strings for booleans; 'false' is false."""
    msgs = [_call('s', 'send_message_to_user',
                  json.dumps({'text': 'x', 'expects_reply': 'false'})),
            _result('s', _SENT)]
    assert lh.send_expects_reply(msgs, 's') is False


def test_a_verdict_that_waits_makes_any_send_an_ask():
    msgs = _send('f', False)
    for verdict in ({'status': 'pending'},
                    {'status': 'completed',
                     'can_perform_without_user_input': 'no - needs the topic'}):
        assert lh.verdict_waits_on_person(verdict)
        assert lh.send_is_ask(msgs, 'f', verdict)
    assert not lh.verdict_waits_on_person({'status': 'completed'})


# ── every reader: a question is never the step done ──────────────────────

class TestAQuestionIsNeverTheReceipt(_BothProjectors):
    def test_the_live_send_as_written_is_not_the_steps_receipt(self):
        """It declared nothing (it predates the argument) and ends in a
        question: an ask, in both projectors."""
        self._set_log(_dispatch(), *_send(expects_reply=None, text=_LIVE_TEXT))
        self._assert_both(None)

    def test_a_send_declared_as_asking_is_not_the_steps_receipt(self):
        self._set_log(_dispatch(), *_send(expects_reply=True, text=_LIVE_TEXT))
        self._assert_both(None)

    def test_a_send_declared_as_telling_is_the_steps_receipt(self):
        self._set_log(_dispatch(), *_send(expects_reply=False))
        self._assert_both({'message_index': 2, 'kind': 'tool_receipt'})

    def test_a_completed_verdict_citing_a_question_is_refused(self):
        """Whatever the verifier wrote: the gate refuses an ask-send cited
        as the receipt, and tells the verifier what the honest verdict is."""
        self._set_log(_dispatch(), *_send(expects_reply=True, text=_LIVE_TEXT))
        self._state(S.IN_PROGRESS)
        result = lh.lifecycle_hook_process_verifier_response(
            self.UP, {'status': 'completed', 'action_id': 1,
                      'evidence': {'message_index': 2, 'kind': 'tool_receipt'}},
            SimpleNamespace(current_action=1))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        self.assertIn('pending', result['message'])

    def test_a_step_that_names_the_send_is_not_done_by_its_question(self):
        named = 'send_message_to_user: ask the learner which topic they want'
        self._flow(named)
        self._set_log(_dispatch(1, named), *_send(expects_reply=True,
                                                   text='Which topic today?'))
        self._assert_both(None)

    def test_a_step_that_names_the_send_is_done_by_its_delivery(self):
        named = 'send_message_to_user: tell the learner today\'s topic'
        self._flow(named)
        self._set_log(_dispatch(1, named), *_send(expects_reply=False))
        self._assert_both({'message_index': 2, 'kind': 'tool_receipt'})

    def test_reuse_reads_this_runs_verdict_too(self):
        """The banked label is CREATE's guess; this run's verifier saying the
        step waits makes its delivery-declared send an ask."""
        self._set_log(_dispatch(), *_send(expects_reply=False),
                      {'role': 'user', 'name': 'StatusVerifier',
                       'content': json.dumps({'status': 'pending', 'action_id': 1})})
        self.assertIsNone(rr._reuse_completion_evidence(self.UP, 1, self.gc))

    def test_a_step_banked_yes_that_asked_waits_for_this_run_only(self):
        """REUSE: a step banked 'yes' whose window holds a send that asks is
        an ask step for this run; its banked flag is not rewritten.  A
        declared delivery alone is not an ask; a step that names another
        tool keeps needing that tool's receipt."""
        self._set_log(_dispatch(), *_send(expects_reply=False))
        self.assertFalse(rr._reuse_ask_only_step(self.UP, 1, self.gc))
        self._set_log(_dispatch(), *_send(expects_reply=True, text=_LIVE_TEXT))
        self.assertTrue(rr._reuse_ask_only_step(self.UP, 1, self.gc))
        self._set_log(_dispatch(), *_send(expects_reply=False),
                      {'role': 'user', 'name': 'StatusVerifier',
                       'content': json.dumps({'status': 'pending', 'action_id': 1})})
        self.assertTrue(rr._reuse_ask_only_step(self.UP, 1, self.gc))
        # 'pending' with no send is the under-report, not an ask.
        self._set_log(_dispatch(), {'role': 'user', 'name': 'StatusVerifier',
                                    'content': json.dumps({'status': 'pending',
                                                           'action_id': 1})})
        self.assertFalse(rr._reuse_ask_only_step(self.UP, 1, self.gc))
        self.assertEqual(
            rr.recipes[self.UP]['actions'][0]['can_perform_without_user_input'], 'yes')
        named = 'google_search: find one starter exercise for the topic'
        self._flow(named)
        self._set_log(_dispatch(1, named), *_send(expects_reply=True, text=_LIVE_TEXT))
        self.assertFalse(rr._reuse_ask_only_step(self.UP, 1, self.gc))

    def test_the_ask_a_yes_step_records_is_its_question_not_its_delivery(self):
        """It asked, then told: its ask is the question, with the text the
        person read.  A step flagged 'no' takes its latest send, as before."""
        log = [_dispatch(), *_send('ask', True, 'Which topic today?'),
               *_send('tell', False)]
        self.assertEqual(
            rr._reuse_ask_index(self.gc, log, 0, written_counts=False, step_waits=False),
            (2, 'send', 'Which topic today?'))
        self.assertEqual(
            rr._reuse_ask_index(self.gc, log, 0, written_counts=False, step_waits=True),
            (4, 'send', _TELLS))
        self.assertIsNone(rr._reuse_ask_index(
            self.gc, [_dispatch(), *_send('tell', False)], 0,
            written_counts=False, step_waits=False))

    def test_a_line_written_after_a_question_is_not_the_steps_output(self):
        """hartos-00's review of 7043d635e, finding 3, in both projectors: a
        step that names no tool sent a question, then the model wrote a line
        that reads as an answer.  The question is out and unanswered, so
        nothing in the window is the step's output; the answer will be."""
        self._set_log(_dispatch(), *_send(expects_reply=True, text=_LIVE_TEXT),
                      _assistant(_LESSON))
        self._assert_both(None)

    def test_create_finds_the_output_written_after_the_persons_answer(self):
        """Review of 31ec05d14, finding 3 (its probe P3): CREATE posts the
        person's words with no dispatch.  The step asked, the person
        answered, then the model wrote the lesson: the lesson (message 4) is
        the step's output, as on the parent.  window_asked never stopped at
        the answer, so the question stayed open and the step lost its
        output."""
        for seat in ('UserProxy', 'User'):
            with self.subTest(seat=seat):
                reply = {'role': 'user', 'name': seat,
                         'content': 'basic algebra, beginner level'}
                self._set_log(_dispatch(), *_send(expects_reply=True, text=_LIVE_TEXT),
                              reply, _assistant(_LESSON))
                self.assertFalse(lh.window_asked(self.gc.messages, 1))
                self.assertEqual(lh.derive_completion_evidence(self.UP, 1),
                                 {'message_index': 4, 'kind': 'user_visible_result'})

    def test_a_question_sent_after_the_answer_is_open_again(self):
        """The answer closes only the questions before it."""
        reply = {'role': 'user', 'name': 'UserProxy', 'content': 'basic algebra'}
        self._set_log(_dispatch(), *_send('s1', True, 'Which topic today?'), reply,
                      *_send('s2', True, _LIVE_TEXT), _assistant(_LESSON))
        self.assertTrue(lh.window_asked(self.gc.messages, 1))
        self.assertIsNone(lh.derive_completion_evidence(self.UP, 1))

    def test_another_seats_line_after_the_question_is_not_an_answer(self):
        """In a native group log every seat's text is role 'user'.  Only the
        person's own seat answers; a steer, a verdict, a timer proxy or the
        model's own line does not."""
        for seat in ('ChatInstructor', 'StatusVerifier', 'Helper', 'Executor',
                     'Assistant', 'multi_role_agent', 'user_proxy_u1'):
            with self.subTest(seat=seat):
                log = [_dispatch(), *_send(expects_reply=True, text=_LIVE_TEXT),
                       {'role': 'user', 'name': seat, 'content': 'Please continue.'},
                       _assistant(_LESSON)]
                self.assertTrue(lh.window_asked(log, 1))

    def test_a_reuse_turns_seed_after_the_question_reads_as_before(self):
        """REUSE carries the person's words inside the turn's dispatch
        (_reuse_seed_message builds it), and a dispatch never closes a
        question, so every REUSE window reads as it did before this change:
        the seed after the ask leaves the window asked."""
        seed = {'role': 'user', 'name': 'User',
                'content': rr._build_reuse_action_message(
                    self.UP, 1, user_words='yes, ready')}
        self.assertEqual(lh.dispatch_action_id(seed['content']), 1)
        log = [_dispatch(), *_send(expects_reply=True, text=_LIVE_TEXT), seed,
               _assistant(_LESSON)]
        self.assertTrue(lh.window_asked(log, 1))

    def test_a_question_beside_a_search_in_one_result_is_not_the_output(self):
        """hartos-00's review of 7043d635e, finding 2: one aggregate tool
        message carrying a send that asks and a google_search result, for a
        step that names no tool.  Neither is the step's output: no receipt in
        either projector, and user_output_receipt finds none on its own (the
        mutant that let it take the ask-send was not equivalent)."""
        calls = {'role': 'assistant', 'name': 'Assistant', 'content': '',
                 'tool_calls': [
                     {'id': 's1', 'type': 'function', 'function': {
                         'name': 'send_message_to_user',
                         'arguments': json.dumps({'text': _LIVE_TEXT,
                                                  'expects_reply': True})}},
                     {'id': 'g1', 'type': 'function', 'function': {
                         'name': 'google_search', 'arguments': '{}'}}]}
        results = {'role': 'tool', 'name': 'Assistant', 'content': _SENT,
                   'tool_responses': [
                       {'tool_call_id': 's1', 'role': 'tool', 'content': _SENT},
                       {'tool_call_id': 'g1', 'role': 'tool',
                        'content': 'Algebra basics for beginners (3 sources)'}]}
        self._set_log(_dispatch(), calls, results)
        self._assert_both(None)
        self.assertIsNone(lh.user_output_receipt(self.UP, 1, self.gc))


# ── the live shape through the real REUSE walk ───────────────────────────

_FLOW = [
    {'action': _ACTION, 'action_id': 1, 'recipe': [],
     'can_perform_without_user_input': 'yes'},
    {'action': 'Write one encouraging line about the chosen topic.',
     'action_id': 2, 'recipe': [], 'can_perform_without_user_input': 'yes'},
]


def _register_send(run, declare, resend=False):
    """The executor's send, with the real tool's signature: ``declare`` is
    what the model passes for expects_reply (None: it leaves it out).
    ``resend``: the model sends its question again every time it is steered
    on the asking turn (hartos-00's re-sending model)."""
    executor = next(a for a in run.gc.agents if a.name == 'Executor')

    def send_message_to_user(text: str, expects_reply: bool) -> str:
        run.state['sends'].append(text)
        return 'Message sent successfully to user with request_id: r'

    executor.register_for_execution(name='send_message_to_user')(send_message_to_user)

    def assistant(state, aid, content):
        if aid != 1:
            return 'Basic algebra is a great place to start.'
        if state['sends'] and not (resend and state['turn'] == 'ask'):
            # Asked once: a model judges what came back, it does not ask the
            # same question again (the reply turn's window holds no send).
            return 'Great, let us begin with basic algebra.'
        args = {'text': _LIVE_TEXT}
        if declare is not None:
            args['expects_reply'] = declare
        return {'content': '', 'tool_calls': [{
            'id': f'send_{len(state["sends"])}_{len(state["steers"])}', 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': json.dumps(args)}}]}
    return assistant


def _walk(speech, declare):
    """The speech fixture's real walk with the executor's send swapped for
    one with the real signature; the assistant script is bound after the
    run exists, through a holder."""
    holder = {}
    run = speech(actions=_FLOW, verdict_script=_completed,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    holder['fn'] = _register_send(run, declare)
    return run


def test_a_question_holds_the_step_and_the_answer_completes_it(speech):
    """A person's turn: the step that asked waits through the existing
    transition (ledger BLOCKED input_required), and the person's next
    message completes it with user_reply evidence; action 2 runs after."""
    from agent_ledger.core import TaskStatus
    run = _walk(speech, True)
    run.turn('basic algebra, beginner level', 'request-1')
    assert _task(run, 1).status == TaskStatus.BLOCKED, _task(run, 1).status
    assert _task(run, 1).blocked_reason == 'input_required'
    assert run.state['sends'] == [_LIVE_TEXT]
    ask = run.L.ledger_open_ask(run.key, 1, 0)
    assert ask is not None and ask['via'] == 'send' and ask['question'] == _LIVE_TEXT, ask
    run.turn('yes, ready', 'request-2', kind='reply')
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    answers = _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY) or []
    assert answers and answers[-1]['answer'] == 'yes, ready'


def _pending_for_step_1(state, aid, content):
    if aid == 1:
        return json.dumps({'status': 'pending', 'action_id': 1})
    return json.dumps({'status': 'completed', 'action_id': aid})


def test_the_under_report_escape_never_completes_a_question(speech):
    """hartos-00's review of 476bbd5d8: under every-'pending' verdicts the
    under-report escape advanced a 'yes' step on its question send's result.
    A window holding a send that asks is not an under-report: the step holds
    for the answer and is never completed on its question."""
    from agent_ledger.core import TaskStatus
    holder = {}
    run = speech(actions=_FLOW, verdict_script=_pending_for_step_1,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    holder['fn'] = _register_send(run, True)
    run.turn('basic algebra, beginner level', 'request-1')
    assert _LIVE_TEXT in run.state['sends']
    assert _task(run, 1).status == TaskStatus.BLOCKED, _task(run, 1).status
    assert _task(run, 1).blocked_reason == 'input_required'
    assert run.rr.user_tasks[run.key].current_action == 1


_READ_THEN_ASK = 'read_item 2, then ask the learner whether they are ready'


def test_the_under_report_escape_never_advances_a_step_that_asked(speech):
    """The escape's own case: a step banked 'yes' that names another tool
    (read_item), ran it, then sent a question, under every-'pending'
    verdicts.  Its tool ran, so the escape read 'pending' as an under-report
    and advanced it on read_item's result; the question was never answered.
    It is not advanced."""
    from agent_ledger.core import TaskStatus
    flow = [dict(_FLOW[0], action=_READ_THEN_ASK), _FLOW[1]]
    holder = {}
    run = speech(actions=flow, verdict_script=_pending_for_step_1,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    ask = _register_send(run, True)

    def read_then_ask(state, aid, content):
        if aid == 1 and not state['read']:
            return {'content': '', 'tool_calls': [{
                'id': 'read_1', 'type': 'function',
                'function': {'name': 'read_item', 'arguments': json.dumps({'item': 2})}}]}
        return ask(state, aid, content)
    holder['fn'] = read_then_ask
    run.turn('basic algebra, beginner level', 'request-1')
    assert run.state['read'] == [2] and _LIVE_TEXT in run.state['sends']
    assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    assert run.rr.user_tasks[run.key].current_action == 1


def test_the_same_words_declared_as_telling_complete_the_step(speech):
    """The act decides, not the words: the live text declared as delivery is
    the step's output, so the step closes on it and records no ask."""
    from agent_ledger.core import TaskStatus
    run = _walk(speech, False)
    run.turn('basic algebra, beginner level', 'request-1')
    assert run.state['sends'][:1] == [_LIVE_TEXT]
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    assert run.L.ledger_open_ask(run.key, 1, 0) is None


def test_a_model_that_never_declares_sends_nothing_and_the_turn_ends(speech):
    """The omission bound (hartos-00, note 2): a model that leaves
    expects_reply out every time sends nothing and the step ends GAVE_UP
    within the re-steer budget; the turn does not spin."""
    from agent_ledger.core import TaskStatus
    run = _walk(speech, None)
    run.turn('basic algebra, beginner level', 'request-1')
    assert run.state['sends'] == []
    assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)


# ── hartos-00's review of 7043d635e ──────────────────────────────────────

def test_a_yes_step_that_asked_holds_even_for_a_model_that_resends(speech):
    """Finding 1 (critical): under every-'pending' verdicts a step banked
    'yes' that asked kept its banked autonomy, so every round posted the
    autonomy nudge, every copy of the question counted as progress, and a
    model that re-sends when nudged sent the person the same question 25
    times.  A step whose send declared expects_reply=true is driven as a
    'no' step is: no nudge, the person hears the question once, and the
    step holds for the answer."""
    from agent_ledger.core import TaskStatus
    holder = {}
    run = speech(actions=_FLOW, verdict_script=_pending_for_step_1,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    holder['fn'] = _register_send(run, True, resend=True)
    run.turn('basic algebra, beginner level', 'request-1')
    assert run.state['sends'] == [_LIVE_TEXT]
    assert not [s for s in run.state['steers'] if rr._REUSE_AUTONOMY_NUDGE in s]
    assert _task(run, 1).status == TaskStatus.BLOCKED, _task(run, 1).status
    assert _task(run, 1).blocked_reason == 'input_required'
    assert run.rr.user_tasks[run.key].current_action == 1


def test_a_misdeclared_question_gets_the_nudge_and_never_completes(speech):
    """Review of 31ec05d14, option A (agreed by hartos-c9 and the
    reviewer): the declaration is the model's own word, so a question it
    declared expects_reply=false is driven as the delivery it declared: the
    nudge is posted, as for any 'yes' step mid-work.  It still never
    completes the step: the under-report escape keeps its own ask check
    (this run's 'pending' verdict), so the turn ends with the step
    unfinished and current.  Cost, stated rather than assumed: the send
    tool refuses an identical re-send only within one request (key =
    request id + text), so the person sees the words once per turn and a
    reworded copy would pass."""
    from agent_ledger.core import TaskStatus
    holder = {}
    run = speech(actions=_FLOW, verdict_script=_pending_for_step_1,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    holder['fn'] = _register_send(run, False, resend=True)
    run.turn('basic algebra, beginner level', 'request-1')
    assert [s for s in run.state['steers'] if rr._REUSE_AUTONOMY_NUDGE in s]
    assert run.state['sends'] and set(run.state['sends']) == {_LIVE_TEXT}
    assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    assert run.rr.user_tasks[run.key].current_action == 1


_PROGRESS = 'One moment, I am reading item 2 for you.'


def test_a_progress_line_does_not_stop_a_yes_step_running_its_tool(speech):
    """Review of 31ec05d14, finding 1 (its probe P1): a step banked 'yes'
    that names read_item sends a progress line declared expects_reply=false,
    and the verifier says 'pending' because the tool has not run.  'pending'
    is the step's plain "not done yet", not a question to the person: the
    nudge is posted, the tool runs, and the step completes.  31ec05d14 read
    the 'pending' as making the progress line a question, so the step lost
    its nudge and its tool never ran (the parent ran it)."""
    from agent_ledger.core import TaskStatus

    def script(state, aid, content):
        if aid != 1:
            return 'Basic algebra is a great place to start.'
        if not state['sends']:
            return {'content': '', 'tool_calls': [{
                'id': 'progress_1', 'type': 'function',
                'function': {'name': 'send_message_to_user',
                             'arguments': json.dumps({'text': _PROGRESS,
                                                      'expects_reply': False})}}]}
        if not state['read']:
            if rr._REUSE_AUTONOMY_NUDGE in content:
                return {'content': '', 'tool_calls': [{
                    'id': f'read_{len(state["steers"])}', 'type': 'function',
                    'function': {'name': 'read_item',
                                 'arguments': json.dumps({'item': 2})}}]}
            return 'I let the learner know I am on it.'
        return 'Item 2 holds the value 20.'

    def verdict(state, aid, content):
        if aid == 1 and not state['read']:
            return json.dumps({'status': 'pending', 'action_id': 1,
                               'message': 'read_item has not run yet'})
        return json.dumps({'status': 'completed', 'action_id': aid})

    flow = [{'action': 'read_item 2, then tell the learner its value',
             'action_id': 1, 'recipe': [], 'can_perform_without_user_input': 'yes'},
            _FLOW[1]]
    run = speech(actions=flow, verdict_script=verdict, assistant_script=script)
    run.turn('go on', 'request-1')
    assert run.state['sends'] == [_PROGRESS]
    assert [s for s in run.state['steers'] if rr._REUSE_AUTONOMY_NUDGE in s]
    assert run.state['read'] == [2], 'the step never ran its tool'
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)


def test_a_question_from_an_earlier_turn_does_not_stop_this_turns_work(speech):
    """Mutant M6 of the 31ec05d14 rework survived without this: the drive
    decision must read THIS turn's window (from its dispatch), not every
    window of the action.  A 'yes' step that names read_item asked "Which
    item should I read?" on the person's first turn and stopped there.  The
    person answered on the next turn, whose window holds no question, so
    the step runs alone again: the nudge is posted and the tool runs.
    Reading the earlier turn's question kept the step from ever running
    its tool."""
    from agent_ledger.core import TaskStatus

    def script(state, aid, content):
        if aid != 1:
            return 'Basic algebra is a great place to start.'
        if state['turn'] == 'ask':
            if not state['sends']:
                return {'content': '', 'tool_calls': [{
                    'id': 'which_1', 'type': 'function',
                    'function': {'name': 'send_message_to_user',
                                 'arguments': json.dumps({
                                     'text': 'Which item should I read?',
                                     'expects_reply': True})}}]}
            return 'I asked which item to read.'
        if not state['read']:
            if rr._REUSE_AUTONOMY_NUDGE in content:
                return {'content': '', 'tool_calls': [{
                    'id': f'read_{len(state["steers"])}', 'type': 'function',
                    'function': {'name': 'read_item',
                                 'arguments': json.dumps({'item': 2})}}]}
            return 'Thanks, I will read it.'
        return 'Item 2 holds the value 20.'

    def verdict(state, aid, content):
        if aid == 1 and not state['read']:
            return json.dumps({'status': 'pending', 'action_id': 1,
                               'message': 'read_item has not run yet'})
        return json.dumps({'status': 'completed', 'action_id': aid})

    flow = [{'action': 'read_item the item the learner names, then tell them its value',
             'action_id': 1, 'recipe': [], 'can_perform_without_user_input': 'yes'},
            _FLOW[1]]
    run = speech(actions=flow, verdict_script=verdict, assistant_script=script)
    run.turn('read me an item', 'request-1')
    assert run.state['read'] == [], 'precondition: the first turn asked and stopped'
    run.turn('item 2', 'request-2', kind='reply')
    assert [s for s in run.state['steers'] if rr._REUSE_AUTONOMY_NUDGE in s]
    assert run.state['read'] == [2], 'the step never ran its tool after the answer'
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)


def test_a_progress_line_is_not_the_question_a_prose_step_waits_on(speech):
    """Review of 31ec05d14, finding 1 (its probe P1b): a prose step banked
    'yes' sends "One moment, I am preparing your lesson." declared
    expects_reply=false, under 'pending' verdicts until the lesson exists.
    The lesson is written on that turn and the step completes only after
    it; no question is held.  31ec05d14 held the progress line as the
    step's open question, and the person's "ok thanks" then completed the
    step with no lesson ever written."""
    from agent_ledger.core import TaskStatus
    lesson = ('Algebra uses letters for numbers we do not know yet. If x + 3 = 5, '
              'x is 2, because 2 + 3 = 5.')

    def script(state, aid, content):
        if aid != 1:
            return 'Basic algebra is a great place to start.'
        if not state['sends']:
            return {'content': '', 'tool_calls': [{
                'id': 'progress_1', 'type': 'function',
                'function': {'name': 'send_message_to_user',
                             'arguments': json.dumps({
                                 'text': 'One moment, I am preparing your lesson.',
                                 'expects_reply': False})}}]}
        if rr._REUSE_AUTONOMY_NUDGE in content or state.get('lesson'):
            state['lesson'] = True
            return lesson
        return 'I let the learner know the lesson is coming.'

    def verdict(state, aid, content):
        if aid == 1 and not state.get('lesson'):
            return json.dumps({'status': 'pending', 'action_id': 1,
                               'message': 'the lesson has not been written yet'})
        return json.dumps({'status': 'completed', 'action_id': aid})

    flow = [{'action': 'Write one short lesson on basic algebra for the learner.',
             'action_id': 1, 'recipe': [], 'can_perform_without_user_input': 'yes'},
            _FLOW[1]]
    run = speech(actions=flow, verdict_script=verdict, assistant_script=script)
    run.turn('teach me algebra', 'request-1')
    assert run.state.get('lesson'), 'the lesson was never written'
    assert run.L.ledger_open_ask(run.key, 1, 0) is None
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)


def test_a_resent_question_is_not_progress_for_the_round_budget():
    """Finding 1, the measure itself: the round budget extends while there
    is progress, and _reuse_evidence_count is that measure.  Each copy of a
    question counted as a result, so a model re-sending its question kept
    the turn alive.  A send that asks adds nothing; real work and a send
    that only tells the person something still count."""
    ask = json.dumps({'text': 'Ready to begin?', 'expects_reply': True})
    tell = json.dumps({'text': 'I saved your topic.', 'expects_reply': False})
    sent = 'Message sent successfully to user with request_id: request-1'
    asks = [_call('s1', 'send_message_to_user', ask), _result('s1', sent),
            _call('s2', 'send_message_to_user', ask), _result('s2', sent)]
    group = SimpleNamespace(messages=list(asks), agents=[])
    assert rr._reuse_evidence_count(group) == 0
    group.messages = asks + [
        _call('g1', 'google_search', json.dumps({'text': 'algebra basics'})),
        _result('g1', '[{"text": "Algebra basics for beginners"}]'),
        _call('s3', 'send_message_to_user', tell), _result('s3', sent)]
    assert rr._reuse_evidence_count(group) == 2


@pytest.mark.parametrize('flag', ['yes', 'no'])
def test_a_daemon_tick_never_completes_a_step_on_the_line_after_its_question(
        speech, flag):
    """Finding 3: on a daemon tick nobody can answer, so no ask is recorded
    (the owner's 2026-10-31 ruling routes a background step's question to
    the goal's owner, parts B1-B3).  The step that asked still never
    completes on the model's next line, and a model that re-sends puts its
    question out once in the tick."""
    from agent_ledger.core import TaskStatus
    flow = [dict(_FLOW[0], can_perform_without_user_input=flag), _FLOW[1]]
    holder = {}
    run = speech(actions=flow, verdict_script=_completed,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    holder['fn'] = _register_send(run, True, resend=True)
    run.turn('tick', 'daemon_goal_turn')
    assert run.state['sends'] == [_LIVE_TEXT]
    assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    assert run.rr.user_tasks[run.key].current_action == 1
    assert run.L.ledger_open_ask(run.key, 1, 0) is None


@pytest.mark.parametrize('flag', ['yes', 'no'])
def test_a_reply_turn_whose_only_line_is_a_note_completes_on_the_answer(speech, flag):
    """Finding 5: the person answers and the model writes only a note to the
    verifier.  The answer is the step's receipt (user_reply evidence); the
    step never gives up with the answer dropped."""
    from agent_ledger.core import TaskStatus
    flow = [dict(_FLOW[0], can_perform_without_user_input=flag), _FLOW[1]]
    holder = {}

    def verdict(state, aid, content):
        status = 'pending' if (aid == 1 and state['turn'] == 'ask') else 'completed'
        return json.dumps({'status': status, 'action_id': aid})

    run = speech(actions=flow, verdict_script=verdict,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    ask = _register_send(run, True)

    def note_only(state, aid, content):
        if aid == 1 and state['turn'] == 'ask':
            return ask(state, aid, content) if not state['sends'] else (
                '@StatusVerifier I asked the question, please check.')
        if aid == 1:
            return '@StatusVerifier Action 1 is done, please verify.'
        return 'Basic algebra is a great place to start.'
    holder['fn'] = note_only
    run.turn('hi, start my first session', 'request-1')
    assert _task(run, 1).status == TaskStatus.BLOCKED, _task(run, 1).status
    run.turn('basic algebra, beginner level', 'request-2', kind='reply')
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    answers = _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY) or []
    assert answers and answers[-1]['answer'] == 'basic algebra, beginner level'


# ── finding 4: banked examples without expects_reply ─────────────────────
#
# hartos-00's ruling: no migration of banked user data and no default, on
# read or on call.  An omission stays a visible failed call; what
# expects_reply means has one definition (core.constants.EXPECTS_REPLY_RULE)
# shown beside the old examples; and every omission is counted.

def test_a_model_that_omits_the_declaration_once_then_corrects_finishes(speech):
    """The omission costs the model one call, not the step: it reads the
    failure, declares, and the delivery completes the step in the same turn
    so the next action runs."""
    from agent_ledger.core import TaskStatus
    holder = {}
    run = speech(actions=_FLOW, verdict_script=_completed,
                 assistant_script=lambda s, a, c: holder['fn'](s, a, c))
    _register_send(run, False)
    calls = []

    def omits_once(state, aid, content):
        if aid != 1:
            return 'Basic algebra is a great place to start.'
        if state['sends']:
            return 'Great, let us begin with basic algebra.'
        args = {'text': _TELLS}
        if calls:
            args['expects_reply'] = False
        calls.append(args)
        return {'content': '', 'tool_calls': [{
            'id': f'send_{len(calls)}', 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': json.dumps(args)}}]}
    holder['fn'] = omits_once
    run.turn('basic algebra, beginner level', 'request-1')
    assert len(calls) == 2, calls
    assert run.state['sends'] == [_TELLS]
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    assert 2 in run.state['dispatched']


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelname, record.getMessage()))


def _refused_lines(fn, *args, **kwargs):
    """The [TOOL-CALL-REFUSED] lines one call writes to the tool log."""
    lines = _Lines()
    log = logging.getLogger('agent_logger')
    log.addHandler(lines)
    try:
        fn(*args, **kwargs)
    finally:
        log.removeHandler(lines)
    return [(lvl, msg) for lvl, msg in lines.lines if '[TOOL-CALL-REFUSED]' in msg]


def _executor_call(fn, arguments):
    """The send called as the group's executor calls it: autogen's
    execute_function as hartos.helper.force_apply_autogen_json_fix patches it
    on every node, which refuses arguments that do not fit before the tool
    runs."""
    from autogen.agentchat.conversable_agent import ConversableAgent
    from hartos.helper import force_apply_autogen_json_fix
    orig = (ConversableAgent.execute_function, ConversableAgent.a_execute_function)
    try:
        assert force_apply_autogen_json_fix()
        agent = ConversableAgent('Executor', llm_config=False, human_input_mode='NEVER')
        agent.register_function({'send_message_to_user': agent._wrap_function(fn)})
        return agent.execute_function({'name': 'send_message_to_user',
                                       'arguments': json.dumps(arguments)})
    finally:
        ConversableAgent.execute_function, ConversableAgent.a_execute_function = orig


def test_an_omission_is_counted_by_session_and_step_without_the_message(monkeypatch):
    """(c): the executor's refusal of a call that leaves expects_reply out
    writes one WARNING naming the tool, the parameter, the session and the
    step, never the message; the after-swap check counts these per day.  A
    declared call writes none and reaches the person."""
    from hartos.threadlocal import thread_local_data
    before = (thread_local_data.get_user_id(), thread_local_data.get_prompt_id())
    thread_local_data.set_user_id('u1')
    thread_local_data.set_prompt_id('p1')
    monkeypatch.setitem(lh.action_states, 'u1_p1', {3: S.IN_PROGRESS})
    try:
        send1 = MagicMock(return_value='Message sent successfully to user with request_id: r1')
        _desc, send = _tool(send1)
        refused = _refused_lines(_executor_call, send, {'text': 'Ready to begin?'})
        assert len(refused) == 1, refused
        level, line = refused[0]
        assert level == 'WARNING'
        assert 'send_message_to_user' in line and 'expects_reply' in line, line
        assert 'action=3' in line and 'for session: u1_p1' in line, line
        assert 'Ready to begin?' not in line
        send1.assert_not_called()
        assert _refused_lines(_executor_call, send, {'text': 'Ready to begin?',
                                                     'expects_reply': True}) == []
        send1.assert_called_once()
    finally:
        thread_local_data.set_user_id(before[0])
        thread_local_data.set_prompt_id(before[1])


@pytest.fixture
def banked_positional_send(create_env):
    """Action 1's banked file, its recorded step calling the send the way the
    desktop's old examples do: positionally, without expects_reply."""
    (create_env.prompts / f'{PROMPT_ID}_0_1.json').write_text(json.dumps({
        'action': 'Confirm the learner is ready, then begin',
        'recipe': [{'steps': "send_message_to_user('Ready to begin?')",
                    'tool_name': 'send_message_to_user'}]}), encoding='utf-8')
    return create_env


def test_the_rule_beside_old_examples_is_the_tools_own_definition(
        banked_positional_send, reuse_agents):
    """(a)+(b): the REUSE Assistant built by the real create_agents_for_user
    shows the old example AND the one definition next to it, and the send
    the group is offered describes expects_reply with that same definition,
    as a required parameter."""
    from core.constants import EXPECTS_REPLY_RULE
    _env, _rr, built, _scheduler, _params = reuse_agents
    system = built[0].system_message
    assert "send_message_to_user('Ready to begin?')" in system
    assert EXPECTS_REPLY_RULE in system
    offered = [entry.get('function') or {}
               for agent in built if isinstance(getattr(agent, 'llm_config', None), dict)
               for entry in agent.llm_config.get('tools') or []
               if (entry.get('function') or {}).get('name') == 'send_message_to_user']
    assert offered, 'no agent in the built group is offered send_message_to_user'
    for fn in offered:
        params = fn['parameters']
        assert params['properties']['expects_reply']['description'] == EXPECTS_REPLY_RULE
        assert 'expects_reply' in params['required']
        assert EXPECTS_REPLY_RULE in fn['description']
