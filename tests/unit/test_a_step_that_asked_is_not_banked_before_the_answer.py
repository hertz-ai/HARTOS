"""A step that asked the person is not banked before the person answers.

LIVE (2026-11-26, installed build #5, speech agent 95568353503, flow 7):
action 4 is "send_message_to_user: Ask the parent in English about the
child's day and whether words may be spoken in the parent's voice, and save
daily_routine."  Between 12:07 and 12:21 the agent asked the parent 13 times
(sends declaring expects_reply true, or declaring nothing) and opened three
cards; nobody answered.  At 12:20:40 it sent one note that declared
expects_reply false: "I've sent the message asking about your child's day
... Waiting for your reply to save the daily routine information!".  At
12:21:08 the StatusVerifier answered {"status": "completed",
"can_perform_without_user_input": "yes", "message": "... The system is
currently awaiting the parent's reply ..."}.

The #158 hold (state_transition) holds a completed step only when its flag
says "no", so the verdict went to the completion owner, which took the note
as the step's receipt ("the action's own tool result at group log message
100 stands in"); the step was banked and its three cards closed unanswered.
The parent answered at 12:27:16, after the flow had moved on.

The rule now (hartos.lifecycle_hooks.verdict_the_tool_settles, read by
state_transition and by the completion owner): a completed verdict on a step
whose words name no tool but send_message_to_user, while the question it put
to the person is still out (step_open_question), waits on the person: it
reads "no", so the existing #158 path holds the step and the loop says the
step waits for the reply.  Once the answer is recorded the same verdict
completes the step.

An answer given on one of the step's cards is an answer too (hartos-17's
MUST, owner 12:45 IST: the person is never asked again for what they
answered).  An ask_person result whose own JSON says status answered, with
something in the answer, closes every question before it; and at the hold
point (state_transition, before a completed verdict is read) each of the
step's cards is read once more (person_ask.ask_state, read-only) and an
answered one is recorded through record_user_answer under 'ask:<id>', so a
card answered after its call stopped waiting counts.  A dismissed, expired
or still-open card, or an empty answer, closes nothing.

    python -m pytest tests/unit/test_a_step_that_asked_is_not_banked_before_the_answer.py -q -p no:cacheprovider --noconftest
"""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from tests.unit.test_completion_needs_real_work import _call, _result  # noqa: E402

STEP = ("send_message_to_user: Ask the parent in English about the child's "
        "day and whether words may be spoken in the parent's voice, and save "
        "daily_routine.")
ASK = ("Hi! Could you please tell me about your child's day? Also, let me "
       "know if words are spoken in your voice.")
# The 12:20:40 send, verbatim.
NOTE = ("I've sent the message asking about your child's day and whether words "
        "can be spoken in your voice. Waiting for your reply to save the daily "
        "routine information!")
# The 12:21:08 verdict's own words.
AWAITING = ("The message has been sent successfully to the parent. The system is "
            "currently awaiting the parent's reply containing details about the "
            "child's day and voice usage.")


# ── tests/conftest.py's fixtures, here so the file runs under --noconftest ──

@pytest.fixture
def test_user_id():
    return 123


@pytest.fixture
def test_prompt_id():
    return 456


@pytest.fixture
def mock_flask_app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    with app.app_context():
        yield app


@pytest.fixture
def sample_config_json(tmp_path, test_prompt_id, monkeypatch):
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    import hartos.create_recipe as cr
    import hartos.helper as h
    for mod in (cr, h):
        if hasattr(mod, 'PROMPTS_DIR'):
            monkeypatch.setattr(mod, 'PROMPTS_DIR', str(prompts))
    monkeypatch.chdir(tmp_path)
    config = {'personas': [{'name': 'Parent'}],
              'flows': [{'persona': 'Parent', 'actions': [STEP]}]}
    (prompts / f'{test_prompt_id}.json').write_text(json.dumps(config),
                                                     encoding='utf-8')
    return config


