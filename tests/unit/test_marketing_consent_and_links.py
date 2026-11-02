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
# Review of "fix(deploy): owner-set keys, the review's fixes" (2026-10-12): the deploy writes
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


def test_flag_off_on_every_later_boot_changes_nothing(consent_db):
    """Central reboots with the flag still 0. Only the first off-boot may
    revoke: later boots must neither re-stamp the history rows (revoke_consent
    on a combination with no active grant marks its first row again) nor
    announce three more revocations."""
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import UserConsent
    _apply(consent_db, '1')
    real = ConsentService.announce_revocation
    with patch.object(ConsentService, 'announce_revocation',
                      Mock(side_effect=real)) as announced:
        assert _apply(consent_db, '0') == ('revoked', 3)
        rows = [(r.id, r.granted, r.revoked_at)
                for r in consent_db.query(UserConsent).order_by(UserConsent.id)]
        assert _apply(consent_db, '0') == ('revoked', 0)
        assert _apply(consent_db, 'off') == ('revoked', 0)
    assert [(r.id, r.granted, r.revoked_at)
            for r in consent_db.query(UserConsent).order_by(UserConsent.id)] == rows
    assert announced.call_count == 3
    assert set(_posting_allowed(consent_db).values()) == {False}


def test_an_off_switch_that_revoked_nothing_and_failed_never_reads_as_off(
        consent_db, caplog):
    """Every revoke raises (database locked): nothing was revoked, so the
    `if n` branch cannot hide it. The boot must say ERROR and must NOT print
    the all-clear "is OFF"; all three identities can still post."""
    import logging
    from integrations.social.consent_service import ConsentService
    caplog.set_level(logging.INFO, logger='hevolve_social')
    _apply(consent_db, '1')
    with patch.object(ConsentService, 'revoke_consent',
                      side_effect=RuntimeError('database is locked')):
        assert _apply(consent_db, '0') == ('revoked', 0)
    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any(lvl == logging.ERROR and 'may continue' in m for lvl, m in messages), messages
    assert not any('is OFF' in m for _, m in messages), messages
    assert set(_posting_allowed(consent_db).values()) == {True}


def test_a_failed_off_switch_is_loud_and_never_reads_as_off(consent_db, caplog):
    """A revoke that raises leaves that identity able to post: the boot must
    say so at ERROR and must NOT also print the all-clear "is OFF" line.
    A clean later off-boot (nothing left to revoke) does print it."""
    import logging
    from integrations.social.consent_service import ConsentService
    caplog.set_level(logging.INFO, logger='hevolve_social')
    _apply(consent_db, '1')
    real = ConsentService.revoke_consent

    def _revoke(db, uid, *a, **kw):
        if uid == 'system':
            raise RuntimeError('database is locked')
        return real(db, uid, *a, **kw)

    with patch.object(ConsentService, 'revoke_consent', side_effect=_revoke):
        assert _apply(consent_db, '0') == ('revoked', 2)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("'system'" in m and 'may continue' in m for m in errors), errors
    assert not any('is OFF' in r.getMessage() for r in caplog.records)
    assert _posting_allowed(consent_db)['system'] is True

    caplog.clear()
    assert _apply(consent_db, '0') == ('revoked', 1)   # the retry clears 'system'
    assert _apply(consent_db, '0') == ('revoked', 0)
    assert any('is OFF' in r.getMessage() for r in caplog.records)
    assert set(_posting_allowed(consent_db).values()) == {False}


def test_flag_off_with_nothing_granted_writes_no_row(consent_db):
    """revoke_consent on an empty combination marks its first row as a
    declined ask; with no row at all it must not invent one per boot."""
    from integrations.social.models import UserConsent
    assert _apply(consent_db, '0') == ('revoked', 0)
    assert consent_db.query(UserConsent).count() == 0


