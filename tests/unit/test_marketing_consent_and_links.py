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

Behavioural — imports the real code, mocks only what leaves the node (see section 4),
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


# ── 4. post_to_channel: post where the owner chose, or ask the owner ──
#
# Measured on central 2026-11-02: every revenue goal "completed" on model
# spend while no post left the node.  post_to_channel built an extension
# adapter with no configuration and called send_message with arguments no
# adapter takes, and it never asked the owner for a missing credential.  It
# now sends through the live channel registry to the channel's announcement
# destination, and asks the owner for what is missing: the one credential a
# channel can be connected from (never for one it cannot), or where posts go.
#
# Review of f8fdb9c64 (REJECTED): its tests mocked exactly what changed (the
# destination collector and writer, the person_ask calls, the credential
# answer, an adapter with no status), so none of its findings could show.
# These run the real ones: the destination collector and its one writer on a
# real admin config (a temp file), request_credential and ConsentService on a
# real consent table, a fresh vault, person_ask, register_channel, and the
# channel integration's own send over a running event loop.  The boundaries
# are what reaches outside the node: the platform behind each adapter, the
# live adapter wiring (_wire_live_adapter), the connect watcher
# (_report_when_live), the Liquid UI card push, the notification row with its
# Firebase push, and the consent broadcast.

import asyncio  # noqa: E402
import queue  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from integrations.channels.base import (  # noqa: E402
    ChannelAdapter, ChannelConfig, ChannelStatus, SendResult,
)

OWNER = '10202'
TOKEN = '123:abc-test-token'
TOKEN_NAME = 'TELEGRAM_BOT_TOKEN'


