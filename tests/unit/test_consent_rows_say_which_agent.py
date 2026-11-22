"""GET /api/social/consent says which agent each row is for, by id and name.

Owner, 2026-11-21: a kept consent is turned off "in nunba admin for that
agent".  The list route left agent_id out of every row, so no page could
show one agent's consent or tell an agent's own no from "Allow ALL agents".
The switch itself is the existing POST /consent/decline with agent_id
(tests/unit/test_a_no_to_one_agent_stands_under_allow_all.py).

    python -m pytest tests/unit/test_consent_rows_say_which_agent.py -q
"""
import contextlib
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TOKEN = 'TEST-USER-'
UID = 10
AGENT = '88659566083'
NAMELESS = '70264903070'
PHONE = 'device:' + 'ab' * 32
_AUTH = {'Authorization': f'Bearer {_TOKEN}{UID}'}
_KEYS_BEFORE = {'id', 'consent_type', 'scope', 'granted', 'granted_at',
                'revoked_at', 'reopened_at'}


@pytest.fixture
def factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social.models import User, UserConsent

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    User.__table__.create(engine)
    UserConsent.__table__.create(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False)
    with _db(make) as db:
        # The agent's social mirror: where its person-facing name is kept.
        db.add(User(id='mirror-1', username='vijai_agent', display_name='Vijai',
                    user_type='agent', agent_id=AGENT, api_token='tok-mirror-1'))
    yield make
    engine.dispose()


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    """Nothing leaves the test, and no real prompt file names an agent."""
    from integrations.social import consent_service
    import core.platform_paths as platform_paths
    monkeypatch.setattr(consent_service, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(consent_service, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(platform_paths, 'get_recipe_prompts_dir', lambda: str(tmp_path))


@pytest.fixture
def client(factory, monkeypatch):
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


def _svc(factory, method, consent_type, **kw):
    from integrations.social.consent_service import ConsentService
    with _db(factory) as db:
        return getattr(ConsentService, method)(db, str(UID), consent_type, **kw)


def _rows(client, **query):
    resp = client.get('/api/social/consent', query_string=query, headers=_AUTH)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['data']['consents']


def test_a_row_for_one_agent_names_it_by_id_and_by_name(client, factory):
    _svc(factory, 'request_consent', 'computer_control', agent_id=AGENT)
    (row,) = _rows(client)
    assert row.get('agent_id') == AGENT, f'the row does not say which agent: {row}'
    assert row.get('agent_name') == 'Vijai'


def test_an_allow_all_row_names_no_agent(client, factory):
    _svc(factory, 'grant_consent', 'computer_control')
    (row,) = _rows(client)
    assert 'agent_id' in row and row['agent_id'] is None
    assert 'agent_name' in row and row['agent_name'] is None


def test_an_agent_with_no_name_kept_still_shows_its_id(client, factory):
    _svc(factory, 'request_consent', 'computer_control', agent_id=NAMELESS)
    (row,) = _rows(client)
    assert row['agent_id'] == NAMELESS
    assert row['agent_name'] is None


def test_each_row_carries_its_own_agent(client, factory):
    _svc(factory, 'grant_consent', 'computer_control')
    _svc(factory, 'request_consent', 'computer_control', agent_id=AGENT)
    _svc(factory, 'request_consent', 'computer_control', agent_id=NAMELESS)
    seen = {r['agent_id']: r['agent_name'] for r in _rows(client)}
    assert seen == {None: None, AGENT: 'Vijai', NAMELESS: None}


def test_the_agents_no_under_allow_all_is_listed_as_that_agents(client, factory):
    """What the admin switch reads back after Off."""
    _svc(factory, 'grant_consent', 'computer_control')
    resp = client.post('/api/social/consent/decline', headers=_AUTH,
                       json={'consent_type': 'computer_control', 'agent_id': AGENT})
    assert resp.status_code == 200
    mine = [r for r in _rows(client) if r['agent_id'] == AGENT]
    assert len(mine) == 1 and mine[0]['revoked_at'] and not mine[0]['granted']
    assert mine[0]['agent_name'] == 'Vijai'


def test_one_name_lookup_per_agent_not_per_row(client, factory, monkeypatch):
    from integrations.social import consent_api
    looked_up = []
    monkeypatch.setattr(consent_api, 'agent_display_name',
                        lambda db, aid: looked_up.append(aid) or 'Vijai')
    _svc(factory, 'grant_consent', 'computer_control')
    _svc(factory, 'request_consent', 'computer_control', agent_id=AGENT)
    _svc(factory, 'request_consent', 'screen_capture', agent_id=AGENT)
    _svc(factory, 'request_consent', 'credential', agent_id=AGENT, scope='secret:X')
    assert len(_rows(client)) == 4
    assert looked_up == [AGENT], f'looked up {looked_up}'


# -- each row says where its agent stands, by the rule the tools enforce ------
#
# Review of Nunba b0849302: the admin page worked the state out again in
# JavaScript, and for a no given 0.8 ms before "Allow ALL agents" it showed
# "Off for this agent" while check_consent allowed the agent.  The row now
# carries the state (consent_service.agent_state) and the page shows it.

def _add(factory, agent, granted, granted_at=None, revoked_at=None,
         reopened_at=None, scope='*', consent_type='camera_capture'):
    import uuid
    from datetime import datetime
    from integrations.social.models import UserConsent
    at = lambda t: datetime.fromisoformat(t) if t else None  # noqa: E731
    with _db(factory) as db:
        db.add(UserConsent(id=str(uuid.uuid4()), user_id=str(UID), agent_id=agent,
                           consent_type=consent_type, scope=scope, granted=granted,
                           granted_at=at(granted_at), revoked_at=at(revoked_at),
                           reopened_at=at(reopened_at)))


def _states(client, **query):
    return {(r['consent_type'], r['agent_id']): r.get('agent_state', 'NOT SAID')
            for r in _rows(client, **query)}


def _checked(factory, agent, consent_type='camera_capture'):
    from integrations.social.consent_service import ConsentService
    with _db(factory) as db:
        return ConsentService.check_consent(db, str(UID), consent_type, agent_id=agent)


def test_a_no_under_allow_all_reads_turned_off_and_the_others_allowed(client, factory):
    _svc(factory, 'grant_consent', 'camera_capture')
    resp = client.post('/api/social/consent/decline', headers=_AUTH,
                       json={'consent_type': 'camera_capture', 'agent_id': AGENT})
    assert resp.status_code == 200
    assert _states(client, agent_id=AGENT) == {
        ('camera_capture', None): 'turned_off', ('camera_capture', AGENT): 'turned_off'}
    # Asked about another agent, the every-agent row says that agent's
    # state; this agent's own row still says its own.
    assert _states(client, agent_id=NAMELESS) == {
        ('camera_capture', None): 'allowed', ('camera_capture', AGENT): 'turned_off'}
    # Asked about no agent: the every-agent row is its own state.
    assert _states(client) == {
        ('camera_capture', None): 'allowed', ('camera_capture', AGENT): 'turned_off'}


@pytest.mark.parametrize('name, grant_at, no_at, state', [
    ('the no 0.8 ms before the allow-all', '2026-11-21T12:00:00.500900',
     '2026-11-21T12:00:00.500100', 'allowed'),
    ('the no 0.8 ms after the allow-all', '2026-11-21T12:00:00.500100',
     '2026-11-21T12:00:00.500900', 'turned_off'),
    ('the no at the very time of the allow-all', '2026-11-21T12:00:00.500100',
     '2026-11-21T12:00:00.500100', 'turned_off'),
    ('the no one second before the allow-all', '2026-11-21T12:00:01',
     '2026-11-21T12:00:00', 'allowed'),
])
def test_the_state_is_check_consents_to_the_microsecond(client, factory, name,
                                                        grant_at, no_at, state):
    _add(factory, None, True, granted_at=grant_at)
    _add(factory, AGENT, False, revoked_at=no_at)
    states = _states(client, agent_id=AGENT)
    assert states[('camera_capture', AGENT)] == state, name
    assert states[('camera_capture', None)] == state, name
    assert (state == 'allowed') is _checked(factory, AGENT), (
        'the row says one thing and check_consent another')


def test_a_no_taken_back_reads_allowed_again(client, factory):
    _add(factory, None, True, granted_at='2026-11-21T10:00:00')
    _add(factory, AGENT, False, revoked_at='2026-11-21T11:00:00',
         reopened_at='2026-11-21T11:30:00')
    assert _states(client, agent_id=AGENT)[('camera_capture', AGENT)] == 'allowed'


def test_a_no_for_one_scope_leaves_every_scope_allowed(client, factory):
    _add(factory, None, True, granted_at='2026-11-21T12:00:00')
    _add(factory, AGENT, False, revoked_at='2026-11-21T12:00:01', scope='files')
    rows = _rows(client, agent_id=AGENT)
    by_scope = {r['scope']: r.get('agent_state', 'NOT SAID') for r in rows}
    assert by_scope == {'*': 'allowed', 'files': 'turned_off'}


def test_an_ask_nobody_answered_has_no_state(client, factory):
    _svc(factory, 'request_consent', 'computer_control', agent_id=AGENT)
    (row,) = _rows(client, agent_id=AGENT)
    assert 'agent_state' in row and row['agent_state'] is None


def test_a_declined_ask_with_no_allow_all_reads_turned_off(client, factory):
    _svc(factory, 'request_consent', 'computer_control', agent_id=AGENT)
    _svc(factory, 'decline', 'computer_control', agent_id=AGENT)
    (row,) = _rows(client)
    assert row.get('agent_state', 'NOT SAID') == 'turned_off'


def test_an_agents_own_grant_reads_allowed(client, factory):
    _svc(factory, 'grant_consent', 'computer_control', agent_id=AGENT)
    (row,) = _rows(client)
    assert row.get('agent_state', 'NOT SAID') == 'allowed'


def test_an_allow_all_that_was_ended_allows_no_one(client, factory):
    _add(factory, None, False, granted_at='2026-11-21T10:00:00',
         revoked_at='2026-11-21T10:30:00')
    assert _states(client, agent_id=AGENT) == {('camera_capture', None): None}
    assert _states(client) == {('camera_capture', None): 'turned_off'}


def test_the_state_is_the_one_the_frame_tools_read(client, factory, monkeypatch):
    """turned_off_for (the camera and screenshot tools) and the row go
    through one reading, consent_service.agent_state."""
    from integrations.social import consent_api, consent_service
    from integrations.social import models
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', str(UID))
    monkeypatch.setattr(models, 'db_session',
                        lambda commit=False: _db(factory))
    _svc(factory, 'grant_consent', 'camera_capture')
    _svc(factory, 'decline', 'camera_capture', agent_id=AGENT)
    assert consent_service.turned_off_for('camera_capture', AGENT) is True
    assert _states(client, agent_id=AGENT)[('camera_capture', AGENT)] == 'turned_off'
    asked = []
    monkeypatch.setattr(consent_api, 'agent_state',
                        lambda db, user, t, scope, agent: asked.append((t, scope, agent))
                        or 'allowed', raising=False)
    assert set(_states(client, agent_id=AGENT).values()) == {'allowed'}
    assert asked == [('camera_capture', '*', AGENT)], (
        f'one reading per type, scope and agent: {asked}')


def test_a_row_of_a_type_this_build_does_not_know_lists_with_no_state(client, factory):
    _add(factory, None, True, granted_at='2026-11-21T10:00:00',
         consent_type='a_type_from_before')
    (row,) = _rows(client)
    assert row['consent_type'] == 'a_type_from_before'
    assert row.get('agent_state', 'NOT SAID') is None


# -- what must not change -----------------------------------------------------

def test_every_key_a_row_had_is_still_there(client, factory):
    _svc(factory, 'grant_consent', 'computer_control')
    _svc(factory, 'request_consent', 'computer_control', agent_id=AGENT)
    for row in _rows(client):
        assert _KEYS_BEFORE <= set(row), f'lost {_KEYS_BEFORE - set(row)}'


def test_a_phone_row_keeps_its_label_and_fingerprint(client, factory):
    _svc(factory, 'request_consent', 'device_access', scope=PHONE,
         requester_name='Giri')
    (row,) = _rows(client, consent_type='device_access')
    assert row['label'] == 'Giri'
    assert row['fingerprint'] == 'abab abab abab abab'
    assert row['agent_id'] is None and row['agent_name'] is None


def test_the_filters_and_the_count_are_as_before(client, factory):
    _svc(factory, 'grant_consent', 'computer_control')
    _svc(factory, 'request_consent', 'screen_capture', agent_id=AGENT)
    resp = client.get('/api/social/consent', headers=_AUTH,
                      query_string={'consent_type': 'computer_control',
                                    'active_only': 'true'})
    data = resp.get_json()['data']
    assert data['count'] == 1 and data['consents'][0]['consent_type'] == 'computer_control'
