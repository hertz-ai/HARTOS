"""link-hevolve mints a HARTOS JWT only for an email a Hevolve login proves.

Measured 2026-09-14: /api/social/auth/link-hevolve took the body's email on
trust, so any caller could POST an existing user's email and get that user's
JWT, with the user's own role, from the public internet on central (the route
answered an empty body with 400 "email required" through azurekong).  On a
desktop the same call opened every path the LAN gate protects.  The fix asks
Kong which account minted the caller's Hevolve token and links only that
account's email.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from flask import Flask

from integrations.social import kong_identity
from integrations.social.api import social_bp
from integrations.social.models import Base, User, get_db, get_engine
from integrations.social.rate_limiter import get_limiter

OWNER = 'owner@example.com'
ADMIN = 'http://kong-admin:8101'


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv('KONG_ADMIN_URL', raising=False)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(social_bp)
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    get_limiter()._buckets.clear()
    db = get_db()
    try:
        db.add(User(username='owner', display_name='Owner', email=OWNER,
                    user_type='human', role='admin', api_token='t-owner'))
        db.commit()
    finally:
        db.close()
    yield app.test_client()
    Base.metadata.drop_all(engine)


def _link(client, email, token=None):
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return client.post('/api/social/auth/link-hevolve',
                       json={'email': email}, headers=headers)


def _has_account(email):
    db = get_db()
    try:
        return db.query(User).filter(User.email == email).first() is not None
    finally:
        db.close()


# ── The route ──

def test_an_email_alone_gets_no_token(client):
    resp = _link(client, OWNER)
    assert resp.status_code == 401
    assert 'data' not in resp.get_json()


def test_a_token_proving_another_email_gets_no_token(client):
    with patch.object(kong_identity, 'email_for_token',
                      return_value='someone@else.com'):
        resp = _link(client, OWNER, token='their-own-token')
    assert resp.status_code == 401


def test_a_token_kong_cannot_vouch_for_gets_no_token(client):
    # Kong unreachable, or a node without Kong: nothing is proven.
    with patch.object(kong_identity, 'email_for_token', return_value=None):
        resp = _link(client, OWNER, token='kong-token')
    assert resp.status_code == 401


def test_no_account_is_created_under_an_unproven_email(client):
    resp = _link(client, 'victim@example.com')
    assert resp.status_code == 401
    assert not _has_account('victim@example.com')


def test_a_proven_email_links_its_existing_account(client):
    with patch.object(kong_identity, 'email_for_token',
                      return_value=OWNER) as proof:
        resp = _link(client, ' Owner@Example.com ', token='kong-token')
    assert resp.status_code == 200
    assert resp.get_json()['data']['token']
    proof.assert_called_once_with('kong-token')


def test_a_proven_new_email_gets_an_account(client):
    with patch.object(kong_identity, 'email_for_token',
                      return_value='new@example.com'):
        resp = _link(client, 'new@example.com', token='kong-token')
    assert resp.status_code == 201
    assert _has_account('new@example.com')


# ── Asking Kong ──

_LIVE_ROW = {'access_token': 'live-token', 'expires_in': 0,
             'credential': {'id': 'cred-1'}}


def _kong(token_row=_LIVE_ROW, credential=None, consumer=None, status=200):
    credential = credential or {'consumer': {'id': 'cons-1'}}
    consumer = consumer or {'username': 'Owner@Example.com'}
    calls = []

    def get(url, timeout=None):
        calls.append(url)
        if '/oauth2_tokens/' in url:
            body = token_row
        elif '/oauth2/' in url:
            body = credential
        else:
            body = consumer
        return SimpleNamespace(status_code=status, json=lambda: body)
    return get, calls


def test_a_live_token_proves_its_consumer_email(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN + '/')
    get, calls = _kong()
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') == OWNER
    assert calls == [f'{ADMIN}/oauth2_tokens/live-token',
                     f'{ADMIN}/oauth2/cred-1',
                     f'{ADMIN}/consumers/cons-1']


def test_the_token_is_looked_up_by_path_never_by_query(monkeypatch):
    # Kong ignores ?access_token= and returns row 0 of every token.
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, calls = _kong(token_row=dict(_LIVE_ROW, access_token='a/b?c'))
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        kong_identity.email_for_token('a/b?c')
    assert calls[0] == f'{ADMIN}/oauth2_tokens/a%2Fb%3Fc'
    assert not any('?' in c for c in calls)


def test_a_row_for_another_token_proves_nothing(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong(token_row=dict(_LIVE_ROW, access_token='someone-else'))
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') is None


def test_an_expired_token_proves_nothing(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong(token_row=dict(_LIVE_ROW, expires_in=60, created_at=1))
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') is None


def test_an_unknown_token_proves_nothing(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong(status=404)
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') is None


def test_without_kong_nothing_is_proven_or_asked(monkeypatch):
    monkeypatch.delenv('KONG_ADMIN_URL', raising=False)
    with patch.object(kong_identity.requests, 'get') as get:
        assert kong_identity.email_for_token('live-token') is None
    get.assert_not_called()


def test_kong_unreachable_proves_nothing(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    with patch.object(kong_identity.requests, 'get',
                      side_effect=requests.ConnectionError('refused')):
        assert kong_identity.email_for_token('live-token') is None
