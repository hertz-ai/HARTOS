"""link-hevolve mints a HARTOS JWT only for an email a Hevolve login proves.

Measured 2026-09-14: /api/social/auth/link-hevolve took the body's email on
trust, so any caller could POST an existing user's email and get that user's
JWT, with the user's own role, from the public internet on central (the route
answered an empty body with 400 "email required" through azurekong).  On a
desktop the same call opened every path the LAN gate protects.  The fix asks
Kong which account minted the caller's Hevolve token and links only that
account's email.
"""
import logging
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


@pytest.fixture(autouse=True)
def _fresh_kong_answers():
    """Kong's kept answers, the lookup budgets and the refusal report are
    process-wide; every test starts with none of them."""
    # getattr: a run against code without the kept answers still runs the
    # tests, so it fails on what they check.
    def fresh():
        for kept in ('_answers', '_proven'):
            getattr(kong_identity, kept, {}).clear()
        getattr(kong_identity, '_refusals', {}).update(logged_at=0.0, count=0)
        get_limiter()._buckets.clear()
    fresh()
    yield
    fresh()


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


def test_a_banned_account_is_refused_with_the_doors_own_reply(client):
    db = get_db()
    try:
        db.query(User).filter(User.email == OWNER).first().is_banned = True
        db.commit()
    finally:
        db.close()
    with patch.object(kong_identity, 'email_for_token', return_value=OWNER):
        resp = _link(client, OWNER, token='kong-token')
    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'Account banned'
    assert 'data' not in resp.get_json()


def test_an_account_the_one_rule_refuses_gets_no_token(client, monkeypatch):
    """This door mints a JWT, so it asks the ONE "may this person act here"
    rule the Bearer doors ask (auth.user_refusal): a refusal added there
    stops the token here too, with this door's own reply shape.  Before, the
    door read is_banned for itself and a person the rule refused was still
    given a JWT (parallel-path audit 2026-10-27, item 4 rework)."""
    from integrations.social import auth
    from tests.unit.test_user_refusal_one_rule import ADDED_REFUSAL
    monkeypatch.setattr(auth, 'user_refusal',
                        lambda user: 'no_user' if user is None else ADDED_REFUSAL,
                        raising=False)
    with patch.object(kong_identity, 'email_for_token', return_value=OWNER):
        resp = _link(client, OWNER, token='kong-token')
    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'Account on leave'
    assert 'data' not in resp.get_json()


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


# ── Asking Kong as little as the answer allows ──
# Review rv_e97b and hartos-52 of f5e4358e2: every Bearer the JWT check
# refuses cost up to 3 Kong Admin calls, 5 s each, unpaced and unkept, and a
# JWT went into Kong's admin URL.

