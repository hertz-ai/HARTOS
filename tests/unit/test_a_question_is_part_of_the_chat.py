"""A question is part of the asking agent's chat (design_1125 A, v4).

Owner, 2026-11-23 and 2026-11-25: a question an agent asks is a saved chat
message, answerable later; tonight the tablet still showed separate
Later/Dismiss question cards where the desktop shows them inside the chat.
Before this change a question was a Liquid UI card and a bell row only,
never written into the agent's chat (ConversationEntry); the open-ask cap
counted every open ask, so three questions nothing waited on refused every
later one (live 2026-11-25 19:16:46 IST, "3 own asks already open" during
the speech build); and a typed answer the held step took left the card up
on every surface.

Settled in design_1125 v4 and its amendment (hartos-17):
- ask() saves the question as the asking agent's chat message: msg_id is
  the ask id, the agent's ids are on the row, and its attachment is
  {'kind': 'agent_ask', 'ask_id', 'card'}.  Once per ask id; an ask with no
  agent (no prompt_id) stays a card and a bell row.
- person_ask.is_held(ask_id) is True while a tool call waits on the ask
  (wait_for_answer), while a step of the asking session waits for the
  person on the question it asked in that turn (the REUSE and CREATE hold,
  hartos.lifecycle_hooks), or while a waiting goal's card is the ask.  The
  cap counts only held asks.
- A typed reply goes to /chat as today.  When the held step takes it as its
  answer (lifecycle_hooks.record_user_answer, the one answer writer), the
  card it asked closes on every surface with outcome answered_in_chat, and
  a later press on it gets 409.  A reply the step does not take leaves it
  open.
- A finished build still closes its questions (close_create_run_asks), and
  the chat message is marked closed: build_finished.  Every close names its
  outcome on the ask.answered event and on the saved message.

Real person_ask asks through the real ask_person tool (core.agent_tools
.build_core_tool_closures), NotificationService and the in-memory social DB
(the ``legs`` fixture of test_person_ask), a real JSON ledger with the real
hooks (hartos.lifecycle_hooks), and the real chat store
(integrations.social.chat_messages) with its writes run inline and its
chat.new publish recorded.

    python -m pytest tests/unit/test_a_question_is_part_of_the_chat.py -q
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_person_ask import _no_open_asks, _row, _settle, legs  # noqa: E402,F401
from tests.unit.test_every_reply_and_question_names_its_persona import (  # noqa: E402,F401
    CHILD, QUESTION, SOLO, SPEECH, _agent_named, kept)

#: The person's turn the held step asked in, and the child's session with
#: the speech agent.
TURN = 'req-turn-1'
UP = f'{CHILD}_{SPEECH}'
APPROVAL = {'type': 'approval', 'action': 'start_the_lesson',
            'description': 'Shall we start the lesson now?'}


@pytest.fixture
def chat(monkeypatch):
    """The agent chat store, its writes run at once and chat.new recorded."""
    from integrations.social import chat_messages as cm
    published = []
    monkeypatch.setattr(cm, '_EXECUTOR', SimpleNamespace(submit=lambda fn: fn()))
    monkeypatch.setattr(cm, 'publish_new', lambda row: published.append(row))
    return published


@pytest.fixture
def asks(kept, legs, chat):
    _agent_named(SPEECH, 'Speech Buddy')
    legs['published'] = chat
    return legs


@pytest.fixture
def held_step(tmp_path):
    """The child's session: action 1 asked the child in turn TURN and waits
    for the reply (mark_action_waiting_for_user, the one hold CREATE and
    REUSE both write)."""
    from agent_ledger.core import SmartLedger, TaskStatus, add_actions_to_ledger
    from hartos import lifecycle_hooks as L
    L.clear_action_states(UP)
    ledger = SmartLedger(agent_id=SPEECH, session_id=f'{UP}_1',
                         ledger_dir=str(tmp_path / 'ledger'))
    add_actions_to_ledger(ledger, [
        {'action': 'ask what the family calls the child at home', 'action_id': 1},
    ], flow_id=0)
    L.register_ledger_for_session(UP, ledger)
    ledger.tasks['action_1'].status = TaskStatus.IN_PROGRESS
    L.safe_set_state(UP, 1, L.ActionState.IN_PROGRESS, 'working')
    assert L.mark_action_waiting_for_user(UP, 1, 'asks the name at home', flow=0,
                                          asked_in=TURN)
    yield L
    L.clear_action_states(UP)
    with L._state_lock:
        L._ledger_registry.pop(UP, None)


def _tool(session_user=CHILD, request_id=TURN, agent=SPEECH):
    """The ask_person tool of one session with ``agent`` (the speech agent),
    as the create loop builds it, each call in that build's turn (the
    create_run marker): since #295 F6 a question asked on the person's own
    turn outside a build goes in the chat and never becomes a card.
    ``request_id`` is the turn's."""
    from core.agent_tools import build_core_tool_closures
    from integrations.agent_engine import person_ask
    user_prompt = f'{session_user}_{agent}'
    ctx = {'user_id': session_user, 'prompt_id': agent, 'agent_data': {},
           'helper_fun': MagicMock(), 'user_prompt': user_prompt,
           'request_id_list': {user_prompt: request_id}, 'recent_file_id': None,
           'scheduler': None, 'send_message_to_user1': lambda *a, **k: None,
           'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
           'save_conversation_db': lambda *a, **k: None}
    ask_person = {t[0]: t[2] for t in build_core_tool_closures(ctx)}['ask_person']

    def in_the_build(*args, **kwargs):
        token = person_ask._create_run_of.set((str(agent), str(session_user)))
        try:
            return ask_person(*args, **kwargs)
        finally:
            person_ask._create_run_of.reset(token)
    return in_the_build


