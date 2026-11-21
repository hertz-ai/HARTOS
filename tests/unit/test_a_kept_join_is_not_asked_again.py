"""A join the chat's owner already allowed is made again without a second ask.

Owner, 2026-11-21: a consent asked once is kept until the user turns it off.
The join's approval is kept as a chat_join grant (core.persona_registry
.record_join), and every reader of the join asks joined_chats.  The join
tool did not: update_persona(new=False) -> _reuse_join_by_tool looked only at
the join still waiting for an answer, so a person whose join stood (after a
restart, a lapsed session cache, or picking their persona again) had the
child's side asked a second time.

The real reuse_recipe join, core.flow_progress store (temp data dir) and
consent table (in memory); person_ask is a recorded fake.

    python -m pytest tests/unit/test_a_kept_join_is_not_asked_again.py -q
"""
from __future__ import annotations

import json
import os
import sys
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

PARENT, P, OTHER_AGENT = 'u-parent-kept', 78, 79


class Asks:
    def __init__(self):
        self.sent = []

    def ask(self, user_id, agent_id, component, *, prompt_id=None, **_k):
        self.sent.append((user_id, component))
        return {'ok': True, 'ask_id': f'ask-{len(self.sent)}', 'refused': None, 'card': True}


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
    from integrations.social import consent_service
    monkeypatch.setattr(consent_service, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(consent_service, '_audit', lambda *a, **k: None)
    from integrations.social.models import Base, get_engine
    Base.metadata.create_all(get_engine())
    with Flask('kept-join-test').app_context():
        yield rr


@pytest.fixture
def asks(monkeypatch):
    from integrations.agent_engine import person_ask
    import core.event_attribution as event_attribution
    fake = Asks()
    monkeypatch.setattr(person_ask, 'ask', fake.ask)
    monkeypatch.setattr(event_attribution, 'person_for', lambda uid, *_a, **_k: str(uid))
    return fake


def _child(rr, started=True):
    """A child's account on this node, with (by default) their chat of agent
    P started."""
    from integrations.social.models import User, db_session
    uid = str(uuid.uuid4())
    with db_session() as db:
        db.add(User(id=uid, username=f'child_{uid[:8]}', display_name='Arjun',
                    user_type='human', api_token=f'tok-{uid}'))
    if started:
        rr.agents_session[f'{uid}_{P}'] = [
            {'agentInstanceID': f'x.{P}.{uid}', 'user_id': uid, 'role': 'Child',
             'deviceID': 'something'}]
    return uid


def _members(rr, child):
    return [m['user_id'] for m in rr.agents_session.get(f'{child}_{P}') or []]


def _end_the_join(child):
    """The child's side ends the join: the consent API's revoke of the kept
    grant."""
    from integrations.social.models import UserConsent, db_session
    from datetime import datetime
    with db_session() as db:
        for row in db.query(UserConsent).filter_by(user_id=child,
                                                   consent_type='chat_join').all():
            row.revoked_at = datetime.utcnow()


def test_a_join_that_stands_is_made_again_without_asking(rr, asks):
    child = _child(rr)
    assert rr.record_join(child, P, PARENT) is True     # the child's side allowed it
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert asks.sent == [], (
        f'the child\'s side was asked again for a join it already allowed: {reply}')
    assert reply == 'terminate'
    assert PARENT in _members(rr, child)


def test_no_second_grant_row_and_no_waiting_join_are_written(rr, asks):
    from core.flow_progress import pending_join
    from integrations.social.models import UserConsent, db_session
    child = _child(rr)
    rr.record_join(child, P, PARENT)
    rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    with db_session() as db:
        assert db.query(UserConsent).filter_by(
            user_id=child, consent_type='chat_join').count() == 1
    assert pending_join(PARENT, P) is None


def test_the_persona_picked_this_time_is_the_one_joined_as(rr, asks):
    child = _child(rr)
    rr.record_join(child, P, PARENT)
    assert rr._reuse_join_by_tool(PARENT, P, 'Therapist', child) == 'terminate'
    assert asks.sent == []
    assert rr.agents_roles[f'{PARENT}_{P}'][PARENT] == 'Therapist'


def test_a_kept_join_whose_chat_is_not_started_says_so_and_asks_no_one(rr, asks):
    child = _child(rr, started=False)
    rr.record_join(child, P, PARENT)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert 'to start this chat first' in reply
    assert asks.sent == []


# -- what must not change -----------------------------------------------------

def test_a_first_join_still_asks_the_childs_side(rr, asks):
    child = _child(rr)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert reply.startswith('I have asked the account holder of ' + child)
    assert [who for who, _card in asks.sent] == [child]
    assert PARENT not in _members(rr, child)


def test_a_join_the_childs_side_ended_is_asked_for_again(rr, asks):
    child = _child(rr)
    rr.record_join(child, P, PARENT)
    _end_the_join(child)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert reply.startswith('I have asked the account holder of ' + child)
    assert [who for who, _card in asks.sent] == [child]
    assert PARENT not in _members(rr, child)


def test_a_join_kept_for_another_agent_opens_nothing_here(rr, asks):
    """The grant is per agent (owner 2026-11-10)."""
    child = _child(rr)
    rr.record_join(child, OTHER_AGENT, PARENT)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert reply.startswith('I have asked the account holder of ' + child)
    assert [who for who, _card in asks.sent] == [child]


def test_a_join_kept_with_someone_else_does_not_cover_this_child(rr, asks):
    first, second = _child(rr), _child(rr)
    rr.record_join(first, P, PARENT)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', second)
    assert reply.startswith('I have asked the account holder of ' + second)
    assert [who for who, _card in asks.sent] == [second]
