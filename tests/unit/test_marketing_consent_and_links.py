"""Marketing autonomy: external-post consent gate + funnel-link health.

Two regressions this pins (both were the reason the flywheel moved 0 strangers):

  1. Every marketing intent pointed at a DEAD landing URL — ``hevolve.ai/download``
     is a 404 (the landing SPA has no such route) and ``nunba.hevolve.ai`` does
     not resolve (DNS dead). So every post the agent could make dead-ended and
     converted nobody. The intents must now resolve to the live installer +
     pricing pages, single-sourced + env-overridable.

  2. The external-post tools PROMISED in their docstrings to "gate every EXTERNAL
     post on operator consent" but never enforced it — the gate was prose the LLM
     could ignore. They must now block (consent_required, no post) without
     standing ``public_exposure`` consent and proceed with it. That grant/revoke
     is the operator's autonomy on/off switch (humans-in-control).

Behavioural — imports the real code, mocks the consent + adapter boundaries,
calls the real tool closures, asserts the observable result. No grep tests.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── 1. Funnel link health ──────────────────────────────────────────

_DEAD_HOST = 'nunba.hevolve.ai'              # DNS dead
_DEAD_EXACT = 'https://hevolve.ai/download'  # 404 — landing SPA has no such route


def test_no_marketing_intent_points_at_a_dead_landing():
    import integrations.marketing.intents as intents
    for it in intents.get_intents():
        blob = f'{it.landing_url} {it.body_text}'
        assert _DEAD_HOST not in blob, (
            f'{it.platform}/{it.code} still points at dead host {_DEAD_HOST}: '
            f'{it.landing_url}')
        assert it.landing_url != _DEAD_EXACT, (
            f'{it.platform}/{it.code} landing is the 404 page')
        # the bare 404 path must not appear even inside the body text
        assert _DEAD_EXACT not in blob, (
            f'{it.platform}/{it.code} body still embeds the 404 download link')
        assert it.landing_url.startswith('https://'), it.landing_url


def test_download_url_is_env_overridable():
    """Single source + env override (mirrors the HEVOLVE_INVITE_BASE_URL pattern)
    so a future tracked ``hevolve.ai/download?ref=`` landing can be switched on
    without a code change."""
    import integrations.marketing.intents as intents
    try:
        with patch.dict(os.environ,
                        {'HEVOLVE_DOWNLOAD_URL': 'https://example.test/dl'}):
            importlib.reload(intents)
            installs = [it for it in intents.get_intents()
                        if it.platform in ('linkedin', 'whatsapp')]
            assert installs, 'expected linkedin/whatsapp install intents'
            assert all('https://example.test/dl' in it.landing_url
                       for it in installs)
    finally:
        importlib.reload(intents)  # restore env-free default for other tests


# ── 2. External-post consent gate ──────────────────────────────────

class _Registrar:
    """Captures the closure tools that register_marketing_tools builds."""

    def __init__(self):
        self.funcs = {}

    def register_for_llm(self, name=None, description=None):
        return lambda f: f

    def register_for_execution(self, name=None):
        def deco(f):
            self.funcs[name] = f
            return f
        return deco


def _marketing_funcs(user_id='system_bootstrap'):
    from integrations.agent_engine.marketing_tools import register_marketing_tools
    reg = _Registrar()
    register_marketing_tools(reg, reg, user_id)
    return reg.funcs


@contextmanager
def _consent(granted: bool):
    """Mock the consent boundary that _external_post_allowed reads."""
    @contextmanager
    def _fake_db():
        yield Mock()

    with patch('integrations.social.models.db_session', _fake_db), \
         patch('integrations.social.consent_service.ConsentService.check_consent',
               Mock(return_value=granted)):
        yield


def test_external_post_blocked_without_consent():
    funcs = _marketing_funcs()
    with _consent(False):
        out = json.loads(funcs['post_to_channel']('twitter', 'hello world'))
    assert out['success'] is False
    assert out['consent_required'] == 'public_exposure'


def test_external_browser_post_blocked_without_consent():
    funcs = _marketing_funcs()
    with _consent(False):
        out = json.loads(funcs['post_to_channel_via_browser']('hackernews', 'hi'))
    assert out['ok'] is False
    assert out['consent_required'] == 'public_exposure'


def test_external_post_proceeds_with_consent():
    """With consent the gate passes — the result is no longer consent_required
    (it falls through to the adapter lookup, which is fine; the point is the gate
    did not block)."""
    funcs = _marketing_funcs()
    with _consent(True):
        out = json.loads(funcs['post_to_channel']('twitter', 'hello world'))
    assert 'consent_required' not in out


def test_browser_post_proceeds_with_consent():
    funcs = _marketing_funcs()
    sentinel = {'ok': True, 'platform': 'hackernews', 'status': 'success'}
    with _consent(True), \
         patch('integrations.marketing.browser_poster.post_to_platform_via_browser',
               Mock(return_value=sentinel)):
        out = json.loads(funcs['post_to_channel_via_browser']('hackernews', 'hi'))
    assert out['ok'] is True
    assert 'consent_required' not in out


def test_internal_platform_post_is_not_consent_gated():
    """create_social_post stays on-platform — it must NOT require public_exposure
    even when external consent is denied."""
    funcs = _marketing_funcs()
    with _consent(False), \
         patch('integrations.social.models.get_db',
               side_effect=RuntimeError('no db in test')):
        out = json.loads(funcs['create_social_post']('title', 'body'))
    assert 'consent_required' not in out


def test_external_post_allowed_fails_closed_on_error():
    """If the consent lookup itself blows up, deny the post (fail-closed)."""
    from integrations.agent_engine.marketing_tools import _external_post_allowed
    with patch('integrations.social.models.db_session',
               side_effect=RuntimeError('db down')):
        assert _external_post_allowed('system_bootstrap') is False


# ── 3. The autonomy switch: boot-time standing grant ───────────────

def test_enable_autonomous_marketing_consent_is_idempotent_and_covers_identities():
    """The operator switch grants public_exposure for every system identity the
    daemon may dispatch as, once — reboots don't stack rows."""
    from integrations.agent_engine import goal_seeding
    granted_calls = []
    existing = set()

    def fake_check(db, uid, ctype, scope='*', agent_id=None):
        return (uid, ctype) in existing

    def fake_grant(db, uid, ctype, scope='*', agent_id=None):
        granted_calls.append((uid, ctype))
        existing.add((uid, ctype))

    with patch('integrations.social.consent_service.ConsentService.check_consent',
               fake_check), \
         patch('integrations.social.consent_service.ConsentService.grant_consent',
               fake_grant):
        n1 = goal_seeding.enable_autonomous_marketing_consent(Mock())
        n2 = goal_seeding.enable_autonomous_marketing_consent(Mock())

    assert n1 == len(goal_seeding._AUTONOMOUS_MARKETING_IDENTITIES)
    assert n2 == 0  # idempotent
    assert {c[1] for c in granted_calls} == {'public_exposure'}
    uids = {c[0] for c in granted_calls}
    assert {'system', 'system_bootstrap'} <= uids