def _ask(component=None, request_id=TURN):
    """The agent asks on the child's session; the tool's reply."""
    return json.loads(_tool(CHILD, request_id)(json.dumps(component or QUESTION), 0))


def _saved(ask_id):
    """The chat rows saved under ``ask_id``."""
    from integrations.social.models import ConversationEntry, db_session
    with db_session(commit=False) as db:
        return [r.to_dict() for r in db.query(ConversationEntry).filter_by(msg_id=ask_id).all()]


def _agent_ask(row):
    [kept_ask] = [a for a in (row.get('attachments') or []) if a.get('kind') == 'agent_ask']
    return kept_ask


def _outcomes(legs, ask_id):
    """The outcome each ask.answered event for ``ask_id`` named."""
    return [d.get('outcome') for t, d, _u in legs['events']
            if t == 'chat.social' and d.get('type') == 'ask.answered'
            and d.get('ask_id') == ask_id]


# -- the question is the asking agent's chat message --------------------------

def test_a_question_is_saved_once_as_the_asking_agents_chat_message(asks):
    ask_id = _ask()['ask_id']
    [row] = _saved(ask_id)
    assert (row['role'], str(row['user_id']), str(row['prompt_id']), str(row['agent_id'])) == (
        'assistant', CHILD, SPEECH, SPEECH)
    assert row['content'] == 'Agent Speech Buddy asks you'
    kept_ask = _agent_ask(row)
    assert kept_ask['ask_id'] == ask_id and kept_ask['card']['ask_id'] == ask_id
    assert [r['msg_id'] for r in asks['published']] == [ask_id]      # chat.new, once
    assert _ask()['ask_id'] == ask_id                                 # the same question again
    assert len(_saved(ask_id)) == 1 and len(asks['published']) == 1


def test_a_question_with_no_agent_stays_a_card_and_a_bell_row(asks):
    from integrations.agent_engine import person_ask
    out = person_ask.ask(CHILD, 'post_to_channel', {
        'type': 'form', 'title': 'Where should posts on Telegram go?',
        'description': 'Your agent is ready to post on Telegram.',
        'fields': [{'name': 'chat_id', 'label': 'Telegram group or channel?', 'type': 'text'}]})
    assert out['ok'] is True, out
    assert _saved(out['ask_id']) == [] and asks['published'] == []