def _send(call_id, text, expects_reply=None):
    args = {'text': text}
    if expects_reply is not None:
        args['expects_reply'] = expects_reply
    return [_call(call_id, 'send_message_to_user', json.dumps(args)),
            _result(call_id, f'Message sent successfully to user with request_id: '
                             f'msg_{call_id}-intermediate')]


def _verdict(gate='yes', status='completed', index=4):
    return {'role': 'user', 'name': 'StatusVerifier', 'content': json.dumps({
        'status': status, 'action': STEP, 'action_id': 1, 'message': AWAITING,
        'evidence': {'message_index': index, 'kind': 'tool_receipt'},
        'can_perform_without_user_input': gate})}


def _card(call_id, ask_id, status, answer=None):
    """The step's ask_person call and its result as person_ask's tool
    returns it: 'answered' with the answer (the call waited and got it), or
    'waiting_for_person' (its wait ended first)."""
    component = {'type': 'form', 'description': "How was your child's day?",
                 'fields': [{'name': 'child_day', 'label': "How was the day?",
                             'type': 'text'}]}
    result = {'answered': status == 'answered', 'ask_id': ask_id,
              'status': status}
    if status == 'answered':
        result['answer'] = answer
    return [_call(call_id, 'ask_person', json.dumps(
                {'component': json.dumps(component), 'wait_seconds': 60})),
            _result(call_id, json.dumps(result))]


@pytest.fixture
def selector(test_user_id, test_prompt_id, mock_flask_app, sample_config_json,
             monkeypatch):
    """The REAL state_transition of create_agents (the seam of
    test_create_gate_stops_the_chat), bound to the live step, its group log
    and ledger registered the way create_agents registers them."""
    from tests.unit.test_completion_needs_real_work import _Ledger
    from tests.unit.test_create_gate_stops_the_chat import _built_selector
    import hartos.lifecycle_hooks as lh
    with _built_selector(test_user_id, test_prompt_id) as built:
        log = [{'role': 'user', 'name': 'ChatInstructor',
                'content': f'Execute Action 1: {STEP} ,Latest User message: '}]
        log += _send('ask1', ASK, expects_reply=True)
        log += _send('note1', NOTE, expects_reply=False)
        group = SimpleNamespace(messages=log, agents=[SimpleNamespace(
            name='Helper', llm_config=None,
            _function_map={'send_message_to_user': (lambda **_kw: None),
                           'save_data_in_memory': (lambda **_kw: None),
                           'google_search': (lambda **_kw: None)})])
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _up: group)
        ledger = _Ledger(STEP)
        monkeypatch.setattr(lh, 'get_registered_ledger', lambda _up: ledger)
        lh.force_state_through_valid_path(built.up, 1, lh.ActionState.IN_PROGRESS,
                                          'test')
        built.group, built.lh, built.ledger = group, lh, ledger
        yield built


def test_the_live_verdict_holds_the_step_for_the_persons_answer(selector):
    s = selector
    s.group.messages.append(_verdict())
    chosen = s.select(s.verify, s.group)
    assert chosen is None, (
        'a step whose question is still out went to the completion owner on '
        '"completed" + "yes" (live 12:21:08: banked, three cards closed '
        f'unanswered); got {chosen!r}')
    assert s.task._needs_user_input_action_id == 1, 'the gate did not hold the step'
    assert s.task._needs_user_input_kind == 'awaiting_reply', (
        'the step has asked already; the loop must say it waits for the reply')


def test_the_verdict_the_pipeline_acts_on_says_the_step_waits(selector):
    s = selector
    verdict = json.loads(_verdict()['content'])
    before = json.dumps(verdict, sort_keys=True)
    settled = s.lh.verdict_the_tool_settles(s.up, 1, verdict)
    assert str(settled.get('can_perform_without_user_input')).lower().startswith('no'), (
        f'the open question did not hold the verdict: {settled!r}')
    assert settled.get('status') == 'completed' and settled.get('action_id') == 1
    assert json.dumps(verdict, sort_keys=True) == before, "the judge's dict was changed"


