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
