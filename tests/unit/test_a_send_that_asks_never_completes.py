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
    ask too, in REUSE as in CREATE.

No new state: expects_reply selects an existing transition.

    python -m pytest tests/unit/test_a_send_that_asks_never_completes.py -q
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
from tests.unit.test_completion_needs_real_work import _call, _result  # noqa: E402
from tests.unit.test_prose_step_receipt_is_its_send import (  # noqa: E402
    _ACTION, _BothProjectors, _SENT, _dispatch)
from tests.unit.test_reuse_reply_is_evidence import (  # noqa: E402,F401
    _completed, _task, speech)

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


# ── the live shape through the real REUSE walk ───────────────────────────

_FLOW = [
    {'action': _ACTION, 'action_id': 1, 'recipe': [],
     'can_perform_without_user_input': 'yes'},
    {'action': 'Write one encouraging line about the chosen topic.',
     'action_id': 2, 'recipe': [], 'can_perform_without_user_input': 'yes'},
]


def _register_send(run, declare):
    """The executor's send, with the real tool's signature: ``declare`` is
    what the model passes for expects_reply (None: it leaves it out)."""
    executor = next(a for a in run.gc.agents if a.name == 'Executor')

    def send_message_to_user(text: str, expects_reply: bool) -> str:
        run.state['sends'].append(text)
        return 'Message sent successfully to user with request_id: r'

    executor.register_for_execution(name='send_message_to_user')(send_message_to_user)

    def assistant(state, aid, content):
        if aid != 1:
            return 'Basic algebra is a great place to start.'
        if state['sends']:
            # Asked once: a model judges what came back, it does not ask the
            # same question again (the reply turn's window holds no send).
            return 'Great, let us begin with basic algebra.'
        args = {'text': _LIVE_TEXT}
        if declare is not None:
            args['expects_reply'] = declare
        return {'content': '', 'tool_calls': [{
            'id': f'send_{len(state["steers"])}', 'type': 'function',
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