def test_a_jwt_is_never_sent_to_kong(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    with patch.object(kong_identity.requests, 'get') as get:
        assert kong_identity.email_for_token(
            'eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiMSJ9.c2ln') is None
    get.assert_not_called()


def test_kongs_answer_is_kept_for_the_next_ask(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, calls = _kong()
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') == OWNER
        assert kong_identity.email_for_token('live-token') == OWNER
    assert len(calls) == 3, calls


def test_a_kept_answer_ends_with_the_tokens_lease(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    now = 1_800_000_000
    get, calls = _kong(token_row=dict(_LIVE_ROW, expires_in=30,
                                      created_at=now))
    clock = {'t': now + 1.0}
    monkeypatch.setattr(kong_identity.time, 'time', lambda: clock['t'])
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') == OWNER
        clock['t'] = now + 31.0
        assert kong_identity.email_for_token('live-token') is None
    assert len(calls) == 4, 'the lapsed lease was answered from memory'


def test_kongs_no_is_kept_too(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, calls = _kong(status=404)
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('junk-token') is None
        assert kong_identity.email_for_token('junk-token') is None
    assert len(calls) == 1, calls


def test_kong_unreachable_is_never_remembered(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    with patch.object(kong_identity.requests, 'get',
                      side_effect=requests.ConnectionError('refused')):
        assert kong_identity.email_for_token('live-token') is None
    get, calls = _kong()
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') == OWNER


def test_a_kong_error_status_is_never_remembered_as_a_no(monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong(status=500)
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') is None
    get, calls = _kong()
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') == OWNER
    assert len(calls) == 3


def test_new_tokens_past_the_node_budget_get_no_kong_call(monkeypatch):
    """New tokens past the budget get no Kong call; a token already
    answered still answers."""
    from integrations.social.rate_limiter import LIMITS
    monkeypatch.setitem(LIMITS, 'kong_new_token',
                        {'max_tokens': 2, 'refill_rate': 0.0})
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, asked = _kong_for({'live-token': OWNER})
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('junk-1') is None
        assert kong_identity.email_for_token('junk-2') is None
        before = len(asked)
        assert kong_identity.email_for_token('junk-3') is None
        assert len(asked) == before, 'a token past the budget still reached Kong'
        assert kong_identity.email_for_token('junk-1') is None
        assert len(asked) == before


# ── One gateway address for every phone ──
# hartos-73 via hevolve-66e6, 2026-11-20: central's TRUSTED_PROXY is unset,
# and docker's userland proxy hands HARTOS 172.21.0.1 for every peer
# (core.auth_local.client_address), so a budget per client address was ONE
# budget for every phone: one caller's junk Bearers refused every Hevolve
# login until it refilled.

CENTRAL_PEER = '172.21.0.1'
_central = Flask('central')


def _production_limits(monkeypatch):
    """The Kong budgets as a production node has them; tests otherwise run
    with the limiter relaxed (rate_limiter._build_limits)."""
    from integrations.social import rate_limiter
    monkeypatch.delenv('SOCIAL_RATE_LIMIT_DISABLED', raising=False)
    monkeypatch.delenv('FLASK_ENV', raising=False)
    production = rate_limiter._build_limits()
    for name in ('kong_lookup', 'kong_new_token'):
        if name in production:
            monkeypatch.setitem(rate_limiter.LIMITS, name, production[name])
    # One instant, so no budget refills while a test runs.
    monkeypatch.setattr(kong_identity.time, 'time', lambda: 1_800_000_000.0)
    return rate_limiter.LIMITS


def _from_central():
    """A request as central's HARTOS sees every phone's: from the gateway."""
    return _central.test_request_context(
        '/chat', environ_base={'REMOTE_ADDR': CENTRAL_PEER})


def _kong_for(real, failing=()):
    """Kong with a live row for each token in ``real`` (token -> email), no
    row for any other token, and a 500 for the tokens in ``failing``."""
    asked = []

    def get(url, timeout=None):
        asked.append(url)
        kind, ident = url[len(ADMIN) + 1:].split('/', 1)
        if kind == 'oauth2_tokens':
            if ident in failing:
                return SimpleNamespace(status_code=500, json=lambda: {})
            if ident not in real:
                return SimpleNamespace(status_code=404, json=lambda: {})
            row = {'access_token': ident, 'expires_in': 0,
                   'credential': {'id': f'cred-{ident}'}}
            return SimpleNamespace(status_code=200, json=lambda: row)
        token = ident.split('-', 1)[1]
        if kind == 'oauth2':
            body = {'consumer': {'id': f'cons-{token}'}}
        else:
            body = {'username': real[token]}
        return SimpleNamespace(status_code=200, json=lambda: body)
    return get, asked


def _token_asks(asked, token):
    return sum(1 for url in asked if url.endswith(f'/oauth2_tokens/{token}'))


def test_junk_cannot_refuse_a_phone_kong_proved_before(monkeypatch):
    limits = _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, asked = _kong_for({'phone-token': OWNER})
    with (patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        assert kong_identity.email_for_token('phone-token') == OWNER
        kong_identity._answers.clear()  # its kept answer lapses
        for n in range(200):
            assert kong_identity.email_for_token(f'junk{n}') is None
        assert kong_identity.email_for_token('phone-token') == OWNER, (
            'junk Bearers refused a phone Kong had proven')
    junk = sum(1 for url in asked if '/oauth2_tokens/junk' in url)
    ceiling = limits.get('kong_new_token', limits['kong_lookup'])['max_tokens']
    assert junk <= ceiling, f'{junk} junk tokens reached Kong'


def test_a_burst_of_first_logins_is_not_refused(monkeypatch):
    """The Kong export (485 tokens, 2023-06-07 to 2023-10-04) minted at most
    7 tokens in any minute and 14 in any ten.  Thirty first logins at once,
    all through the one gateway address, all pass."""
    _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    real = {f'phone{n}': f'user{n}@example.com' for n in range(30)}
    get, _ = _kong_for(real)
    with (patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        got = [kong_identity.email_for_token(token) for token in real]
    assert got == list(real.values())


def test_one_tokens_retries_are_paced_on_their_own(monkeypatch):
    """While Kong errors on one token, that token's retries spend its own
    budget, and another phone is still asked about."""
    limits = _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    failing = set()
    get, asked = _kong_for({'phone-a': OWNER, 'phone-b': 'b@example.com'},
                           failing)
    with (patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        assert kong_identity.email_for_token('phone-a') == OWNER
        assert kong_identity.email_for_token('phone-b') == 'b@example.com'
        kong_identity._answers.clear()
        failing.add('phone-a')
        for _ in range(30):
            assert kong_identity.email_for_token('phone-a') is None
        assert kong_identity.email_for_token('phone-b') == 'b@example.com'
    assert _token_asks(asked, 'phone-a') <= limits['kong_lookup']['max_tokens']


def test_a_token_kong_refuses_again_spends_the_node_budget(monkeypatch):
    """Being proven once frees a token from the node budget only until Kong
    says no to it."""
    from integrations.social.rate_limiter import LIMITS
    monkeypatch.setitem(LIMITS, 'kong_new_token',
                        {'max_tokens': 1, 'refill_rate': 0.0})
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    real = {'phone-token': OWNER}
    get, asked = _kong_for(real)
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('phone-token') == OWNER
        kong_identity._answers.clear()
        real.clear()  # Kong no longer has the token
        assert kong_identity.email_for_token('phone-token') is None
        kong_identity._answers.clear()
        assert kong_identity.email_for_token('phone-token') is None
    assert _token_asks(asked, 'phone-token') == 2, asked


def test_a_junk_flood_is_reported_once(monkeypatch, caplog):
    _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong_for({})
    with (caplog.at_level(logging.WARNING, logger='hevolve_social'),
          patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        for n in range(200):
            kong_identity.email_for_token(f'junk{n}')
    refused = [r for r in caplog.records if 'refused' in r.getMessage()]
    assert len(refused) == 1, [r.getMessage() for r in refused][:3]