def test_once_the_answer_is_recorded_the_same_verdict_completes(selector, tmp_path):
    """The person's reply closes the question (record_user_answer, the one
    writer, and the person-seat line that carries it): the step is the
    completion owner's again."""
    from tests.unit.test_create_gate_stops_the_chat import _ledger_for
    s = selector
    with _ledger_for(s.up, tmp_path, in_progress=True):
        assert s.lh.record_user_answer(s.up, 1, 'She played with blocks all day',
                                       0, request_id='msg_parent_reply')
        s.group.messages.append({'role': 'user', 'name': 'UserProxy',
                                 'content': 'She played with blocks all day'})
        s.group.messages += _send('note2', 'Thank you, saved.', expects_reply=False)
        s.group.messages.append(_verdict(index=len(s.group.messages) - 1))
        chosen = s.select(s.verify, s.group)
    assert getattr(s.task, '_needs_user_input_action_id', None) is None, (
        'the person answered; holding the step again asks twice')
    assert chosen is s.chat_instructor, chosen


def test_a_step_with_no_question_out_is_unchanged(selector):
    """A send step that only told the person something: the completed verdict
    goes to the completion owner as before."""
    s = selector
    del s.group.messages[1:3]          # the asking send and its result
    s.group.messages.append(_verdict(index=2))
    verdict = json.loads(s.group.messages[-1]['content'])
    assert s.lh.verdict_the_tool_settles(s.up, 1, verdict) is verdict
    assert s.select(s.verify, s.group) is s.chat_instructor
    assert getattr(s.task, '_needs_user_input_action_id', None) is None


def test_a_step_that_names_another_tool_is_unchanged(selector):
    """That tool's receipt decides such a step (_work_results); a question it
    also sent does not turn its verdict."""
    s = selector
    s.ledger.tasks['action_1'].description = (
        'google_search: find speech games for a two year old, then '
        'send_message_to_user: ask the parent which one to try')
    verdict = json.loads(_verdict()['content'])
    assert s.lh.verdict_the_tool_settles(s.up, 1, verdict) is verdict


def test_a_verdict_that_already_says_no_is_left_as_it_is(selector):
    s = selector
    verdict = json.loads(_verdict(gate='no - the parent answers this step')['content'])
    assert s.lh.verdict_the_tool_settles(s.up, 1, verdict) is verdict


# ── the card leg: an answer given on the step's card ─────────────────────

def _the_card_says(monkeypatch, state):
    """person_ask.ask_state, as the hold point reads a card (read-only)."""
    import integrations.agent_engine.person_ask as pa
    read = []

    def ask_state(ask_id, user_id):
        read.append((ask_id, user_id))
        return dict(state)
    monkeypatch.setattr(pa, 'ask_state', ask_state)
    return read


def _recorded(s):
    return s.lh.recorded_answer_requests(s.up, 1, 0)


def test_a_card_answered_in_its_wait_closes_the_question_and_is_recorded_once(
        selector, tmp_path):
    from tests.unit.test_create_gate_stops_the_chat import _ledger_for
    s = selector
    del s.group.messages[3:]           # keep the dispatch and the asking send
    s.group.messages += _card('card1', 'A1', 'answered',
                              {'child_day': 'She played with blocks all day'})
    s.group.messages += _send('note2', 'Thank you, saved.', expects_reply=False)
    s.group.messages.append(_verdict(index=len(s.group.messages) - 1))
    with _ledger_for(s.up, tmp_path, in_progress=True):
        chosen = s.select(s.verify, s.group)
        recorded = _recorded(s)
        # The next verdict reads the same card again: nothing new is recorded.
        s.group.messages.append(_verdict(index=len(s.group.messages) - 2))
        s.select(s.verify, s.group)
        again = _recorded(s)
    assert getattr(s.task, '_needs_user_input_action_id', None) is None, (
        'she answered on the card; holding the step asks her again')
    assert chosen is s.chat_instructor, chosen
    assert recorded == [('She played with blocks all day', 'ask:A1')], recorded
    assert again == recorded, again