# -- what holds a question, and the cap ----------------------------------------

def test_a_question_is_held_while_a_tool_call_waits_on_it(asks):
    from integrations.agent_engine import person_ask
    ask_id = _ask(request_id='req-other')['ask_id']         # no step waits on this turn
    assert person_ask.is_held(ask_id) is False
    waiter = threading.Thread(target=person_ask.wait_for_answer, args=(ask_id, 10))
    waiter.start()
    deadline = time.monotonic() + 5
    while not person_ask.is_held(ask_id) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert person_ask.is_held(ask_id) is True
    assert person_ask.answer_ask(ask_id, {'home_name': 'Chinnu'}, CHILD)[1] == 200
    waiter.join(5)
    assert person_ask.is_held(ask_id) is False


def test_a_step_waiting_on_its_question_holds_the_card_it_asked(asks, held_step):
    from integrations.agent_engine import person_ask
    held = _ask()['ask_id']                                   # asked in the step's turn
    other = _ask(dict(QUESTION, description='Another turn: the name at home.'),
                 request_id='req-turn-0')['ask_id']
    assert person_ask.is_held(held) is True
    assert person_ask.is_held(other) is False


def test_a_goal_waiting_on_its_card_holds_the_question(asks, tmp_path, monkeypatch):
    """The third hold: a goal waiting for its person, whose staged plan's
    card is this ask (the readers release_holds_answered uses)."""
    import core.platform_paths as platform_paths
    from integrations.agent_engine import dispatch, person_ask
    plan_pid = dispatch.prompt_id_for_goal('g-plan')
    out = person_ask.ask(CHILD, plan_pid, dict(APPROVAL, action=f'approve_plan:{plan_pid}'),
                         prompt_id=plan_pid)
    assert out['ok'] is True, out
    prompts = tmp_path / 'plans'
    prompts.mkdir()
    (prompts / f'{plan_pid}.proposed.json').write_text(json.dumps(
        {'hold': {'ask_id': out['ask_id'], 'asked': CHILD}}), encoding='utf-8')
    monkeypatch.setattr(platform_paths, 'get_recipe_prompts_dir', lambda: str(prompts))
    waiting = []
    monkeypatch.setattr(dispatch, '_get_distributed_coordinator', lambda create=False:
                        SimpleNamespace(goals_waiting_for_a_person=lambda: list(waiting)))
    assert person_ask.is_held(out['ask_id']) is False
    waiting.append('g-plan')
    assert person_ask.is_held(out['ask_id']) is True


def test_questions_nothing_waits_on_no_longer_refuse_the_next(asks):
    """Live 2026-11-25 19:16:46 IST: '3 own asks already open' refused the
    speech build's next question, and nothing waited on the three."""
    from integrations.agent_engine import person_ask
    for n in range(person_ask.MAX_OPEN_PER_PERSON):
        assert _ask(dict(QUESTION, description=f'Question {n}: the name at home.'),
                    request_id='req-other').get('ask_id')
    nxt = _ask(dict(QUESTION, description='One more: the name at home.'),
               request_id='req-other')
    assert nxt.get('status') != 'refused', nxt


def test_questions_a_held_step_waits_on_still_fill_the_cap(asks, held_step):
    from integrations.agent_engine import person_ask
    for n in range(person_ask.MAX_OPEN_PER_PERSON):
        assert _ask(dict(QUESTION, description=f'Question {n}: the name at home.')).get('ask_id')
    nxt = _ask(dict(QUESTION, description='One more: the name at home.'))
    assert (nxt.get('status'), nxt.get('refused')) == ('refused', 'too_many_open_asks'), nxt


# -- a typed reply the held step takes closes its question (v4 N2) -------------

