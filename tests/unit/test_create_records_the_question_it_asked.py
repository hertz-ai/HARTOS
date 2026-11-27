"""CREATE keeps the question a held step asked on its ask record.

CREATE held a step for its person without the question: create_recipe
._hold_for_the_person -> mark_action_waiting_for_user -> _record_the_persons_
ask -> record_ask_delivered(..., None, flow), so the record's question and
asked were None on every CREATE hold (REUSE records its own).  The reply to a
turn on the held step (held_step_question) read the step's window instead,
and after a restart the window is gone: the person got a line about waiting,
not the question she was asked.

Now the hold passes the step's open question (step_question_text: a send's
whole text, or a card's title and description, read inside the step's own
window) to the record, which keeps it as question and its question part as
asked; after a restart the reply is that question.  A record already open is
not changed (REUSE's, or an earlier hold's), and a hold with nothing asked
records none, as before (hartos-17's GO on #290 commit 4).

    python -m pytest tests/unit/test_create_records_the_question_it_asked.py -q -p no:cacheprovider --noconftest
"""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from tests.unit.test_completion_needs_real_work import _call, _result  # noqa: E402

UP = '123_456'
STEP = "send_message_to_user: Agree the weekly review day with the parent."
DRAFT = ("Here is this week's plan: three short sessions with paal and poo. "
         "Shall I start with Monday?")


@pytest.fixture
def mock_flask_app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    with app.app_context():
        yield app


def _dispatch(action_id):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Execute Action {action_id}: step {action_id} ,Latest User message: '}


def _send(call_id, text, expects_reply=None):
    args = {'text': text}
    if expects_reply is not None:
        args['expects_reply'] = expects_reply
    return [_call(call_id, 'send_message_to_user', json.dumps(args)),
            _result(call_id, f'Message sent successfully to user with request_id: '
                             f'msg_{call_id}-intermediate')]


def _card(call_id, ask_id):
    component = {'type': 'form', 'title': 'Weekly review',
                 'description': 'Which day suits you for the weekly review?',
                 'fields': [{'name': 'day', 'label': 'Day', 'type': 'text'}]}
    return [_call(call_id, 'ask_person', json.dumps(
                {'component': json.dumps(component), 'wait_seconds': 60})),
            _result(call_id, json.dumps({'answered': False, 'ask_id': ask_id,
                                         'status': 'waiting_for_person'}))]


@pytest.fixture
def held(tmp_path, monkeypatch, mock_flask_app):
    """A CREATE session on its person's turn: a real ledger with two steps in
    progress, the session's task and request, and its group chat's messages
    set per test (the window the hold reads).  The node's model is recorded."""
    import hartos.lifecycle_hooks as lh
    import hartos.create_recipe as cr
    import core.llm_json
    from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType
    from hartos.helper import Action
    lh.clear_action_states(UP)
    ledger = SmartLedger(agent_id='456', session_id=f'{UP}_0', ledger_dir=str(tmp_path))
    for aid in (1, 2):
        ledger.tasks[f'action_{aid}'] = Task(task_id=f'action_{aid}',
                                             description=f'step {aid}',
                                             task_type=TaskType.PRE_ASSIGNED)
        ledger.tasks[f'action_{aid}'].status = TaskStatus.IN_PROGRESS
    lh.register_ledger_for_session(UP, ledger)
    task = Action([STEP, 'Save the plan'])
    monkeypatch.setattr(cr, 'user_tasks', {UP: task})
    monkeypatch.setattr(cr, 'request_id_list', {UP: 'msg_her_turn'})
    state = SimpleNamespace(messages=[], asked=[], task=task, lh=lh, cr=cr)
    chat = lambda _up: SimpleNamespace(messages=state.messages)  # noqa: E731
    monkeypatch.setattr(lh, 'get_registered_groupchat', chat)
    monkeypatch.setattr(cr, 'get_registered_groupchat', chat)

    def model(content, schema, name, **kw):
        state.asked.append(name)
        return None
    monkeypatch.setattr(core.llm_json, 'ask_model_json', model)
    for aid in (1, 2):
        lh.force_state_through_valid_path(UP, aid, lh.ActionState.IN_PROGRESS, 'test')
    try:
        yield state
    finally:
        lh.clear_action_states(UP)
        with lh._state_lock:
            lh._ledger_registry.pop(UP, None)