# ── 4. post_to_channel: post through the live registry, or ask the owner ──
#
# Measured on central 2026-11-02: every revenue goal "completed" on model
# spend while no post left the node.  post_to_channel built an extension
# adapter with no configuration and called send_message with arguments no
# adapter takes, and when a channel had no credentials it never asked the
# owner for them.  It now sends through the live channel registry and, for a
# channel that is not connected, asks the owner for each value connecting it
# needs.

class _Adapter:
    """A channel adapter as the registry holds one, with the real
    ChannelAdapter.send_message signature (no **kwargs), so a call with the
    wrong arguments fails here as it does in production."""

    def __init__(self, name, result=None, running=True):
        self.name, self._result, self._running = name, result, running
        self.sent = []

    def on_message(self, handler):
        pass

    def is_running(self):
        return self._running

    async def send_message(self, chat_id, text, reply_to=None, media=None,
                           buttons=None):
        self.sent.append({'chat_id': chat_id, 'text': text, 'media': media})
        return self._result


class _Integration:
    """The two members of FlaskChannelIntegration that post_to_channel
    reads: the real ChannelRegistry, and send_threadsafe, which runs the
    registry's send_to_channel to its SendResult (None while the channel
    loop is not running)."""

    def __init__(self, *adapters, loop_running=True):
        from integrations.channels.registry import ChannelRegistry
        self.registry = ChannelRegistry()
        for adapter in adapters:
            self.registry.register(adapter)
        self._loop_running = loop_running

    def send_threadsafe(self, channel, chat_id, text, *, wait=None, **kwargs):
        if not self._loop_running:
            return None
        # A loop of its own, as the channel thread has: never this thread's
        # current loop, which core.event_loop.run_async reuses.
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.registry.send_to_channel(channel, chat_id, text, **kwargs))
        finally:
            loop.close()


def _post(integration, *args, ask=None, **kwargs):
    """Call the real post_to_channel with consent granted, the channel
    integration and the owner ask (request_credential) as the boundaries."""
    funcs = _marketing_funcs()
    ask = ask or Mock(return_value='Asked the owner of this computer on '
                                   'the consent card.')
    with _consent(True), \
         patch('integrations.channels.flask_integration.get_channel_integration',
               return_value=integration), \
         patch('hartos.ai_key_vault.request_credential', ask):
        return json.loads(funcs['post_to_channel'](*args, **kwargs)), ask


def _sent(**kw):
    from integrations.channels.base import SendResult
    return SendResult(**kw)


def test_a_connected_channel_posts_and_reports_the_channels_answer():
    tg = _Adapter('telegram', _sent(success=True, message_id='m-1'))
    out, ask = _post(_Integration(tg), 'telegram', 'hello',
                     extra_config='{"chat_id": "@hevolve"}')
    assert tg.sent == [{'chat_id': '@hevolve', 'text': 'hello', 'media': None}]
    assert (out['success'], out['sent'], out['message_id']) == (True, True, 'm-1')
    ask.assert_not_called()


def test_a_send_the_channel_refuses_is_reported_as_not_sent():
    tg = _Adapter('telegram', _sent(success=False, error='chat not found'))
    out, _ = _post(_Integration(tg), 'Telegram', 'hello',
                   extra_config='{"chat_id": "@nobody"}')
    assert len(tg.sent) == 1
    assert (out['success'], out['sent'], out['error']) == (
        False, False, 'chat not found')


def test_a_media_url_reaches_the_channel_as_an_attachment():
    from integrations.channels.base import MessageType
    tg = _Adapter('telegram', _sent(success=True))
    _post(_Integration(tg), 'telegram', 'demo',
          media_url='https://x.test/demo.mp4?sig=1',
          extra_config='{"chat_id": "@hevolve"}')
    [media] = tg.sent[0]['media']
    assert (media.url, media.type) == ('https://x.test/demo.mp4?sig=1',
                                       MessageType.VIDEO)