def test_the_reply_the_held_step_takes_closes_its_question_everywhere(asks, held_step):
    """The person types the answer in the chat; the held step takes it
    (record_user_answer, the one answer writer); the card it asked closes on
    every surface with outcome answered_in_chat, and a later press gets 409.
    A card of another turn, or of another agent, stays open."""
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    # Not the step's: the same session's card from another turn, and another
    # agent's card from the same turn.
    other_turn = _ask(dict(QUESTION, description='Another turn: the name at home.'),
                      request_id='req-turn-0')['ask_id']
    other_agent = json.loads(_tool(agent=SOLO)(json.dumps(QUESTION), 0))['ask_id']
    assert held_step.record_user_answer(UP, 1, 'We call her Chinnu', 0)
    _settle()
    assert person_ask.ask_state(ask_id, CHILD)['status'] == 'answered'
    assert person_ask.is_held(ask_id) is False
    assert _outcomes(asks, ask_id) == ['answered_in_chat']
    assert _row(ask_id)['is_read'] is True
    assert _agent_ask(_saved(ask_id)[0])['outcome'] == 'answered_in_chat'
    assert person_ask.answer_ask(ask_id, {'home_name': 'Chinnu'}, CHILD)[1] == 409
    for still_open in (other_turn, other_agent):
        assert person_ask.ask_state(still_open, CHILD)['status'] == 'open'
        assert _outcomes(asks, still_open) == []


def test_an_approval_answered_by_typing_yes_closes_and_a_later_press_gets_409(asks, held_step):
    """v4 amendment: the step's judgment closes the ask, not the card's type."""
    from integrations.agent_engine import person_ask
    ask_id = _ask(APPROVAL)['ask_id']
    assert held_step.record_user_answer(UP, 1, 'yes', 0)
    _settle()
    assert _outcomes(asks, ask_id) == ['answered_in_chat']
    body, status = person_ask.answer_ask(ask_id, {'decision': 'approve'}, CHILD)
    assert status == 409, body


def test_a_reply_the_step_does_not_take_leaves_its_question_open(asks, held_step):
    """The step judged the reply was not its answer ('what do you mean?'):
    nothing is recorded, it holds again, and the question stays open."""
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    assert person_ask.ask_state(ask_id, CHILD)['status'] == 'open'
    assert person_ask.is_held(ask_id) is True
    assert _outcomes(asks, ask_id) == []


# -- every close names its outcome; a finished build closes its questions ------

def test_every_way_a_question_ends_names_its_outcome(asks):
    from integrations.agent_engine import person_ask
    answered = _ask(dict(QUESTION, description='First: the name at home.'))['ask_id']
    dismissed = _ask(dict(QUESTION, description='Second: the name at home.'))['ask_id']
    assert person_ask.answer_ask(answered, {'home_name': 'Chinnu'}, CHILD)[1] == 200
    assert person_ask.dismiss_ask(dismissed, CHILD)[1] == 200
    _settle()
    assert _outcomes(asks, answered) == ['answered']
    assert _outcomes(asks, dismissed) == ['dismissed']
    assert _agent_ask(_saved(answered)[0])['outcome'] == 'answered'
    assert _agent_ask(_saved(dismissed)[0])['outcome'] == 'dismissed'


def test_a_finished_build_marks_its_question_closed_in_the_chat(asks):
    """M2: a build question answered after the build ends would land out of
    context, so the build's close stays, and the chat message says so."""
    from integrations.agent_engine import person_ask
    asked = []

    @person_ask.create_run
    def recipe(user_id, prompt_id):
        asked.append(_ask()['ask_id'])
        return person_ask.AGENT_BUILT[0]

    assert recipe(CHILD, SPEECH) == person_ask.AGENT_BUILT[0]
    _settle()
    [ask_id] = asked
    assert person_ask.ask_state(ask_id, CHILD)['status'] == 'expired'
    assert _outcomes(asks, ask_id) == ['build_finished']
    assert _agent_ask(_saved(ask_id)[0])['outcome'] == 'build_finished'