def test_a_card_answered_after_its_wait_is_read_at_the_hold_point(
        selector, tmp_path, monkeypatch):
    from tests.unit.test_create_gate_stops_the_chat import _ledger_for
    s = selector
    read = _the_card_says(monkeypatch, {'status': 'answered',
                                        'answer': {'child_day': 'She napped'}})
    del s.group.messages[3:]
    s.group.messages += _card('card2', 'A2', 'waiting_for_person')
    s.group.messages += _send('note2', NOTE, expects_reply=False)
    s.group.messages.append(_verdict(index=len(s.group.messages) - 1))
    with _ledger_for(s.up, tmp_path, in_progress=True):
        chosen = s.select(s.verify, s.group)
        recorded = _recorded(s)
    assert ('A2', str(s.up).rsplit('_', 1)[0]) in read, read
    assert recorded == [('She napped', 'ask:A2')], recorded
    assert chosen is s.chat_instructor, chosen
    assert getattr(s.task, '_needs_user_input_action_id', None) is None


@pytest.mark.parametrize('state', [
    {'status': 'open', 'answer': None},
    {'status': 'dismissed', 'answer': None},
    {'status': 'expired', 'answer': None},
    {'status': 'answered', 'answer': {'child_day': '   '}},
])
def test_a_card_not_answered_closes_nothing(selector, tmp_path, monkeypatch, state):
    from tests.unit.test_create_gate_stops_the_chat import _ledger_for
    s = selector
    _the_card_says(monkeypatch, state)
    del s.group.messages[3:]
    s.group.messages += _card('card3', 'A3', 'waiting_for_person')
    s.group.messages += _send('note2', NOTE, expects_reply=False)
    s.group.messages.append(_verdict(index=len(s.group.messages) - 1))
    with _ledger_for(s.up, tmp_path, in_progress=True):
        chosen = s.select(s.verify, s.group)
        recorded = _recorded(s)
    assert recorded == [], recorded
    assert chosen is None and s.task._needs_user_input_action_id == 1, (
        f'a card that is {state} is no answer; the step must wait')


def test_an_answered_card_with_nothing_in_it_closes_nothing(selector, monkeypatch):
    s = selector
    _the_card_says(monkeypatch, {'status': 'answered', 'answer': {'child_day': ''}})
    del s.group.messages[3:]
    s.group.messages += _card('card4', 'A4', 'answered', {'child_day': ''})
    s.group.messages += _send('note2', NOTE, expects_reply=False)
    s.group.messages.append(_verdict(index=len(s.group.messages) - 1))
    assert s.select(s.verify, s.group) is None
    assert s.task._needs_user_input_action_id == 1


def test_a_card_answered_before_a_later_question_leaves_that_question_open(selector):
    s = selector
    del s.group.messages[1:]           # keep the dispatch
    s.group.messages += _card('card5', 'A5', 'answered', {'child_day': 'Busy'})
    s.group.messages += _send('ask2', 'And may words be spoken in your voice?',
                              expects_reply=True)
    s.group.messages += _send('note2', NOTE, expects_reply=False)
    s.group.messages.append(_verdict(index=len(s.group.messages) - 1))
    assert s.select(s.verify, s.group) is None
    assert s.task._needs_user_input_action_id == 1


def test_reuse_reads_an_answered_card_the_same_way(selector):
    """window_asked (REUSE's reader of the one scan) agrees: a card the
    person answered after the question leaves no question out; a question
    asked after the card does."""
    s = selector
    answered = s.group.messages[:3] + _card('card6', 'A6', 'answered',
                                            {'child_day': 'Busy'})
    later = s.group.messages[:1] + _card('card7', 'A7', 'answered',
                                         {'child_day': 'Busy'}) + s.group.messages[1:3]
    assert s.lh.window_asked(answered, 1, []) is False
    assert s.lh.window_asked(later, 1, []) is True