class _Adapter(ChannelAdapter):
    """A channel as the registry holds one, standing in for its platform:
    it records each message it is asked to send.  ``status`` is what the
    adapter reports (it runs only when CONNECTED); ``hold`` keeps a send
    unconfirmed past the caller's wait, as a slow platform does."""

    def __init__(self, channel_name, result=None,
                 status=ChannelStatus.CONNECTED, hold=0.0):
        super().__init__(ChannelConfig())
        self._channel_name = channel_name
        self._result = result or SendResult(success=True, message_id='m-1')
        self.status = status
        self._running = status == ChannelStatus.CONNECTED
        self._hold = hold
        self.sent = []

    @property
    def name(self):
        return self._channel_name

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send_message(self, chat_id, text, reply_to=None, media=None,
                           buttons=None):
        self.sent.append({'chat_id': chat_id, 'text': text, 'media': media})
        if self._hold:
            await asyncio.sleep(self._hold)
        return self._result

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        return True

    async def send_typing(self, chat_id):
        return None

    async def get_chat_info(self, chat_id):
        return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Every store post_to_channel reads and writes, fresh: a consent and
    binding database behind models.get_db, the admin channel config on a
    temp file, a vault on temp files with its own key, this node's owner,
    and a channel integration over a running loop installed as the
    process's own.  Yields what the boundaries saw."""
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from hartos import ai_key_vault
    from integrations.agent_engine import (
        liquid_ui_service, marketing_tools, person_ask)
    from integrations.channels import agent_tools, flask_integration
    from integrations.channels import registry as registry_mod
    from integrations.channels.admin import api as admin_api
    from integrations.channels.registry import ChannelRegistry
    from integrations.social import (
        api_channels, consent_service, models, realtime, services)
    from security import secrets_manager
    from security.secrets_manager import SecretsManager

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    for table in (models.UserConsent, models.UserChannelBinding):
        table.__table__.create(engine)
    monkeypatch.setattr(models, 'get_db',
                        sessionmaker(bind=engine, expire_on_commit=False))
    said = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        said.append((topic, dict(data), msg_id)))

    monkeypatch.setattr(admin_api.AdminAPI, '_config_path',
                        lambda self: str(tmp_path / 'admin_config.json'))
    monkeypatch.setattr(admin_api.AdminAPI, '_legacy_config_path',
                        staticmethod(lambda: str(tmp_path / 'legacy.json')))
    admin = admin_api.AdminAPI()
    monkeypatch.setattr(admin_api, 'get_api', lambda: admin)

    monkeypatch.setattr(secrets_manager, '_SALT_PATH', str(tmp_path / 'salt'))
    monkeypatch.setattr(secrets_manager, '_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'test-only-vault-passphrase')
    for name in (TOKEN_NAME, 'SLACK_APP_TOKEN'):
        monkeypatch.setenv(name, 'x')      # recorded, so it is restored
        monkeypatch.delenv(name)
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    registry = ChannelRegistry()
    integration = flask_integration.FlaskChannelIntegration.__new__(
        flask_integration.FlaskChannelIntegration)
    integration.registry = registry
    integration._loop = loop
    integration._thread = None
    monkeypatch.setattr(registry_mod, '_registry', registry)
    monkeypatch.setattr(flask_integration, '_integration', integration)

    wired, watched, cards, rows = [], [], [], []
    monkeypatch.setattr(api_channels, '_wire_live_adapter',
                        lambda ch, cred: wired.append((ch, cred))
                        or {'success': True})
    # The connect watcher stands in for the platform's answer: a start is
    # watched until the test says how it ended (_watch_ends), as the real
    # one watches until the adapter connects or gives up.
    ends, closing = queue.Queue(), threading.Event()
    real_watch = agent_tools._report_when_live

    def _watch(channel, *a, **k):
        watched.append(channel)
        while not closing.is_set():
            try:
                return ends.get(timeout=0.05)
            except queue.Empty:
                pass
        return False
    monkeypatch.setattr(agent_tools, '_report_when_live', _watch)
    monkeypatch.setattr(liquid_ui_service, 'push_agent_ui',
                        lambda agent_id, card, user_id=None:
                        cards.append((agent_id, dict(card), user_id)) or True)
    monkeypatch.setattr(liquid_ui_service, 'withdraw_agent_ui',
                        lambda ask_id: None)
    monkeypatch.setattr(services.NotificationService, 'create',
                        staticmethod(lambda db, user_id, ntype, **kw:
                                     rows.append((user_id, ntype, kw))))
    monkeypatch.setattr(services.NotificationService, 'mark_read',
                        staticmethod(lambda db, ids, user_id: None))
    monkeypatch.setattr(realtime, 'on_notification', lambda *a, **k: None)

    # The posting identity's standing public_exposure grant (the operator's
    # switch, section 3).
    with models.db_session() as db:
        db.add(models.UserConsent(
            id='public-exposure', user_id='system_bootstrap',
            consent_type='public_exposure', scope='*', granted=True,
            granted_at=datetime.utcnow()))
    # getattr: on the parent these maps do not exist, and its tests must
    # fail on what it does, not on the fixture.
    for name in ('_SELF_CONNECTS', '_DESTINATION_ASKS', '_DESTINATION_SAID'):
        getattr(marketing_tools, name, {}).clear()
    getattr(agent_tools, '_STARTS', {}).clear()
    with person_ask._lock:
        person_ask._asks.clear()

    yield SimpleNamespace(admin=admin, registry=registry,
                          integration=integration, said=said, wired=wired,
                          watched=watched, cards=cards, rows=rows, ends=ends,
                          real_watch=real_watch,
                          config_file=tmp_path / 'admin_config.json')

    closing.set()                 # a start still being watched ends
    _join_watchers()
    # A destination waiter still waiting gets no answer and ends.
    with person_ask._lock:
        for a in person_ask._asks.values():
            a.event.set()
        person_ask._asks.clear()
    _join_destination_waiters()

    async def _drain():           # a held send still sleeping on the loop
        await asyncio.gather(*(t for t in asyncio.all_tasks()
                               if t is not asyncio.current_task()),
                             return_exceptions=True)
    asyncio.run_coroutine_threadsafe(_drain(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()


def _announce_only_key():
    """agent_tools.ANNOUNCE_ONLY_KEY ('announce_only' where it does not exist
    yet, so the parent fails on behaviour, not on the import)."""
    from integrations.channels import agent_tools
    return getattr(agent_tools, 'ANNOUNCE_ONLY_KEY', 'announce_only')


def _join_destination_waiters():
    for t in threading.enumerate():
        if t.name.startswith('post-destination-'):
            t.join(timeout=5)


def _join_watchers():
    for t in threading.enumerate():
        if t.name.startswith('channel-probe-'):
            t.join(timeout=5)


def _watch_ends(world, connected):
    """The start being watched ends: the channel connected with the value,
    or refused it."""
    world.ends.put(connected)
    _join_watchers()


def _watching(world, count=1):
    """The starts being watched, once ``count`` of them are (each watcher
    runs on its own thread)."""
    deadline = time.monotonic() + 5
    while len(world.watched) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    return list(world.watched)


def _post(*args, **kwargs):
    """The real post_to_channel tool, as the marketing agent calls it."""
    return json.loads(_marketing_funcs()['post_to_channel'](*args, **kwargs))


def _destination(world, channel, chat_id):
    """The owner chose ``chat_id`` for ``channel`` on the Channels page."""
    world.admin._channels[channel] = {'channel_type': channel, 'enabled': True,
                                      'announce_chat_id': chat_id}


def _credential_asks():
    from integrations.social.models import UserConsent, db_session
    with db_session(commit=False) as db:
        return [(r.scope, r.agent_id, bool(r.granted)) for r in
                db.query(UserConsent).filter_by(user_id=OWNER,
                                                consent_type='credential')]


def _card_requests(world):
    return [(data, msg_id) for topic, data, msg_id in world.said
            if topic == 'consent.request']


def _owner_enters(value):
    """The owner's Accept on the consent card: the desktop vault keeps the
    value and hands it over (hold_credential), and the grant row says the
    owner entered it (consent_api.grant_consent writes the same row)."""
    from hartos.ai_key_vault import get_ai_key_vault
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    get_ai_key_vault().hold_credential(TOKEN_NAME, value)
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'credential',
                                     'secret:' + TOKEN_NAME)


def _owner_answers(ask_id, chat_id):
    """The owner answers the destination card from any surface."""
    from integrations.agent_engine import person_ask
    body, status = person_ask.answer_ask(
        ask_id, {'values': {'chat_id': chat_id}}, OWNER)
    assert status == 200, body
    _join_destination_waiters()


# ── Posting ─────────────────────────────────────────────────────────

def test_a_connected_channel_posts_where_the_owner_chose(world):
    tg = _Adapter('telegram')
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    _destination(world, 'discord', 'd-9')
    out = _post('telegram', 'hello', extra_config='{"chat_id": "@somebody_else"}')
    assert tg.sent == [{'chat_id': '-1001', 'text': 'hello', 'media': None}]
    assert (out['success'], out['sent'], out['pending']) == (True, True, False)
    assert out['posts'] == [{'chat_id': '-1001', 'sent': True,
                             'message_id': 'm-1', 'error': None}]
    assert _card_requests(world) == [] and world.cards == []


def test_a_send_the_channel_refuses_is_reported_as_not_sent(world):
    tg = _Adapter('telegram', SendResult(success=False, error='chat not found'))
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    out = _post('Telegram', 'hello')
    assert len(tg.sent) == 1
    assert (out['success'], out['sent']) == (False, False)
    assert out['posts'][0]['error'] == 'chat not found'


def test_a_send_not_confirmed_in_time_is_pending_and_never_resent(
        world, monkeypatch):
    from integrations.channels import flask_integration
    monkeypatch.setattr(flask_integration, 'CHANNEL_SEND_WAIT_S', 0.1)
    tg = _Adapter('telegram', hold=0.5)
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    out = _post('telegram', 'hello')
    assert len(tg.sent) == 1                    # one send, no retry
    # Neither sent nor not sent: it may still appear.
    assert (out['success'], out['sent'], out['pending']) == (None, None, True)
    assert out['posts'] == [{'chat_id': '-1001', 'sent': None, 'pending': True}]
    assert 'Do not post it again' in out['note']


def test_a_media_url_reaches_the_channel_as_an_attachment(world):
    from integrations.channels.base import MessageType
    tg = _Adapter('telegram')
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    _post('telegram', 'demo', media_url='https://x.test/demo.MOV?sig=1')
    [media] = tg.sent[0]['media']
    assert (media.url, media.file_path, media.type) == (
        'https://x.test/demo.MOV?sig=1', None, MessageType.VIDEO)


def test_a_url_that_does_not_name_its_type_goes_out_as_a_link(world):
    """A share page or a CDN id is no attachment; the post carries it as a
    link (f8fdb9c64 refused the whole post for it)."""
    tg = _Adapter('telegram')
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    out = _post('telegram', 'demo', media_url='https://x.test/share/abc123')
    assert tg.sent == [{'chat_id': '-1001', 'media': None,
                        'text': 'demo\nhttps://x.test/share/abc123'}]
    assert out['sent'] is True


@pytest.mark.parametrize('name', ['reel.mp4', 'clip#1.mp4', 'take?2.mp4'])
def test_a_file_this_node_made_rides_as_a_file_not_a_url(
        world, tmp_path, name):
    """'#' and '?' are ordinary characters in a file name: cutting at them
    dropped "clip#1.mp4" (review of f8fdb9c64)."""
    from integrations.channels.base import MessageType
    if name == 'take?2.mp4' and sys.platform == 'win32':
        pytest.skip("'?' cannot be in a Windows file name")
    data = tmp_path / 'data'
    data.mkdir()
    clip = data / name
    clip.write_bytes(b'\x00')
    tg = _Adapter('telegram')
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    with patch('core.platform_paths.get_data_dir', return_value=str(data)):
        out = _post('telegram', 'demo', media_url=str(clip))
    [media] = tg.sent[0]['media']
    assert media.url is None and media.type == MessageType.VIDEO
    assert os.path.realpath(media.file_path) == os.path.realpath(str(clip))
    assert out['sent'] is True


def test_a_file_outside_the_data_folder_is_not_attached_and_nothing_posts(
        world, tmp_path):
    outside = tmp_path / 'secret.png'
    outside.write_bytes(b'\x00')
    data = tmp_path / 'data'
    data.mkdir()
    tg = _Adapter('telegram')
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    with patch('core.platform_paths.get_data_dir', return_value=str(data)):
        out = _post('telegram', 'demo', media_url=str(outside))
    assert tg.sent == []
    assert out['sent'] is False and 'data folder' in out['error']


def test_nothing_is_posted_while_the_channel_loop_is_down(world):
    tg = _Adapter('telegram')
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    world.integration._loop = None
    out = _post('telegram', 'hello')
    assert tg.sent == []
    assert (out['success'], out['sent']) == (False, False)


# ── A channel that is not connected ─────────────────────────────────

def test_a_channel_that_can_be_connected_asks_the_owner_once_for_its_value(world):
    first = _post('telegram', 'hello')
    second = _post('telegram', 'hello')
    assert _credential_asks() == [('secret:' + TOKEN_NAME, None, False)]
    requests = _card_requests(world)
    assert {msg_id for _d, msg_id in requests} == {requests[0][1]}  # one card
    assert 'post_to_channel' in requests[0][0]['reason']
    for out in (first, second):
        assert (out['sent'], out['needs_owner']) == (False, True)
        assert out['asked_owner_for'] == TOKEN_NAME
        assert 'Asked the owner' in out['owner_answer']
        assert 'not report it as done' in out['next']
        # The agent is never handed an alias to pass anywhere itself.
        assert '{{secret:' not in json.dumps(out)
    assert '{{secret:' not in requests[0][0]['reason']
    assert world.wired == []


def test_a_registered_channel_that_is_stopped_asks_too(world):
    tg = _Adapter('telegram', status=ChannelStatus.DISCONNECTED)
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    out = _post('telegram', 'hello')
    assert tg.sent == [] and out['needs_owner'] is True
    assert len(_card_requests(world)) == 1


@pytest.mark.parametrize('channel', ['twitter', 'slack'])
def test_a_channel_its_answer_could_never_start_asks_nothing(world, channel):
    """Twitter needs five credentials, and Slack needs SLACK_APP_TOKEN on
    the server: register_channel starts neither from one value, so no card
    is filed (the rejected version filed five for Twitter)."""
    out = _post(channel, 'hello')
    assert _credential_asks() == [] and _card_requests(world) == []
    assert (out['sent'], out['needs_owner'], out['cannot_connect_here']) == (
        False, False, True)


@pytest.mark.parametrize('channel', ['signal', 'whatsapp', 'imessage'])
def test_a_channel_where_no_answer_could_be_used_asks_nothing(world, channel):
    """On a person-to-person channel every destination needs a
    subscription, which the owner cannot give on a card: connecting it or
    asking where posts go only led to answers refused on every post."""
    out = _post(channel, 'hello')
    assert _credential_asks() == [] and _card_requests(world) == []
    assert world.cards == [] and world.wired == []
    assert (out['sent'], out['needs_owner'], out['cannot_post_here']) == (
        False, False, True)


def test_once_the_owner_entered_the_value_the_channel_is_connected_for_them(
        world):
    from security.secrets_manager import SEALED_PREFIX, SecretsManager
    from integrations.social.models import UserChannelBinding, db_session
    _owner_enters(TOKEN)
    out = _post('telegram', 'hello')
    assert world.wired == [('telegram', TOKEN)]
    assert _watching(world) == ['telegram']
    assert (out['sent'], out['connecting']) == (False, True)
    assert TOKEN not in json.dumps(out)          # the model never holds it
    assert _card_requests(world) == []
    # Kept sealed in the admin config and on the owner's binding, as the
    # card said ("kept encrypted on this computer"); f8fdb9c64 wrote the
    # admin copy in plain text.
    kept = world.admin._channels['telegram']['config']['bot_token']
    assert kept.startswith(SEALED_PREFIX)
    assert SecretsManager.get_instance().decrypt_value(kept) == TOKEN
    assert TOKEN not in world.config_file.read_text()
    with db_session(commit=False) as db:
        [binding] = db.query(UserChannelBinding).all()
        assert binding.user_id == OWNER
        assert binding.metadata_json['bot_token'].startswith(SEALED_PREFIX)


def test_a_refused_value_is_never_tried_again_and_the_owner_is_asked_anew(
        world):
    """H2 of the f8fdb9c64 review: the same refused value was registered
    again on every call (a new watcher, an error toast and an unhealthy
    banner each time) and the owner was never asked.  Refused is what the
    watcher saw, not a clock (review of 506a79c1f)."""
    _owner_enters(TOKEN)
    _post('telegram', 'hello')
    still = _post('telegram', 'hello')           # its start is being watched
    assert still['connecting'] is True and world.wired == [('telegram', TOKEN)]
    _watch_ends(world, connected=False)          # the channel refused it
    refused = _post('telegram', 'hello')
    assert world.wired == [('telegram', TOKEN)]  # not registered again
    assert refused.get('value_refused') is True and refused['needs_owner'] is True
    [(card, _msg)] = _card_requests(world)
    assert 'rejected' in card['reason']
    assert '{{secret:' not in json.dumps(refused)
    _owner_enters('456:a-new-value')
    _post('telegram', 'hello')
    assert world.wired == [('telegram', TOKEN), ('telegram', '456:a-new-value')]


def test_a_start_that_failed_is_judged_by_the_real_watcher(world, monkeypatch):
    """The real watcher ends at ERROR, where a failed start ends
    (ChannelAdapter.start), and its verdict makes the value refused; the
    owner sees one error toast for it."""
    from integrations.channels import agent_tools
    from integrations.social import fleet_command
    toasts, banners = [], []
    monkeypatch.setattr(agent_tools, '_report_when_live', world.real_watch)
    monkeypatch.setattr(agent_tools, '_emit_to_user',
                        lambda owner, payload: toasts.append((owner, payload)))
    monkeypatch.setattr(fleet_command, 'emit_channel_unhealthy',
                        lambda db, **kw: banners.append(kw))
    _owner_enters(TOKEN)
    _post('telegram', 'hello')
    world.registry.register(_Adapter('telegram', status=ChannelStatus.ERROR))
    _join_watchers()
    out = _post('telegram', 'hello')
    assert out.get('value_refused') is True and world.wired == [('telegram', TOKEN)]
    assert [(o, p['type']) for o, p in toasts] == [(OWNER, 'toast')]
    assert [b['user_id'] for b in banners] == [OWNER]


def test_a_value_that_connected_and_later_dropped_is_connected_again(world):
    """A value that reached CONNECTED is proven: when the channel later
    drops, it is connected again with it, not treated as refused and asked
    for (review of 506a79c1f)."""
    _owner_enters(TOKEN)
    _post('telegram', 'hello')
    _watch_ends(world, connected=True)
    # ...and later it stopped: registered, not running.
    world.registry.register(_Adapter('telegram', status=ChannelStatus.DISCONNECTED))
    out = _post('telegram', 'hello')
    assert world.wired == [('telegram', TOKEN), ('telegram', TOKEN)]
    assert out.get('connecting') is True and out.get('value_refused') is None
    assert _card_requests(world) == []


def test_a_start_that_could_not_be_scheduled_asks_nothing_and_keeps_no_try(
        world, monkeypatch):
    """register_channel answered without starting anything (here the live
    wiring failed): no value from the owner would change that, so they are
    not asked, and the next post tries again (review of 506a79c1f: it was
    kept as a try, and the value later counted as refused)."""
    from integrations.social import api_channels
    monkeypatch.setattr(api_channels, '_wire_live_adapter',
                        lambda ch, cred: world.wired.append((ch, cred))
                        or {'success': False,
                            'error': 'the channel loop is not running'})
    _owner_enters(TOKEN)
    first = _post('telegram', 'hello')
    assert first.get('cannot_connect_here') is True
    assert first['needs_owner'] is False and first['sent'] is False
    assert 'the channel loop is not running' in first['error']
    assert TOKEN not in json.dumps(first)
    second = _post('telegram', 'hello')
    assert world.wired == [('telegram', TOKEN), ('telegram', TOKEN)]
    assert second.get('cannot_connect_here') is True
    assert world.watched == [] and _card_requests(world) == []


def test_the_owner_entering_the_same_value_again_retries_it_once(world):
    """A fresh entry by the owner is their answer to "it was rejected", even
    when the value is the same: it is tried once more (review of
    506a79c1f: the same value was refused forever)."""
    _owner_enters(TOKEN)
    _post('telegram', 'hello')
    _watch_ends(world, connected=False)
    assert _post('telegram', 'hello').get('value_refused') is True
    _owner_enters(TOKEN)                         # the same value, entered again
    again = _post('telegram', 'hello')
    assert world.wired == [('telegram', TOKEN), ('telegram', TOKEN)]
    assert again.get('connecting') is True
    _watch_ends(world, connected=False)
    third = _post('telegram', 'hello')
    assert third.get('value_refused') is True and len(world.wired) == 2


@pytest.mark.parametrize('running', [True, False])
def test_a_channel_the_owner_turned_off_is_left_alone(world, running):
    """Turned off in its channel settings: nothing is posted, nothing is
    asked, and it is not connected again (register_channel would turn it
    back on).  It was asked about on every post (review of 506a79c1f)."""
    tg = _Adapter('telegram')
    if running:
        world.registry.register(tg)
    world.admin._channels['telegram'] = {'channel_type': 'telegram',
                                         'enabled': False,
                                         'announce_chat_id': '-1001'}
    _owner_enters(TOKEN)
    out = _post('telegram', 'hello')
    assert tg.sent == [] and world.wired == [] and world.cards == []
    assert _card_requests(world) == []
    assert world.admin._channels['telegram']['enabled'] is False
    assert (out['sent'], out.get('needs_owner'), out.get('channel_off')) == (
        False, False, True)


def test_a_channel_still_connecting_is_left_alone(world):
    """register_channel only schedules start(): while the adapter is
    CONNECTING, a second register would rebuild it mid-connect."""
    tg = _Adapter('telegram', status=ChannelStatus.CONNECTING)
    world.registry.register(tg)
    _destination(world, 'telegram', '-1001')
    _owner_enters(TOKEN)
    out = _post('telegram', 'hello')
    assert world.wired == [] and _card_requests(world) == []
    assert tg.sent == [] and (out['sent'], out['connecting']) == (False, True)


def test_an_owner_who_said_no_is_not_asked_again(world):
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    _post('telegram', 'hello')
    with db_session() as db:                      # the card's "Don't allow"
        ConsentService.decline(db, OWNER, 'credential', 'secret:' + TOKEN_NAME)
    world.said.clear()
    out = _post('telegram', 'hello')
    assert _card_requests(world) == [] and world.wired == []
    assert (out['sent'], out['needs_owner'], out['owner_declined']) == (
        False, False, True)
    assert 'do not ask' in out['next'].lower()


def test_a_node_with_no_owner_connects_nothing_and_asks_no_one(
        world, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    out = _post('telegram', 'hello')
    assert _credential_asks() == [] and _card_requests(world) == []
    assert out['needs_owner'] is True and 'no owner' in out['error']
    world.registry.register(_Adapter('telegram'))
    out = _post('telegram', 'hello')
    assert world.cards == [] and out['sent'] is False


# ── Where posts go ──────────────────────────────────────────────────

def test_no_destination_asks_the_owner_where_posts_go_once(world):
    tg = _Adapter('telegram')
    world.registry.register(tg)
    first = _post('telegram', 'hello')
    second = _post('telegram', 'hello')
    [(_agent, card, person)] = world.cards
    assert (person, card['type']) == (OWNER, 'form')
    assert [f['name'] for f in card['fields']] == ['chat_id']
    [(row_person, _type, row)] = world.rows       # one row, one phone push
    assert row_person == OWNER and row['push']['data']['ask_id'] == first['ask_id']
    assert tg.sent == []
    assert (first['needs_owner'], second['ask_id']) == (True, first['ask_id'])


def test_the_owners_answer_is_saved_when_given_and_the_post_goes_out(world):
    """H1 of the f8fdb9c64 review: a channel started from the environment
    has no admin entry, and the answer was dropped as "not registered".
    It is now written the moment it comes, so it survives the ask leaving
    memory before the agent's next call."""
    from integrations.agent_engine import person_ask
    ANNOUNCE_ONLY_KEY = _announce_only_key()
    tg = _Adapter('telegram')
    world.registry.register(tg)
    asked = _post('telegram', 'hello')
    _owner_answers(asked['ask_id'], ' -1009 ')
    entry = world.admin._channels['telegram']
    assert (entry['announce_chat_id'], entry[ANNOUNCE_ONLY_KEY]) == ('-1009', True)
    assert '-1009' in world.config_file.read_text()
    with person_ask._lock:                        # a restart, or an hour later
        person_ask._asks.clear()
    out = _post('telegram', 'hello')
    assert tg.sent == [{'chat_id': '-1009', 'text': 'hello', 'media': None}]
    assert out['sent'] is True and len(world.cards) == 1


def test_a_refused_answer_is_named_in_the_next_question(world):
    tg = _Adapter('telegram')
    world.registry.register(tg)
    asked = _post('telegram', 'hello')
    _owner_answers(asked['ask_id'], '12345')      # a private chat
    assert 'telegram' not in world.admin._channels
    again = _post('telegram', 'hello')
    assert again['ask_id'] != asked['ask_id'] and len(world.cards) == 2
    assert 'could not be used' in world.cards[1][1]['description']
    assert 'one-to-one' in world.cards[1][1]['description']
    assert tg.sent == []


def test_a_channel_with_no_catalog_entry_asks_nothing_and_names_the_browser(
        world):
    out = _post('linkedin', 'hello')
    assert _card_requests(world) == [] and world.cards == []
    assert (out['success'], out['sent']) == (False, False)
    assert 'post_to_channel_via_browser' in out['error']


def test_an_entry_that_only_holds_a_destination_is_never_activated_at_boot():
    """The channel runs from the environment or a saved binding; activated
    from its admin entry it would be built with no credential."""
    from hartos import hartos_bootstrap
    ANNOUNCE_ONLY_KEY = _announce_only_key()
    channels = Mock()
    channels.registry._adapters = {}
    channels.register_channel.return_value = True
    admin = Mock()
    admin._channels = {
        'telegram': {'channel_type': 'telegram', 'enabled': True,
                     'announce_chat_id': '-1009', ANNOUNCE_ONLY_KEY: True},
        'discord': {'channel_type': 'discord', 'enabled': True, 'token': 'd'},
    }
    with patch('integrations.channels.flask_integration.init_channels',
               return_value=channels), \
         patch('integrations.channels.admin.api.get_api', return_value=admin):
        hartos_bootstrap._init_channel_adapters(Mock(), {})
    registered = [c.args[0] for c in channels.register_channel.call_args_list]
    assert registered == ['discord', 'web'], registered


# ── Other senders on the same channels ──────────────────────────────

def test_send_to_channel_says_an_unconfirmed_message_may_still_arrive(
        world, monkeypatch):
    from integrations.channels import flask_integration
    from integrations.channels.agent_tools import build_channel_tool_closures
    monkeypatch.setattr(flask_integration, 'CHANNEL_SEND_WAIT_S', 0.1)
    tg = _Adapter('telegram', hold=0.5)
    world.registry.register(tg)
    send = next(t[2] for t in build_channel_tool_closures({'user_id': OWNER})
                if t[0] == 'send_to_channel')
    said = send('telegram', 'Your order is ready', '-1001')
    assert len(tg.sent) == 1
    assert 'may still arrive' in said and 'Do not send it again' in said


def test_a_journey_message_not_confirmed_is_not_sent_on_another_channel(
        world, monkeypatch):
    from integrations.agent_engine.journey_engine import JourneyEngine
    from integrations.channels import flask_integration
    monkeypatch.setattr(flask_integration, 'CHANNEL_SEND_WAIT_S', 0.1)
    tg = _Adapter('telegram', hold=0.5)
    discord = _Adapter('discord')
    world.registry.register(tg)
    world.registry.register(discord)
    engine = JourneyEngine()
    monkeypatch.setattr(engine, 'get_channels_for_stage',
                        lambda stage: ['email', 'telegram', 'discord'])
    prospect = {'id': 'p1', 'email': 'ana@shop.example',
                'channel_ids': {'telegram': 'tg-ana', 'discord': 'dc-ana'}}
    assert engine.send_via_channel(prospect, 'hi', 'telegram') == {
        'success': None, 'pending': True, 'channel': 'telegram'}
    tg.sent.clear()
    engine._try_alternate_channel(prospect)
    assert len(tg.sent) == 1 and discord.sent == []
    assert prospect['alternate_channel_tried'] == 'telegram'


# ── Instagram feed and the channel registration guard ───────────────

class _Instagram(_Adapter):
    def __init__(self):
        super().__init__('instagram')
        self.calls = []

    async def publish_reel(self, url, caption):
        self.calls.append(('reel', url))
        return SendResult(success=True, message_id='r-1')

    async def publish_photo(self, url, caption):
        self.calls.append(('photo', url))
        return SendResult(success=True, message_id='p-1')


def test_the_instagram_feed_posts_through_the_running_instagram_channel(world):
    """It built a fresh adapter that nothing had connected, so every call
    answered "Not connected" (review of f8fdb9c64)."""
    from integrations.agent_engine.marketing_tools import _publish_instagram_feed
    ig = _Instagram()
    world.registry.register(ig)
    reel = json.loads(_publish_instagram_feed(
        'u', 'https://x.test/a.MOV?sig=1', 'c', _consent=lambda _u: True))
    photo = json.loads(_publish_instagram_feed(
        'u', 'https://x.test/a.jpg', 'c', _consent=lambda _u: True))
    assert ig.calls == [('reel', 'https://x.test/a.MOV?sig=1'),
                        ('photo', 'https://x.test/a.jpg')]
    assert (reel['kind'], reel['media_id'], reel['success']) == ('reel', 'r-1', True)
    assert (photo['kind'], photo['media_id']) == ('photo', 'p-1')


def test_the_instagram_feed_with_instagram_not_running_posts_nothing(world):
    from integrations.agent_engine.marketing_tools import _publish_instagram_feed
    out = json.loads(_publish_instagram_feed(
        'u', 'https://x.test/a.jpg', 'c', _consent=lambda _u: True))
    assert out['success'] is False and out.get('sent') is False


def test_the_instagram_feed_posts_nothing_on_a_channel_the_owner_turned_off(
        world):
    from integrations.agent_engine.marketing_tools import _publish_instagram_feed
    ig = _Instagram()
    world.registry.register(ig)
    world.admin._channels['instagram'] = {'channel_type': 'instagram',
                                          'enabled': False}
    out = json.loads(_publish_instagram_feed(
        'u', 'https://x.test/a.jpg', 'c', _consent=lambda _u: True))
    assert ig.calls == [] and out.get('channel_off') is True


def test_activate_refuses_an_entry_that_only_holds_a_destination(
        world, monkeypatch):
    """POST /api/admin/channels/<type>/activate registered an adapter from
    the admin entry: for an entry that only holds the announcement target,
    that is an adapter with no credential replacing the running one (boot
    already skips such entries; review of 506a79c1f)."""
    import flask
    from integrations.channels.admin import api as admin_api
    ANNOUNCE_ONLY_KEY = _announce_only_key()
    world.admin._channels['telegram'] = {
        'channel_type': 'telegram', 'enabled': True,
        'announce_chat_id': '-1009', ANNOUNCE_ONLY_KEY: True}
    world.admin._channels['discord'] = {'channel_type': 'discord',
                                        'enabled': True, 'token': 'd-token'}
    register = Mock(return_value=False)
    monkeypatch.setattr(world.integration, 'register_channel', register)
    with flask.Flask(__name__).test_request_context(method='POST'):
        refused = admin_api.activate_channel('telegram')
        admin_api.activate_channel('discord')
    assert [c.args[0] for c in register.call_args_list] == ['discord']
    assert isinstance(refused, tuple), refused
    body, status = refused
    assert status == 400
    assert 'announcement destination' in body.get_json()['error']


def test_registering_the_channel_drops_the_destination_only_mark(world):
    """Once the channel has its own config, boot activates its entry."""
    from integrations.channels.agent_tools import (
        build_channel_tool_closures, set_announcement_target)
    ANNOUNCE_ONLY_KEY = _announce_only_key()
    world.registry.register(_Adapter('telegram'))
    set_announcement_target('telegram', '-1009', OWNER)
    assert world.admin._channels['telegram'][ANNOUNCE_ONLY_KEY] is True
    register = next(t[2] for t in build_channel_tool_closures({'user_id': OWNER})
                    if t[0] == 'register_channel')
    register('telegram', json.dumps({'bot_token': TOKEN}))
    entry = world.admin._channels['telegram']
    assert ANNOUNCE_ONLY_KEY not in entry
    assert entry['announce_chat_id'] == '-1009' and entry['enabled'] is True


def test_the_journey_tool_says_not_to_send_a_pending_message_again(
        world, monkeypatch):
    """The agent read a pending send as a failure and sent it again
    (review of 506a79c1f)."""
    import core.labeled_autogen_function as labeled
    from integrations.agent_engine import journey_engine, outreach_crm_tools
    from integrations.channels import flask_integration
    monkeypatch.setattr(flask_integration, 'CHANNEL_SEND_WAIT_S', 0.1)
    tg = _Adapter('telegram', hold=0.5)
    world.registry.register(tg)
    monkeypatch.setattr(outreach_crm_tools, '_load_prospects', lambda: {
        'prospects': {'p1': {'id': 'p1', 'journey_stage': 'outreach',
                             'channel_ids': {'telegram': 'tg-ana'}}}})
    funcs = {}
    monkeypatch.setattr(labeled, 'register_labeled_function',
                        lambda func, **kw: funcs.setdefault(func.__name__, func))
    journey_engine.register_journey_tools(None, None, OWNER)
    out = json.loads(funcs['send_prospect_message']('p1', 'hi', 'telegram'))
    assert len(tg.sent) == 1 and out['pending'] is True
    assert 'Do not send it again' in out.get('note', '')


def test_register_channel_never_saves_an_alias_the_owner_has_not_entered():
    """An unresolved {{secret:NAME}} is a credential nobody entered;
    saved, it became the token of a dead adapter."""
    from integrations.channels.agent_tools import build_channel_tool_closures
    register = next(t[2] for t in build_channel_tool_closures({'user_id': OWNER})
                    if t[0] == 'register_channel')
    with patch('integrations.channels.admin.api.get_api') as get_api:
        said = register('telegram',
                        '{"bot_token": "{{secret:TELEGRAM_BOT_TOKEN}}"}')
    get_api.assert_not_called()
    assert 'was not saved' in said and 'TELEGRAM_BOT_TOKEN' in said
