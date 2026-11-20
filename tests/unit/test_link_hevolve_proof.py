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

from core.session_cache import TTLCache
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
    def fresh():
        # Every store kong_identity keeps, found by type, so a run against
        # the parent commit (which kept one more) starts clean too.
        for kept in vars(kong_identity).values():
            if isinstance(kept, TTLCache):
                kept.clear()
        for report in ('_refusals', '_paced_report'):  # parent's, this one's
            getattr(kong_identity, report, {}).update(logged_at=0.0, count=0)
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


def test_a_paced_link_says_busy_not_unproven(client):
    """Review rv_5local of 787964a11: a lookup the budget refused was
    answered as if Kong had said no.  Nothing is known about the token
    then, so the door says to try again and when, never "not proven"."""
    busy = getattr(kong_identity, 'KongBusy', None)
    assert busy is not None, 'nothing can say the check is busy'
    with patch.object(kong_identity, 'email_for_token', side_effect=busy(20)):
        resp = _link(client, OWNER, token='kong-token')
    assert resp.status_code == 503, resp.get_json()
    assert resp.headers.get('Retry-After') == '20'
    body = resp.get_json()
    assert 'data' not in body
    assert body['code'] == 'login_check_busy'
    assert body['error'] != body['code']
    assert ' ' in body['error'] and '20 seconds' in body['error']
    # One sentence for both doors (the gate's test pins the same).
    assert body['error'] == busy(20).message


def test_a_busy_check_has_one_sentence_and_one_code():
    """KongBusy is the one home of the busy answer's words and code, so the
    gate and link-hevolve cannot drift apart."""
    busy = kong_identity.KongBusy(20)
    assert busy.code == 'login_check_busy'
    assert busy.message != busy.code
    assert busy.message.endswith('Try again in 20 seconds.')


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


# Kong's answer is kept for a minute, never past the token's lease.  Review
# rv_5local of 224e10f3f: asking Kong on every request cost every signed-in
# turn 3 Admin round trips, and a Kong outage right after a proven turn
# refused the turns the kept minute had served.  A revoked token is taken
# for at most that minute, no wider than live, where link-hevolve swaps the
# token for a HARTOS JWT that lives 30 days (auth.TOKEN_EXPIRY).

_app = Flask('kong_requests')


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


def test_a_signed_in_phone_costs_kong_once_a_minute_not_once_a_turn(
        monkeypatch):
    """Twenty turns of one phone inside a minute, each its own request:
    one Kong lookup (3 Admin calls), not 3 per turn."""
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    now = 1_800_000_000.0
    clock = {'t': now}
    monkeypatch.setattr(kong_identity.time, 'time', lambda: clock['t'])
    get, calls = _kong()
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        for turn in range(20):
            clock['t'] = now + turn * 2.5
            with _app.test_request_context('/chat'):
                assert kong_identity.email_for_token('live-token') == OWNER
    assert len(calls) == 3, f'{len(calls)} Kong Admin calls for 20 turns'


def test_kong_down_after_a_proven_turn_keeps_serving_for_the_minute(
        monkeypatch):
    """Kong's Admin API goes down right after the phone was proven: the
    phone's next turns inside the minute are served from the kept answer,
    and Kong is not asked; past the minute Kong is asked again."""
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    now = 1_800_000_000.0
    clock = {'t': now}
    monkeypatch.setattr(kong_identity.time, 'time', lambda: clock['t'])
    get, calls = _kong()
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') == OWNER
    down = []

    def kong_down(url, timeout=None):
        down.append(url)
        raise requests.ConnectionError('refused')
    with patch.object(kong_identity.requests, 'get', side_effect=kong_down):
        for second in (1, 10, 20, 30, 45, 59):
            clock['t'] = now + second
            with _app.test_request_context('/chat'):
                assert kong_identity.email_for_token('live-token') == OWNER
        assert down == [], 'a kept answer asked Kong'
        clock['t'] = now + 61
        assert kong_identity.email_for_token('live-token') is None
    assert len(down) == 1


