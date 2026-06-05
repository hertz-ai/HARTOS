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
