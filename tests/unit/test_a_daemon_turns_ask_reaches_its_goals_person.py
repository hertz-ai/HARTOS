"""An ask put out by a background (daemon) turn reaches the goal's person.

Owner 2026-10-31: a background step that asks goes to the owner. Relayed
2026-11-03: "all daemon goals in central or regional nodes shd reach my user
id (owner of central) 10202 / other co owners or the flat topology node shd
reach appropriate local user".

Measured on central 2026-10-31 10:29:25Z: a daemon turn's ask_person push
was addressed to 'system_daemon'. Every daemon /chat turn on a keyless node
runs as that identity (dispatch._internal_auth_headers), and the ask tool
was built with the turn's user_id, so the card, the bell row and the
Firebase push went to a name no person signs in as. Nobody was asked.

The person is worked out when the tool is called, from the turn's request
id (daemon_<goal id>, core.chat_client.daemon_goal_id): the goal's owner
through core.event_attribution.person_for. A goal with no owner (system
seeded) is the node owner's. One agent's session and its tools are shared
by every goal of that agent, so a person fixed when the tool was built
would be the first goal's.

The real build_core_tool_closures, person_ask, NotificationService and
in-memory social DB, with test_person_ask's recorded legs (Liquid UI card,
Firebase push, WAMP).

    python -m pytest tests/unit/test_a_daemon_turns_ask_reaches_its_goals_person.py -q
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tests.unit.test_person_ask import (  # noqa: F401
    POST, _no_open_asks, _row, legs)

OWNER = '10202'
CO_OWNER = '777'


@pytest.fixture
def node(monkeypatch, legs):  # noqa: F811
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    return legs


def _goal(gid, owner_id=None, created_by='system_bootstrap'):
    from integrations.social.models import AgentGoal, db_session
    with db_session() as db:
        db.merge(AgentGoal(id=gid, goal_type='marketing', title='Find buyers',
                           owner_id=owner_id, created_by=created_by))


def _ask_tool(user_id, request_ids, user_prompt=None):
    """The ask_person tool of one session, as the create and reuse loops
    build it (core.agent_tools.build_core_tool_closures)."""
    from core.agent_tools import build_core_tool_closures
    user_prompt = user_prompt or f'{user_id}_342'
    ctx = {'user_id': user_id, 'prompt_id': '342', 'agent_data': {},
           'helper_fun': MagicMock(), 'user_prompt': user_prompt,
           'request_id_list': request_ids, 'recent_file_id': None,
           'scheduler': None, 'send_message_to_user1': lambda *a, **k: None,
           'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
           'save_conversation_db': lambda *a, **k: None}
    return {t[0]: t[2] for t in build_core_tool_closures(ctx)}['ask_person']


def _asked(node, reply):
    """Whom the ask reached: the card's person, the bell row's and the
    Firebase push's."""
    ask_id = json.loads(reply)['ask_id']
    card_to = [user for _agent, card, user in node['cards']
               if card.get('ask_id') == ask_id]
    return card_to, (_row(ask_id) or {}).get('user_id'), ask_id


def test_a_daemon_turns_ask_for_an_unowned_goal_reaches_the_node_owner(node):
    """FAILS BEFORE THE FIX: the card and the row went to 'system_daemon'."""
    _goal('g-seeded')
    ask = _ask_tool('system_daemon', {'system_daemon_342': 'daemon_g-seeded'})
    cards, row_user, _ = _asked(node, ask(json.dumps(POST), 0))
    assert cards == [OWNER] and row_user == OWNER


def test_a_daemon_turns_ask_for_a_co_owners_goal_reaches_that_co_owner(node):
    """FAILS BEFORE THE FIX."""
    _goal('g-co', owner_id=CO_OWNER, created_by=CO_OWNER)
    ask = _ask_tool('system_daemon', {'system_daemon_342': 'daemon_g-co'})
    cards, row_user, _ = _asked(node, ask(json.dumps(POST), 0))
    assert cards == [CO_OWNER] and row_user == CO_OWNER


def test_a_daemon_turn_whose_goal_cannot_be_found_asks_the_node_owner(node):
    """No goal row (it was removed): the turn's identity is a system one,
    which answers to the node owner (person_for). FAILS BEFORE THE FIX."""
    ask = _ask_tool('system_daemon', {'system_daemon_342': 'daemon_g-gone'})
    cards, row_user, _ = _asked(node, ask(json.dumps(POST), 0))
    assert cards == [OWNER] and row_user == OWNER


