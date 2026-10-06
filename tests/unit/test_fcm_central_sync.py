"""Where a user's pushes go is set one way: the person's profile pull
(#90, #180).

The push path (core/peer_link/local_subscribers -> send_fcm_push) reads the
token by the local social UUID (get_local_fcm_token(uuid)), while central's
registry keys it by account number, so the token must arrive already mapped to
the UUID.  core.profile_sync.sync_profile pulls the person's own profile from
central by their central id, maps the local UUID to that id (core.fcm_sync
pulls the token by it and addresses central's push relay to it) and caches
the FCMtoken under the local UUID.

A sync_user item in a hierarchy batch sets neither.  That receiver also serves
/api/social/hierarchy/sync, which applies an unsigned batch outside hard
enforcement (#188), so either let any caller send another user's pushes to a
phone of their choosing.  No sender ever put them there: the node's own
producer (hart_onboarding) sends neither, and the route central was meant to
call, /auth/sync-user, never had a caller and was deleted on 10-06.

The real sync_profile, receiver and route against an in-memory DB; central is
mocked at fetch_central_profile.  No grep tests.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import profile_sync  # noqa: E402
from core.fcm_sync import (  # noqa: E402
    CENTRAL_ID_SETTINGS_KEY, get_local_fcm_token, resolve_central_id,
    set_central_id, store_local_fcm_token)
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402
from integrations.social.sync_engine import SyncEngine  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(get_engine())


def _central_says(monkeypatch, profile):
    monkeypatch.setattr(profile_sync, 'fetch_central_profile', lambda cid, **k: profile)


def _user(uid):
    with db_session(commit=False) as db:
        row = db.query(User).filter_by(id=uid).first()
        return (row.username, dict(row.settings or {})) if row else None


def test_a_profile_pull_caches_centrals_token_under_the_local_uuid(monkeypatch):
    _central_says(monkeypatch, {'name': 'Alice', 'FCMtoken': 'dev-token-xyz'})
    assert profile_sync.sync_profile('uuid-alice', 9003054371) is True
    assert get_local_fcm_token('uuid-alice') == 'dev-token-xyz'
    name, settings = _user('uuid-alice')
    assert name == 'Alice'
    assert settings.get(CENTRAL_ID_SETTINGS_KEY) == '9003054371'


def test_a_pull_takes_either_spelling_of_centrals_token(monkeypatch):
    _central_says(monkeypatch, {'name': 'Bob', 'fcm_token': 'dev-token-bob'})
    assert profile_sync.sync_profile('uuid-bob', 77) is True
    assert get_local_fcm_token('uuid-bob') == 'dev-token-bob'


def test_a_pull_without_a_token_creates_the_user_and_caches_none(monkeypatch):
    _central_says(monkeypatch, {'name': 'Carol'})
    assert profile_sync.sync_profile('uuid-carol', 78) is True
    assert get_local_fcm_token('uuid-carol') is None
    assert _user('uuid-carol')[0] == 'Carol'


def test_a_repull_replaces_the_cached_token(monkeypatch):
    _central_says(monkeypatch, {'name': 'Dan', 'FCMtoken': 'old'})
    assert profile_sync.sync_profile('uuid-dan', 79) is True
    _central_says(monkeypatch, {'name': 'Dan', 'FCMtoken': 'new'})
    assert profile_sync.sync_profile('uuid-dan', 79) is True
    assert get_local_fcm_token('uuid-dan') == 'new'


def test_a_sync_item_lands_the_user_and_never_sets_where_pushes_go():
    with db_session() as db:
        SyncEngine._handle_sync_user(db, {'user_id': 'uuid-erin', 'username': 'erin'})
    store_local_fcm_token('uuid-erin', 'erins-phone')
    set_central_id('uuid-erin', '9000000001')
    with db_session() as db:
        SyncEngine._handle_sync_user(db, {'user_id': 'uuid-erin', 'username': 'erin',
                                          'fcm_token': 'not-erins',
                                          'central_user_id': '9999999999'})
        SyncEngine._handle_sync_user(db, {'user_id': 'uuid-finn', 'username': 'finn',
                                          'FCMtoken': 'not-finns',
                                          'account_number': '9999999999'})
    assert get_local_fcm_token('uuid-erin') == 'erins-phone'
    assert resolve_central_id('uuid-erin') == '9000000001'
    assert get_local_fcm_token('uuid-finn') is None
    assert resolve_central_id('uuid-finn') is None
    assert _user('uuid-finn')[0] == 'finn'


def test_an_unsigned_batch_at_the_sync_ingress_sets_no_token():
    """#188: outside hard enforcement the ingress applies an unsigned batch.
    It still lands the users, as before; it no longer sets where their
    pushes go."""
    from integrations.social.discovery import discovery_bp
    store_local_fcm_token('uuid-gina', 'ginas-phone')
    app = Flask('central')
    app.register_blueprint(discovery_bp)
    batch = {'node_id': 'no-such-node', 'items': [
        {'id': 'q-gina', 'operation_type': 'sync_user',
         'payload': {'user_id': 'uuid-gina', 'username': 'gina', 'fcm_token': 'callers-phone',
                     'central_user_id': '9999999999'}},
        {'id': 'q-hal', 'operation_type': 'sync_user',
         'payload': {'user_id': 'uuid-hal', 'username': 'hal', 'FCMtoken': 'callers-phone',
                     'phone': '9999999999'}}]}
    with patch('security.master_key.get_enforcement_mode', return_value='warn'):
        resp = app.test_client().post('/api/social/hierarchy/sync', json=batch)
    assert resp.status_code == 200
    assert resp.get_json()['processed'] == ['q-gina', 'q-hal']
    assert get_local_fcm_token('uuid-gina') == 'ginas-phone'
    assert get_local_fcm_token('uuid-hal') is None
    assert resolve_central_id('uuid-gina') is None
    assert resolve_central_id('uuid-hal') is None
    assert _user('uuid-hal')[0] == 'hal'
