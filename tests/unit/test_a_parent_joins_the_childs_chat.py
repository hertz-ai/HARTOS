"""A parent or therapist joins a child's chat with an agent, by user id.

Owner, 2026-11-04: "make joining work with minimalistic changes, we have same
user id based joining work if they are from different devices".  Before this
no one could join: update_persona(new=False) looked the person up only in
temp_users, a contact-number map that nothing in HARTOS or Nunba fills, so
every join answered "Ask the user with contact number:X to create a new
chat".  Three more breaks (hartos-7a, 11-04): a first join raised KeyError on
agents_roles, a join replaced the joiner's joins to other agents, and
get_role raised KeyError for someone who had joined a different agent.

The real reuse_recipe join and get_role, the real core.flow_progress store on
a temp data dir.

    python -m pytest tests/unit/test_a_parent_joins_the_childs_chat.py -q
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

CHILD, PARENT, P = 'u-child', 'u-parent', 77


@pytest.fixture
def rr(monkeypatch, tmp_path):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    from hartos import reuse_recipe as rr
    for name in ('agents_session', 'agents_roles', 'chat_joinees', 'temp_users'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    with Flask('join-test').app_context():
        yield rr


def _child_chat(rr):
    rr.agents_session[f'{CHILD}_{P}'] = [{'user_id': CHILD, 'role': 'Child'}]


def test_a_parent_joins_by_the_childs_user_id(rr):
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
    set_persona(CHILD, P, 'Child')
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