def _record(s, action_id=1):
    return s.lh.ledger_open_ask(UP, action_id, 0) or {}


def test_a_hold_records_the_send_that_asked(held):
    held.messages += [_dispatch(1)] + _send('c1', DRAFT)
    held.cr._hold_for_the_person(UP, 1, 'asked the person: the plan')
    record = _record(held)
    assert record.get('question') == DRAFT, record
    assert record.get('asked') == 'Shall I start with Monday?', record


def test_a_hold_on_a_card_records_the_cards_own_words(held):
    held.messages += [_dispatch(1)] + _card('c2', 'A1')
    held.cr._hold_for_the_person(UP, 1, 'asked the person on a card')
    assert _record(held).get('question') == (
        'Weekly review: Which day suits you for the weekly review?')


def test_a_hold_in_a_later_round_records_the_steps_question_not_a_later_line(held):
    """hartos-17: the hold can come rounds after the ask; it records the
    question the step asked, not a later note that asked nothing."""
    held.messages += ([_dispatch(1)] + _send('c1', DRAFT)
                      + [{'role': 'user', 'name': 'ChatInstructor',
                          'content': 'Lets continue the work we were doing'}]
                      + _send('c3', "Noted, I'll wait for your answer.", expects_reply=False)
                      + [{'role': 'assistant', 'name': 'Assistant',
                          'content': 'The plan is ready to send.'}])
    held.cr._hold_for_the_person(UP, 1, 'asked the person: the plan')
    assert _record(held).get('question') == DRAFT, _record(held)


def test_the_walk_stays_inside_the_steps_own_window(held):
    """Step 1 asked; step 2, held with nothing asked, does not take step 1's
    question for its own."""
    held.messages += [_dispatch(1)] + _send('c1', DRAFT) + [_dispatch(2)]
    held.cr._hold_for_the_person(UP, 2, 'needs the person')
    record = _record(held, action_id=2)
    assert record, 'the hold recorded no ask for step 2'
    assert record.get('question') is None and record.get('asked') is None, record


def test_after_a_restart_the_reply_is_the_recorded_question(held):
    held.messages += [_dispatch(1)] + _send('c1', DRAFT)
    held.cr._hold_for_the_person(UP, 1, 'asked the person: the plan')
    # A restart: the group chat is new and the session's gate flag is gone.
    held.messages.clear()
    held.task._needs_user_input_action_id = None
    held.task._needs_user_input_kind = None
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('msg_her_next_turn')
    reply = held.cr._ask_for_help(UP, 456, 1, STEP, 'the step waits for her')
    assert reply == 'Shall I start with Monday?', reply
    assert held.lh.held_step_reply(UP, 1, 0) == 'Shall I start with Monday?'
    assert held.asked == [], 'the model was asked for a line the record holds'


def test_a_hold_with_nothing_asked_records_no_question(held):
    held.messages += [_dispatch(1)]
    held.cr._hold_for_the_person(UP, 1, 'needs the person')
    record = _record(held)
    assert record and record.get('question') is None, record
    assert held.lh.held_step_reply(UP, 1, 0) == held.lh.HELD_STEP_FALLBACK_LINE


def test_an_open_record_keeps_its_question(held):
    """REUSE records its ask, with the question, before it holds; one open
    record per step, so a later hold changes nothing on it."""
    assert held.lh.record_ask_delivered(UP, 1, 'msg_her_turn', 3, 0, via='send',
                                        question='Which flower do you like?')
    held.messages += [_dispatch(1)] + _send('c1', DRAFT)
    held.lh.mark_action_waiting_for_user(UP, 1, 'waiting for the reply', flow=0,
                                         asked_in='msg_her_turn', question=DRAFT)
    assert _record(held).get('question') == 'Which flower do you like?', _record(held)