def test_a_daemon_turn_whose_goal_cannot_be_found_asks_the_turns_own_person(node):
    """No goal row, and the turn runs as a person (not a system identity):
    that person is asked, not the node owner (review of ff45f77a4: a
    'missing goal asks the node owner' mutant passed every test)."""
    ask = _ask_tool(CO_OWNER, {f'{CO_OWNER}_342': 'daemon_g-gone'})
    cards, row_user, _ = _asked(node, ask(json.dumps(POST), 0))
    assert cards == [CO_OWNER] and row_user == CO_OWNER


def test_a_daemon_turn_whose_goal_cannot_be_found_as_an_agent_asks_its_owner(node):
    """No goal row, and the turn runs as an agent row owned by a person:
    the agent's owner is asked (person_for), not the node owner."""
    from integrations.social.models import User, db_session
    with db_session() as db:
        if not db.query(User).filter_by(id='agent_y').first():
            db.add(User(id='agent_y', username='agent_y', display_name='Agent Y',
                        user_type='agent', owner_id=CO_OWNER, api_token='tok-agent_y'))
    ask = _ask_tool('agent_y', {'agent_y_342': 'daemon_g-gone'})
    cards, row_user, _ = _asked(node, ask(json.dumps(POST), 0))
    assert cards == [CO_OWNER] and row_user == CO_OWNER


def test_a_daemon_turn_whose_goal_cannot_be_read_asks_the_turns_own_person(
        node, monkeypatch):
    """The goal read fails: the tool neither raises nor drops the ask; the
    turn's own person is asked (review of ff45f77a4: 'raises out of the
    tool' and 'no person' mutants passed every test)."""
    import integrations.social.models as models
    _goal('g-co', owner_id=OWNER)
    monkeypatch.setattr(models, 'AgentGoal', object())     # the query raises
    ask = _ask_tool(CO_OWNER, {f'{CO_OWNER}_342': 'daemon_g-co'})
    reply = ask(json.dumps(POST), 0)
    assert json.loads(reply)['status'] == 'waiting_for_person', reply
    cards, row_user, _ = _asked(node, reply)
    assert cards == [CO_OWNER] and row_user == CO_OWNER


def test_a_goal_made_by_a_person_with_no_owner_set_asks_its_maker(node):
    """owner_id empty, created_by a person: that person is the goal's
    (core.event_attribution.goal_owner_user_id precedence; review of
    ff45f77a4: an 'owner_id only' mutant passed every test)."""
    _goal('g-made', owner_id=None, created_by=CO_OWNER)
    ask = _ask_tool('system_daemon', {'system_daemon_342': 'daemon_g-made'})
    cards, row_user, _ = _asked(node, ask(json.dumps(POST), 0))
    assert cards == [CO_OWNER] and row_user == CO_OWNER


def test_background_asks_never_take_the_place_of_the_owners_own_ask(node):
    """Three background asks are open for the owner (the per-person limit):
    his own turn's ask still reaches him, and a fourth background ask is
    still refused.  FAILS BEFORE THE FIX: background asks for unowned goals
    used the owner's own limit, so his own agent got 'too_many_open_asks'
    (review of ff45f77a4; 37 of 37 active desktop goals have no owner)."""
    from integrations.agent_engine import person_ask
    rids = {}
    background = _ask_tool('system_daemon', rids)
    open_ids = []
    for i in range(person_ask.MAX_OPEN_PER_PERSON):
        _goal(f'g-{i}')
        rids['system_daemon_342'] = f'daemon_g-{i}'
        reply = json.loads(background(json.dumps(dict(POST, action=f'step_{i}')), 0))
        assert reply['status'] == 'waiting_for_person', reply
        open_ids.append(reply['ask_id'])
    _goal('g-more')
    rids['system_daemon_342'] = 'daemon_g-more'
    more = json.loads(background(json.dumps(dict(POST, action='one_more')), 0))
    note = more.pop('note')            # what to do instead (test_person_ask pins it)
    assert more == {'answered': False, 'refused': 'too_many_open_asks',
                    'status': 'refused'}
    assert 'from background work' in note, note
    # The ids are read for the PERSON asked (the goal's owner), not the
    # turn's user (system_daemon): all 66 refusals measured on the desktop
    # were daemon turns (review of dead56131, mutant M4).
    assert all(ask_id in note for ask_id in open_ids), note

    own = _ask_tool(OWNER, {f'{OWNER}_342': '1730600000'})
    reply = own(json.dumps(dict(POST, action='mine')), 0)
    assert json.loads(reply)['status'] == 'waiting_for_person', reply
    cards, row_user, _ = _asked(node, reply)
    assert cards == [OWNER] and row_user == OWNER


