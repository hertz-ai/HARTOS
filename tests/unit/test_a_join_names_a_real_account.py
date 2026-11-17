"""A join asks someone only when it names an account on this node, and the
same join is asked once however often the model calls for it.

Live 13 Nov 2026 12:30 IST, Nunba on Claude Sonnet 5.5, the Tamil speech
agent: on the child's persona turn the model called update_persona six times,
twice with new=False and an invented contact_number '1234567890'.  Approval
asks went to that nonexistent account and the child was told "Still waiting
for the account holder of 1234567890 to allow you to join as Therapist".  The
parent's next turn called it 19 times.  update_persona's join (new=False) is
_reuse_join_by_tool: it resolves the number the same way as before
(temp_users, then the string as a user id), refuses when no users row is
behind it, and does not ask again for a join already pending.

The real reuse_recipe join and core.flow_progress store on a temp data dir,
the real users table (in memory); person_ask is a recorded fake.

    python -m pytest tests/unit/test_a_join_names_a_real_account.py -q
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

PARENT, P = 'u-parent', 78


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
    from integrations.social.models import Base, get_engine
    Base.metadata.create_all(get_engine())
    with Flask('join-account-test').app_context():
        yield rr


@pytest.fixture
def asks(monkeypatch):
    from integrations.agent_engine import person_ask
    import core.event_attribution as event_attribution
    fake = Asks()
    monkeypatch.setattr(person_ask, 'ask', fake.ask)
    monkeypatch.setattr(event_attribution, 'person_for', lambda uid, *_a, **_k: str(uid))
    return fake


def _account():
    """A person's users row on this node: the child whose chat is joined."""
    from integrations.social.models import User, db_session
    uid = str(uuid.uuid4())
    with db_session() as db:
        db.add(User(id=uid, username=f'child_{uid[:8]}', display_name='Arjun',
                    user_type='human', api_token=f'tok-{uid}'))
    return uid


def test_a_made_up_account_asks_no_one_and_changes_nothing(rr, asks):
    from core.flow_progress import pending_join
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', '1234567890')
    assert 'There is no account 1234567890' in reply and 'never make one up' in reply
    assert asks.sent == []
    assert pending_join(PARENT, P) is None


def test_a_real_account_named_by_its_user_id_is_asked(rr, asks):
    from core.flow_progress import pending_join
    child = _account()
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert reply.startswith('I have asked the account holder of ' + child)
    assert [who for who, _card in asks.sent] == [child]
    assert pending_join(PARENT, P)['creator'] == child


def test_a_mapped_contact_number_is_resolved_before_the_check(rr, asks):
    child = _account()
    rr.temp_users['0000-test'] = child
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', '0000-test').startswith('I have asked')
    assert [who for who, _card in asks.sent] == [child]


def test_the_same_join_called_again_is_asked_once(rr, asks):
    child = _account()
    first = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    for _ in range(18):
        assert rr._reuse_join_by_tool(PARENT, P, 'Parent', child) == first
    assert len(asks.sent) == 1


def test_a_different_persona_for_the_same_chat_is_asked_again(rr, asks):
    child = _account()
    rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert len(asks.sent) == 2


@pytest.mark.parametrize('named', ['', '   ', PARENT])
def test_no_number_or_ones_own_id_asks_for_the_id(rr, asks, named):
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', named) == (
        'Ask the person for the user id of the chat they want to join.')
    assert asks.sent == []
