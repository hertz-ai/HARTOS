"""A no to one agent stands while "Allow ALL agents" stays on for the rest.

Owner, 2026-11-21: a consent an agent asked for once is kept for the agent's
whole life "unless explicitly asked to be turned off by user or user consents
off in nunba admin for that agent".

Every grant a page or a card makes is "Allow ALL agents": a row with no
agent.  Measured on the desktop's consent table the same day (read-only,
counts): 5 active computer_control grants and 12 screen_capture, none of
them for an agent.  So a no for one agent has to stand next to that grant.
On 1fef56008 it did not:

    allow all, then POST /consent/decline for an agent that never asked
        -> 404 "no such ask", nothing written
    agent asks, allow all, then "Don't allow" that agent
        -> the row is marked and check_consent still passes for the agent

    python -m pytest tests/unit/test_a_no_to_one_agent_stands_under_allow_all.py -q
"""
import contextlib
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TOKEN = 'TEST-USER-'
UID = 10
AGENT = '88659566083'
OTHER = '70264903070'
TYPE = 'computer_control'       # asked per agent (vlm.safety); no feed behind it


@pytest.fixture
def factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social.models import UserConsent

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    UserConsent.__table__.create(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """Nothing leaves the test: no broadcast, no audit entry."""
    from integrations.social import consent_service
    monkeypatch.setattr(consent_service, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(consent_service, '_audit', lambda *a, **k: None)


@pytest.fixture
def client(factory, monkeypatch):
    """The consent routes every client calls, on the same database."""
    from flask import Flask
    from integrations.social import auth as auth_mod
    from integrations.social import consent_api

    def _user(token):
        if not (isinstance(token, str) and token.startswith(_TOKEN)):
            return None, None
        return (SimpleNamespace(id=int(token[len(_TOKEN):]), is_admin=False,
                                is_moderator=False), factory())

    monkeypatch.setattr(auth_mod, '_get_user_from_token', _user)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(consent_api.consent_bp)
    return app.test_client()


@contextlib.contextmanager
def _db(factory):
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


_AUTH = {'Authorization': f'Bearer {_TOKEN}{UID}'}


def _post(client, path='', **body):
    body.setdefault('consent_type', TYPE)
    return client.post(f'/api/social/consent{path}', json=body, headers=_AUTH)


def _rows(client):
    resp = client.get('/api/social/consent', query_string={'consent_type': TYPE},
                      headers=_AUTH)
    assert resp.status_code == 200
    return resp.get_json()['data']['consents']


def _allow_all(client, factory, ago=60):
    """The page's or a card's Allow ("Allow ALL agents"), given ``ago``
    seconds back, so the order of two answers never rests on the clock's
    resolution."""
    from integrations.social.models import UserConsent
    resp = _post(client)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    row_id = resp.get_json()['data']['id']
    with _db(factory) as db:
        row = db.query(UserConsent).filter_by(id=row_id).one()
        row.granted_at = row.granted_at - timedelta(seconds=ago)


def _no_given(factory, agent_id, ago):
    """Put the agent's no ``ago`` seconds back."""
    from integrations.social.models import UserConsent
    with _db(factory) as db:
        row = db.query(UserConsent).filter_by(agent_id=agent_id).one()
        row.revoked_at = datetime.utcnow() - timedelta(seconds=ago)


def _svc(factory, method, *args, **kw):
    from integrations.social.consent_service import ConsentService
    with _db(factory) as db:
        return getattr(ConsentService, method)(db, str(UID), TYPE, *args, **kw)


def _allowed(factory, agent_id=None, scope='*'):
    return _svc(factory, 'check_consent', scope=scope, agent_id=agent_id)


# -- the off switch for one agent -------------------------------------------

def test_a_no_to_an_agent_that_never_asked_turns_it_off_for_that_agent(client, factory):
    _allow_all(client, factory)
    assert _allowed(factory, AGENT) is True
    resp = _post(client, '/decline', agent_id=AGENT)
    assert resp.status_code == 200, (
        'allow-all stands and one agent is turned off; the route answered '
        f'{resp.status_code} {resp.get_data(as_text=True)}')
    assert _allowed(factory, AGENT) is False, (
        'the owner turned this agent off and the check still passes')


def test_the_other_agents_and_the_grant_itself_stay_on(client, factory):
    _allow_all(client, factory)
    _post(client, '/decline', agent_id=AGENT)
    assert _allowed(factory, AGENT) is False
    assert _allowed(factory, OTHER) is True, 'a no to one agent turned another off'
    assert _allowed(factory) is True, 'a no to one agent ended allow-all'
    assert [r for r in _rows(client) if r['granted'] and not r['revoked_at']], (
        'the allow-all row is no longer active')


def test_a_no_on_an_agents_open_ask_stands_after_allow_all(client, factory):
    """The agent asked, another surface allowed all, then this agent's card
    was answered "Don't allow <agent>"."""
    _svc(factory, 'request_consent', agent_id=AGENT)
    _allow_all(client, factory)
    assert _post(client, '/decline', agent_id=AGENT).status_code == 200
    assert _allowed(factory, AGENT) is False
    assert _allowed(factory, OTHER) is True


def test_the_service_revoke_for_one_agent_does_the_same(client, factory):
    """/api/agent/approval's Deny (record_capability_decision) and Nunba's
    agent routes call revoke_consent with the agent."""
    _allow_all(client, factory)
    row = _svc(factory, 'revoke_consent', agent_id=AGENT)
    assert row is not None and row.agent_id == AGENT and row.revoked_at is not None
    assert row.granted is False and row.granted_at is None, (
        'the no must not read as a grant that was once given')
    assert _allowed(factory, AGENT) is False


def test_an_agent_turned_off_is_told_no_and_not_asked_again(client, factory):
    from integrations.social import consent_service
    _allow_all(client, factory)
    _post(client, '/decline', agent_id=AGENT)
    with patch.object(consent_service, '_emit') as emit:
        assert _svc(factory, 'check_or_request', agent_id=AGENT) is False
    emit.assert_not_called()
    assert _svc(factory, 'declined', agent_id=AGENT) is True


def test_a_no_for_every_scope_covers_a_scoped_use(client, factory):
    _allow_all(client, factory)
    _post(client, '/decline', agent_id=AGENT)
    assert _allowed(factory, AGENT, scope='files') is False
    assert _allowed(factory, OTHER, scope='files') is True


def test_a_no_for_one_scope_leaves_the_agents_other_uses_on(client, factory):
    _allow_all(client, factory)
    assert _post(client, '/decline', agent_id=AGENT, scope='files').status_code == 200
    assert _allowed(factory, AGENT, scope='files') is False
    assert _allowed(factory, AGENT) is True
    assert _allowed(factory, AGENT, scope='shell') is True


# -- the ways back ------------------------------------------------------------

def test_allow_all_given_after_the_no_covers_the_agent_again(client, factory):
    """The card's promise: a no "lasts until you allow it again in Privacy
    settings".  The first Allow is still on file; the newest one counts."""
    _allow_all(client, factory, ago=120)
    _post(client, '/decline', agent_id=AGENT)
    _no_given(factory, AGENT, ago=60)
    assert _allowed(factory, AGENT) is False
    _allow_all(client, factory, ago=0)
    assert _allowed(factory, AGENT) is True, (
        'the owner allowed all agents again after the no')


def test_off_then_on_from_the_page_covers_the_agent_again(client, factory):
    _allow_all(client, factory, ago=120)
    _post(client, '/decline', agent_id=AGENT)
    _no_given(factory, AGENT, ago=60)
    assert _post(client, '/revoke').status_code == 200
    assert _allowed(factory, OTHER) is False
    _allow_all(client, factory, ago=0)
    assert _allowed(factory, AGENT) is True


def test_taking_the_no_back_covers_the_agent_again(client, factory):
    _allow_all(client, factory)
    _post(client, '/decline', agent_id=AGENT)
    assert _allowed(factory, AGENT) is False
    assert _post(client, '/reopen').status_code == 200
    assert _allowed(factory, AGENT) is True, (
        '"Allow asking again" took the no back and allow-all still stands')


def test_an_approve_on_that_agents_card_covers_it_again(client, factory, monkeypatch):
    """The phone's Approve (/api/agent/approval -> record_capability_decision)
    for the very agent that was turned off: the newest answer counts."""
    from integrations.social import consent_service
    from integrations.social.consent_service import ConsentService
    monkeypatch.setattr(consent_service, '_embodied_feed_from_consent',
                        lambda *a, **k: None)
    with _db(factory) as db:
        ConsentService.grant_consent(db, str(UID), 'camera_capture')
    with _db(factory) as db:
        ConsentService.revoke_consent(db, str(UID), 'camera_capture', agent_id=AGENT)
    from integrations.social.models import UserConsent
    with _db(factory) as db:
        grant = db.query(UserConsent).filter_by(agent_id=None).one()
        grant.granted_at = grant.granted_at - timedelta(seconds=120)
        no = db.query(UserConsent).filter_by(agent_id=AGENT).one()
        no.revoked_at = datetime.utcnow() - timedelta(seconds=60)
    with _db(factory) as db:
        assert ConsentService.check_consent(
            db, str(UID), 'camera_capture', agent_id=AGENT) is False
        assert ConsentService.record_capability_decision(
            db, str(UID), 'enable_camera', True, agent_id=AGENT) == 'camera_capture'
    with _db(factory) as db:
        assert ConsentService.check_consent(
            db, str(UID), 'camera_capture', agent_id=AGENT) is True, (
            'the owner approved this agent after the no and it is still refused')


def test_an_approve_while_allowed_adds_no_second_grant(factory, monkeypatch):
    """As before: an Approve for an agent that allow-all already covers
    leaves the one grant alone."""
    from integrations.social import consent_service
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import UserConsent
    monkeypatch.setattr(consent_service, '_embodied_feed_from_consent',
                        lambda *a, **k: None)
    with _db(factory) as db:
        ConsentService.grant_consent(db, str(UID), 'camera_capture')
    with _db(factory) as db:
        ConsentService.record_capability_decision(
            db, str(UID), 'enable_camera', True, agent_id=AGENT)
    with _db(factory) as db:
        assert db.query(UserConsent).count() == 1


# -- what must not change -----------------------------------------------------

def test_a_no_given_before_allow_all_is_covered_by_it(client, factory):
    """Don't allow one agent, then Allow ALL: the agent is allowed, as it was."""
    _svc(factory, 'request_consent', agent_id=AGENT)
    assert _post(client, '/decline', agent_id=AGENT).status_code == 200
    _no_given(factory, AGENT, ago=60)
    _allow_all(client, factory, ago=0)
    assert _allowed(factory, AGENT) is True


def test_a_no_at_the_very_time_of_the_grant_wins(client, factory):
    """One tie rule for consent, the safe side: with one time on both
    answers (a store that keeps whole seconds) the no wins, as it does for
    the feed (ConsentService.feed_said_no)."""
    from integrations.social.models import UserConsent
    _svc(factory, 'request_consent', agent_id=AGENT)
    _post(client, '/decline', agent_id=AGENT)
    _allow_all(client, factory, ago=0)
    with _db(factory) as db:
        grant = db.query(UserConsent).filter_by(agent_id=None).one()
        no = db.query(UserConsent).filter_by(agent_id=AGENT).one()
        no.revoked_at = grant.granted_at
    assert _allowed(factory, AGENT) is False
    assert _allowed(factory, OTHER) is True


def test_with_no_grant_and_no_ask_there_is_nothing_to_decline(client, factory):
    assert _post(client, '/decline', agent_id=AGENT).status_code == 404
    assert _svc(factory, 'revoke_consent', agent_id=AGENT) is None
    assert _rows(client) == [], 'a row was written for nothing'


def test_a_no_without_an_agent_writes_no_row_of_its_own(client, factory):
    """Under allow-all, a revoke that names no agent and a scope nobody was
    granted has nothing to end, as before."""
    _allow_all(client, factory)
    assert _svc(factory, 'revoke_consent', scope='files') is None
    assert len(_rows(client)) == 1
    assert _allowed(factory) is True


def test_one_agents_own_grant_is_not_ended_by_another_agents_no(client, factory):
    _svc(factory, 'grant_consent', agent_id=OTHER)
    _allow_all(client, factory)
    _post(client, '/decline', agent_id=AGENT)
    assert _allowed(factory, OTHER) is True
    assert _allowed(factory, AGENT) is False