def test_nothing_is_posted_while_the_channel_loop_is_down():
    tg = _Adapter('telegram', _sent(success=True))
    out, _ = _post(_Integration(tg, loop_running=False), 'telegram', 'hello',
                   extra_config='{"chat_id": "@hevolve"}')
    assert tg.sent == []
    assert (out['success'], out['sent']) == (False, False)


def test_a_connected_channel_with_no_target_posts_nothing():
    tg = _Adapter('telegram', _sent(success=True))
    out, ask = _post(_Integration(tg), 'telegram', 'hello')
    assert tg.sent == []
    assert out['sent'] is False and 'chat_id' in out['error']
    ask.assert_not_called()


def test_a_channel_that_is_not_connected_asks_the_owner_for_what_it_needs():
    from integrations.channels.metadata import (
        get_channel_metadata, required_setup_keys)
    needed = required_setup_keys(get_channel_metadata('twitter'))
    assert 'api_key' in needed, needed          # the catalog asks for keys
    out, ask = _post(_Integration(), 'twitter', 'hello')
    asked = [json.loads(c.args[0]) for c in ask.call_args_list]
    assert [a['key_name'] for a in asked] == needed
    assert {a['channel_type'] for a in asked} == {'twitter'}
    assert {a['used_by'] for a in asked} == {'post_to_channel'}
    assert (out['success'], out['sent'], out['needs_owner']) == (
        False, False, True)
    assert 'TWITTER_API_KEY' in out['asked_owner_for']
    assert out['register_channel_config']['api_key'] == '{{secret:TWITTER_API_KEY}}'
    assert 'register_channel' in out['next']


def test_a_registered_channel_that_is_stopped_asks_the_owner_too():
    tw = _Adapter('twitter', _sent(success=True), running=False)
    out, ask = _post(_Integration(tw), 'twitter', 'hello',
                     extra_config='{"chat_id": "123"}')
    assert tw.sent == []
    assert ask.called and out['needs_owner'] is True


def test_the_owners_answer_reaches_the_agent():
    ask = Mock(return_value="The owner of this computer said no to "
                            "providing 'Twitter / X API Key'.")
    out, _ = _post(_Integration(), 'twitter', 'hello', ask=ask)
    assert out['owner_answers'][0].startswith(
        'The owner of this computer said no')


def test_a_channel_with_no_catalog_entry_asks_nothing_and_names_the_browser():
    out, ask = _post(_Integration(), 'linkedin', 'hello')
    ask.assert_not_called()
    assert (out['success'], out['sent']) == (False, False)
    assert 'post_to_channel_via_browser' in out['error']


def test_the_instagram_feed_posts_a_video_as_a_reel_and_an_image_as_a_photo():
    """_publish_instagram_feed and post_to_channel read one video rule
    (_is_video_url); a case or query string does not change the answer."""
    from integrations.agent_engine.marketing_tools import _publish_instagram_feed

    class _Instagram:
        def __init__(self):
            self.calls = []

        async def publish_reel(self, url, caption):
            self.calls.append(('reel', url))
            return _sent(success=True, message_id='r-1')

        async def publish_photo(self, url, caption):
            self.calls.append(('photo', url))
            return _sent(success=True, message_id='p-1')

    ig = _Instagram()
    reel = json.loads(_publish_instagram_feed(
        'u', 'https://x.test/a.MOV?sig=1', 'c', _adapter=ig,
        _consent=lambda _u: True))
    photo = json.loads(_publish_instagram_feed(
        'u', 'https://x.test/a.jpg', 'c', _adapter=ig,
        _consent=lambda _u: True))
    assert ig.calls == [('reel', 'https://x.test/a.MOV?sig=1'),
                        ('photo', 'https://x.test/a.jpg')]
    assert (reel['kind'], reel['media_id']) == ('reel', 'r-1')
    assert (photo['kind'], photo['media_id']) == ('photo', 'p-1')
