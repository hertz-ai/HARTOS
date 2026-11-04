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
