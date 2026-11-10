"""A parent or therapist joins a child's chat with an agent, from their own
account, once the child's side allows it.

Owner, 2026-11-04: "make joining work with minimalistic changes".  Owner,
2026-11-06: "multiple persons multiple accounts associated via consent".
Before this no one could join: update_persona(new=False) looked the person up
only in temp_users, a contact-number map that nothing in HARTOS or Nunba
fills.  Three more breaks (hartos-7a, 11-04): a first join raised KeyError on
agents_roles, a join replaced the joiner's joins to other agents, and
get_role raised KeyError for someone who had joined a different agent.  The
review of the first version (f9951c0d2, 11-06) found that anyone naming a
child's user id joined as any persona with nobody asked.

The real reuse_recipe join, consent turn and get_role; the real
core.flow_progress store on a temp data dir; person_ask is a recorded fake
(its own behaviour is pinned in tests/unit/test_person_ask*.py).

    python -m pytest tests/unit/test_a_parent_joins_the_childs_chat.py -q
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

CHILD, PARENT, GUARDIAN, P = 'u-child', 'u-parent', 'u-guardian', 77


class Asks:
    """person_ask as the join sees it: who was asked what, and their answer."""

    def __init__(self):
        self.sent, self.state = [], {'status': 'open', 'answer': None}

    def ask(self, user_id, agent_id, component, *, prompt_id=None, **_k):
        self.sent.append((user_id, component))
        return {'ok': True, 'ask_id': f'ask-{len(self.sent)}', 'refused': None, 'card': True}

    def ask_state(self, ask_id, user_id):
        self.state_read = (ask_id, user_id)
        return dict(self.state)


@pytest.fixture
def rr(monkeypatch, tmp_path):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    from hartos import reuse_recipe as rr
    for name in ('agents_session', 'agents_roles', 'chat_joinees', 'temp_users'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    prompt = tmp_path / f'{P}.json'
    prompt.write_text(json.dumps({'personas': [{'name': 'Child'}, {'name': 'Parent'},
                                               {'name': 'Therapist'}], 'flows': []}),
                      encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    with Flask('join-test').app_context():
        yield rr


@pytest.fixture
def asks(monkeypatch):
    from integrations.agent_engine import person_ask
    import core.event_attribution as event_attribution
    fake = Asks()
    monkeypatch.setattr(person_ask, 'ask', fake.ask)
    monkeypatch.setattr(person_ask, 'ask_state', fake.ask_state)
    # The child's account is answered by its guardian: the one 'who is asked'
    # resolver decides that, never the raw user id.
    monkeypatch.setattr(event_attribution, 'person_for',
                        lambda uid, *_a, **_k: GUARDIAN if str(uid) == CHILD else str(uid))
    return fake


def _child_chat(rr):
    rr.agents_session[f'{CHILD}_{P}'] = [{'user_id': CHILD, 'role': 'Child'}]


def test_a_join_asks_the_childs_side_and_changes_nothing_yet(rr, asks):
    from core.flow_progress import pending_join
    _child_chat(rr)
    reply = rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    assert 'asked the account holder of u-child' in reply
    (asked, card), = asks.sent
    assert asked == GUARDIAN and card['type'] == 'approval' and 'as Parent' in card['description']
    assert pending_join(PARENT, P)['persona'] == 'Parent'
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD]
    assert PARENT not in rr.chat_joinees
    assert rr.get_role(PARENT, P) == 'user'


def test_an_allowed_join_makes_them_a_member_on_their_next_turn(rr, asks):
    from core.flow_progress import pending_join
    _child_chat(rr)
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    asks.state = {'status': 'answered', 'answer': {'decision': 'approve'}}
    assert rr._reuse_pending_join_turn(PARENT, P) is None          # the turn goes on, as a member
    assert asks.state_read == ('ask-1', GUARDIAN)
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD, PARENT]
    assert rr.get_role(PARENT, P) == 'Parent'
    assert pending_join(PARENT, P) is None


@pytest.mark.parametrize('state', [{'status': 'answered', 'answer': {'decision': 'deny'}},
                                   {'status': 'dismissed', 'answer': None}])
def test_a_refused_or_dismissed_join_changes_nothing(rr, asks, state):
    from core.flow_progress import pending_join
    _child_chat(rr)
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    asks.state = state
    assert 'did not allow you' in rr._reuse_pending_join_turn(PARENT, P)
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD]
    assert pending_join(PARENT, P) is None and rr.get_role(PARENT, P) == 'user'


def test_an_unanswered_join_says_it_is_still_waiting(rr, asks):
    _child_chat(rr)
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    assert 'Still waiting' in rr._reuse_pending_join_turn(PARENT, P)
    assert len(asks.sent) == 1


def test_an_ask_lost_to_a_restart_is_asked_again(rr, asks):
    from core.flow_progress import pending_join
    _child_chat(rr)
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    asks.state = {'status': 'expired', 'answer': None}
    rr._reuse_pending_join_turn(PARENT, P)
    assert len(asks.sent) == 2 and asks.sent[0] == asks.sent[1]     # the same card again
    assert pending_join(PARENT, P)['ask_id'] == 'ask-2'


def test_no_pending_join_leaves_the_turn_alone(rr, asks):
    assert rr._reuse_pending_join_turn(PARENT, P) is None
    assert asks.sent == []


def test_the_join_completes_by_the_childs_user_id(rr):
    _child_chat(rr)
    assert rr._reuse_join_chat(PARENT, P, 'Parent', CHILD) == 'terminate'
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD, PARENT]
    assert rr.chat_joinees[PARENT] == {P: CHILD}
    assert rr.agents_roles[f'{PARENT}_{P}'] == {PARENT: 'Parent'}   # a first join: no KeyError
    assert rr.get_role(PARENT, P) == 'Parent'


def test_a_mapped_contact_number_still_joins(rr):
    _child_chat(rr)
    rr.temp_users['0000-test'] = CHILD
    assert rr._reuse_join_chat(PARENT, P, 'Parent', '0000-test') == 'terminate'
    assert rr.chat_joinees[PARENT] == {P: CHILD}


def test_a_child_known_only_from_the_kept_pick_can_be_joined(rr):
    """The child's session expired (2 h) or the node restarted."""
    from core.flow_progress import set_persona
    set_persona(CHILD, P, 'Child', ['Child', 'Parent', 'Therapist'])
    assert rr._reuse_join_chat(PARENT, P, 'Parent', CHILD) == 'terminate'


def test_no_chat_to_join_says_what_to_ask_and_writes_nothing(rr):
    reply = rr._reuse_join_chat(PARENT, P, 'Parent', CHILD)
    assert reply.startswith('Ask the person with user id or contact u-child')
    assert rr.chat_joinees == {} and rr.agents_session == {}
    assert rr.get_role(PARENT, P) == 'user'


def test_joining_a_second_agent_keeps_the_first(rr):
    _child_chat(rr)
    rr.chat_joinees[PARENT] = {55: 'u-other'}
    rr._reuse_join_chat(PARENT, P, 'Parent', CHILD)
    assert rr.chat_joinees[PARENT] == {55: 'u-other', P: CHILD}


def test_someone_who_joined_another_agent_has_no_role_here(rr):
    rr.chat_joinees[PARENT] = {55: 'u-other'}
    assert rr.get_role(PARENT, P) == 'user'           # no KeyError


def test_the_joiners_pick_outlives_the_caches(rr):
    _child_chat(rr)
    rr._reuse_join_chat(PARENT, P, 'Therapist', CHILD)
    rr.agents_session.clear()
    rr.chat_joinees.clear()
    rr.agents_roles.clear()
    assert rr.get_role(PARENT, P) == 'Therapist'