def test_a_token_revoked_in_kong_is_refused_once_its_kept_answer_ends(
        monkeypatch):
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    now = 1_800_000_000.0
    clock = {'t': now}
    monkeypatch.setattr(kong_identity.time, 'time', lambda: clock['t'])
    rows = {'row': dict(_LIVE_ROW)}

    def get(url, timeout=None):
        if '/oauth2_tokens/' in url:
            row = rows['row']
            if row is None:
                return SimpleNamespace(status_code=404, json=lambda: {})
            return SimpleNamespace(status_code=200, json=lambda: row)
        body = ({'consumer': {'id': 'cons-1'}} if '/oauth2/' in url
                else {'username': OWNER})
        return SimpleNamespace(status_code=200, json=lambda: body)
    with patch.object(kong_identity.requests, 'get', side_effect=get):
        assert kong_identity.email_for_token('live-token') == OWNER
        rows['row'] = None  # revoked out of band, in Kong itself
        clock['t'] = now + 30
        assert kong_identity.email_for_token('live-token') == OWNER
        clock['t'] = now + 61
        assert kong_identity.email_for_token('live-token') is None


def test_kongs_no_is_kept_one_minute_by_the_clock(monkeypatch):
    """Kong's no is kept 60 s (a junk token costs one lookup) and no longer,
    however often the token is presented meanwhile.  Measured for the
    reviewer: a token Kong said no to and then knew would be refused until
    that minute ends, where live asks again next request.  A phone gets its
    token only after Kong's /oauth2/token minted it (Hevolve_Database
    sql/otp.py get_access_token), so this needs Kong's Admin API to miss a
    row its own mint already returned."""
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    now = 1_800_000_000.0
    clock = {'t': now}
    monkeypatch.setattr(kong_identity.time, 'time', lambda: clock['t'])
    known = {'yes': False}

    def get(url, timeout=None):
        if '/oauth2_tokens/' in url and not known['yes']:
            return SimpleNamespace(status_code=404, json=lambda: {})
        if '/oauth2_tokens/' in url:
            return SimpleNamespace(status_code=200, json=lambda: _LIVE_ROW)
        body = ({'consumer': {'id': 'cons-1'}} if '/oauth2/' in url
                else {'username': OWNER})
        return SimpleNamespace(status_code=200, json=lambda: body)
    with patch.object(kong_identity.requests, 'get', side_effect=get) as asked:
        assert kong_identity.email_for_token('live-token') is None
        known['yes'] = True
        for second in (1, 30, 59):  # presented again inside the minute
            clock['t'] = now + second
            assert kong_identity.email_for_token('live-token') is None
        assert asked.call_count == 1
        clock['t'] = now + 61
        assert kong_identity.email_for_token('live-token') == OWNER


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


# ── One gateway address for every phone ──
# hartos-73 via hevolve-66e6, 2026-11-20: central's TRUSTED_PROXY is unset,
# and docker's userland proxy hands HARTOS 172.21.0.1 for every peer
# (core.auth_local.client_address), so a budget per client address was ONE
# budget for every phone: one caller's junk Bearers refused every Hevolve
# login until it refilled.  Review rv_5local of 787964a11: the budget for
# the whole node that replaced it let 61 distinct junk Bearers refuse the
# next first login, and held 80 first logins in a minute to 60.

CENTRAL_PEER = '172.21.0.1'
_central = Flask('central')