# ── 4. The same switch turns it OFF ────────────────────────────────
#
# Review of e45d8f769 (hartos-87's reviewer, 2026-10-12): the deploy writes
# HEVOLVE_AUTONOMOUS_MARKETING into central's .env on every deploy, and boot
# only ever GRANTED.  Setting the flag to 0 left the standing grant in place,
# and POST /api/social/consent/revoke answers 404 for the system identities
# (it revokes the caller's own rows; nobody signs in as `system`).  So once
# on, posting could not be stopped without code.  Real consent table here,
# not a mocked ConsentService: the property is that check_consent goes False.

@pytest.fixture
def consent_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social.models import UserConsent

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    UserConsent.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    yield db
    db.close()
    engine.dispose()


def _posting_allowed(db):
    from integrations.agent_engine import goal_seeding
    from integrations.social.consent_service import ConsentService
    return {uid: ConsentService.check_consent(db, uid, 'public_exposure', scope='*')
            for uid in goal_seeding._AUTONOMOUS_MARKETING_IDENTITIES}


def _apply(db, flag):
    from integrations.agent_engine import goal_seeding
    env = {k: v for k, v in os.environ.items() if k != 'HEVOLVE_AUTONOMOUS_MARKETING'}
    if flag is not None:
        env['HEVOLVE_AUTONOMOUS_MARKETING'] = flag
    with patch.dict(os.environ, env, clear=True):
        return goal_seeding.apply_autonomous_marketing_flag(db)


def test_flag_on_grants_every_identity(consent_db):
    assert _apply(consent_db, ' True ') == ('granted', 3)
    assert set(_posting_allowed(consent_db).values()) == {True}


@pytest.mark.parametrize('off', ['0', 'false', 'NO', ' off '])
def test_flag_off_after_on_stops_posting(consent_db, off):
    _apply(consent_db, '1')
    assert _apply(consent_db, off) == ('revoked', 3)
    assert set(_posting_allowed(consent_db).values()) == {False}


def test_flag_back_on_after_off_grants_again(consent_db):
    _apply(consent_db, '1')
    _apply(consent_db, '0')
    assert _apply(consent_db, 'yes') == ('granted', 3)
    assert set(_posting_allowed(consent_db).values()) == {True}


@pytest.mark.parametrize('flag', [None, '', 'maybe'])
def test_unset_or_unrecognised_flag_changes_nothing(consent_db, flag):
    """Unset is not OFF: a node that never chose leaves whatever is there."""
    _apply(consent_db, '1')
    assert _apply(consent_db, flag) == (None, 0)
    assert set(_posting_allowed(consent_db).values()) == {True}


def test_flag_off_with_nothing_granted_writes_no_row(consent_db):
    """revoke_consent on an empty combination marks its first row as a
    declined ask; with no row at all it must not invent one per boot."""
    from integrations.social.models import UserConsent
    assert _apply(consent_db, '0') == ('revoked', 0)
    assert consent_db.query(UserConsent).count() == 0