def test_the_owners_own_asks_never_take_the_place_of_background_asks(node):
    """The other way round: three of his own asks open, a background goal's
    ask still reaches him (each kind keeps its own limit)."""
    from integrations.agent_engine import person_ask
    own = _ask_tool(OWNER, {f'{OWNER}_342': '1730600000'})
    for i in range(person_ask.MAX_OPEN_PER_PERSON):
        reply = json.loads(own(json.dumps(dict(POST, action=f'mine_{i}')), 0))
        assert reply['status'] == 'waiting_for_person', reply
    _goal('g-seeded')
    background = _ask_tool('system_daemon', {'system_daemon_342': 'daemon_g-seeded'})
    reply = background(json.dumps(POST), 0)
    assert json.loads(reply)['status'] == 'waiting_for_person', reply
    cards, row_user, _ = _asked(node, reply)
    assert cards == [OWNER] and row_user == OWNER


def test_the_same_question_is_one_ask_whichever_kind_asks_it(node):
    """A background turn and the owner's own turn asking the same question
    get the same ask (no second card, no second push), even when the
    second kind's own limit is already full (review of 85e3e0286: the
    reuse was not pinned)."""
    from integrations.agent_engine import person_ask
    _goal('g-seeded')
    background = _ask_tool('system_daemon', {'system_daemon_342': 'daemon_g-seeded'})
    first = json.loads(background(json.dumps(POST), 0))
    own = _ask_tool(OWNER, {f'{OWNER}_342': '1730600000'})
    for i in range(person_ask.MAX_OPEN_PER_PERSON):
        json.loads(own(json.dumps(dict(POST, action=f'mine_{i}')), 0))
    again = json.loads(own(json.dumps(POST), 0))

    assert again['status'] == 'waiting_for_person', again
    assert again['ask_id'] == first['ask_id']
    assert len([c for c in node['cards'] if c[1].get('ask_id') == first['ask_id']]) == 1


@pytest.mark.parametrize('request_id', ['', None])
def test_a_turn_with_no_request_id_asks_as_background(node, request_id):
    """No request id is not the person's own turn (core.chat_client.
    is_user_turn, dispatch's rule): with the owner's own limit full, such a
    turn's ask still goes out, as background work (review of 85e3e0286:
    the 'id is set' half of the rule was not pinned)."""
    from integrations.agent_engine import person_ask
    own = _ask_tool(OWNER, {f'{OWNER}_342': '1730600000'})
    for i in range(person_ask.MAX_OPEN_PER_PERSON):
        json.loads(own(json.dumps(dict(POST, action=f'mine_{i}')), 0))
    unlabelled = _ask_tool(OWNER, {f'{OWNER}_342': request_id})
    reply = unlabelled(json.dumps(dict(POST, action='no_id')), 0)

    assert json.loads(reply)['status'] == 'waiting_for_person', reply
    assert person_ask._asks[json.loads(reply)['ask_id']].background is True


def test_one_session_asks_each_goals_own_person(node):
    """Two goals of one agent share its session and its tools: each turn's
    ask reaches its own goal's person. FAILS BEFORE THE FIX."""
    _goal('g-a')
    _goal('g-b', owner_id=CO_OWNER, created_by=CO_OWNER)
    rids = {}
    ask = _ask_tool('system_daemon', rids)
    rids['system_daemon_342'] = 'daemon_g-a'
    first = _asked(node, ask(json.dumps(POST), 0))[:2]
    rids['system_daemon_342'] = 'daemon_g-b'
    second = _asked(node, ask(json.dumps(dict(POST, action='post_the_other')), 0))[:2]
    assert first == ([OWNER], OWNER) and second == ([CO_OWNER], CO_OWNER)


def test_the_owner_answers_and_the_daemon_turn_reads_the_answer(node):
    """The person asked can answer, and the agent's later read finds it.
    FAILS BEFORE THE FIX: the ask was 'system_daemon''s, so the owner's
    answer was refused (not_yours)."""
    from integrations.agent_engine import person_ask
    _goal('g-seeded')
    ask = _ask_tool('system_daemon', {'system_daemon_342': 'daemon_g-seeded'})
    _cards, _row_user, ask_id = _asked(node, ask(json.dumps(POST), 0))
    body, status = person_ask.answer_ask(ask_id, {'decision': 'approve'}, OWNER)
    assert status == 200, body
    later = json.loads(ask('', 0, ask_id))
    assert later['answered'] is True and later['answer'] == {'decision': 'approve'}


def test_a_persons_own_turn_still_asks_that_person(node):
    """Not a background turn: the ask is the person's own, as before."""
    _goal('g-seeded')
    ask = _ask_tool(CO_OWNER, {f'{CO_OWNER}_342': '1730600000'})
    cards, row_user, _ = _asked(node, ask(json.dumps(POST), 0))
    assert cards == [CO_OWNER] and row_user == CO_OWNER