def _production_limits(monkeypatch):
    """The Kong budgets as a production node has them; tests otherwise run
    with the limiter relaxed (rate_limiter._build_limits)."""
    from integrations.social import rate_limiter
    monkeypatch.delenv('SOCIAL_RATE_LIMIT_DISABLED', raising=False)
    monkeypatch.delenv('FLASK_ENV', raising=False)
    production = rate_limiter._build_limits()
    # Every Kong budget production has (the parent commit had one more).
    for name, cfg in production.items():
        if name.startswith('kong_'):
            monkeypatch.setitem(rate_limiter.LIMITS, name, cfg)
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
    _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, asked = _kong_for({'phone-token': OWNER})
    with (patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        assert kong_identity.email_for_token('phone-token') == OWNER
        kong_identity._answers.clear()  # its kept answer lapses
        for n in range(200):
            assert kong_identity.email_for_token(f'junk{n}') is None
        before = len(asked)
        for n in range(200):  # the same junk again: Kong's no is kept
            assert kong_identity.email_for_token(f'junk{n}') is None
        assert len(asked) == before, 'a junk token Kong refused was asked again'
        assert kong_identity.email_for_token('phone-token') == OWNER, (
            'junk Bearers refused a phone Kong had proven')
    junk = sum(1 for url in asked if '/oauth2_tokens/junk' in url)
    assert junk == 200, f'{junk} Kong lookups for 200 distinct junk tokens'


def test_junk_bearers_never_refuse_a_new_phones_first_login(monkeypatch):
    """Review rv_5local of 787964a11: 61 distinct junk Bearers spent the
    node's new-token budget, and the next phone's FIRST sign-in got 401.
    The live build has no such budget and proves that phone."""
    _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong_for({'new-phone': OWNER})
    with (patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        for n in range(61):
            assert kong_identity.email_for_token(f'junk{n}') is None
        assert kong_identity.email_for_token('new-phone') == OWNER, (
            'junk Bearers refused a first login')


def test_eighty_first_logins_in_a_minute_all_pass(monkeypatch):
    """Review rv_5local of 787964a11: 80 first logins in one minute (a
    restart forgets every phone) proved 60; the live build proves all 80.
    All 80 come through the one gateway address, in one instant."""
    _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    real = {f'phone{n}': f'user{n}@example.com' for n in range(80)}
    get, _ = _kong_for(real)
    with (patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        got = [kong_identity.email_for_token(token) for token in real]
    assert got == list(real.values())


def test_one_tokens_retries_are_paced_on_their_own(monkeypatch):
    """While Kong errors on one token, that token's retries spend its own
    budget.  Past it Kong is not asked and the answer is busy, with when to
    try again, never "not proven"; another phone is still asked about."""
    limits = _production_limits(monkeypatch)
    busy = getattr(kong_identity, 'KongBusy', None)
    assert busy is not None, 'a paced ask has no answer of its own'
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
        outcomes = []
        for _ in range(30):
            try:
                outcomes.append(kong_identity.email_for_token('phone-a'))
            except busy as paced:
                outcomes.append(paced.retry_after)
        assert kong_identity.email_for_token('phone-b') == 'b@example.com'
    allowed = limits['kong_lookup']['max_tokens']
    # One ask proved the token and one found Kong failing, both unpaced;
    # each retry after that spends the token's own budget, and past it the
    # answer is busy for 20 s.
    assert _token_asks(asked, 'phone-a') == 2 + allowed
    assert outcomes == [None] * (1 + allowed) + [20] * (29 - allowed)


def test_junk_tokens_leave_no_budget_behind(monkeypatch):
    """Each distinct junk token is asked once and Kong's no is kept; none
    of them makes per-token budget state, so a flood of them grows nothing
    but the bounded store of answers.  The live build keeps no state."""
    _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong_for({})
    with (patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        for n in range(500):
            assert kong_identity.email_for_token(f'junk{n}') is None
    paced = [k for k in get_limiter()._buckets if k.endswith(':kong_lookup')]
    assert paced == [], f'{len(paced)} budget entries left by 500 junk tokens'


def test_a_retry_storm_is_reported_once(monkeypatch, caplog):
    """Asks a token's budget paces are logged once a minute with a count,
    not once per request, and in words that say pacing: no credential was
    refused (review rv_4e62).  Junk tokens are never paced, so 200 of them
    add no line."""
    _production_limits(monkeypatch)
    monkeypatch.setenv('KONG_ADMIN_URL', ADMIN)
    get, _ = _kong_for({}, failing={'phone-a'})
    with (caplog.at_level(logging.WARNING, logger='hevolve_social'),
          patch.object(kong_identity.requests, 'get', side_effect=get),
          _from_central()):
        for n in range(200):
            kong_identity.email_for_token(f'junk{n}')
        for _ in range(30):
            try:
                kong_identity.email_for_token('phone-a')
            except Exception:
                pass
    paced = [r for r in caplog.records if 'paced' in r.getMessage()]
    assert len(paced) == 1, [r.getMessage() for r in caplog.records][:3]
    assert not [r for r in caplog.records if 'refused' in r.getMessage()], \
        'a paced ask was logged as a refusal'
