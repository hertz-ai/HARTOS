"""P3 defensive / nice-to-have — behavioural tests.

Same discipline as P0/P1/P2: import real code, mock the boundary,
call the function, assert observable side-effects.

Run:
    pytest tests/unit/test_consent_fanout_p3.py -v --noconftest
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@contextmanager
def _snapshot_modules(*names):
    """Snapshot named sys.modules entries on enter; restore on exit.
    Use around any test that does `del sys.modules[X] +
    from X import Y` so the reloaded module doesn't leak past the
    test boundary (which was the root cause of test_phase7c7 and
    test_phase7c_fanout_e2e failing after these P3 tests ran)."""
    saved = {n: sys.modules.get(n) for n in names}
    try:
        yield
    finally:
        for n, original in saved.items():
            if original is not None:
                sys.modules[n] = original
            else:
                sys.modules.pop(n, None)


@contextmanager
def _snapshot_and_restore_services():
    """Back-compat alias for tests that reference the older helper
    name.  Wraps the generic snapshotter with the canonical module."""
    with _snapshot_modules('integrations.social.services'):
        yield


HARTOS_ROOT = r'C:/Users/sathi/PycharmProjects/HARTOS'
NUNBA_ROOT = r'C:/Users/sathi/PycharmProjects/Nunba-HART-Companion'
HEVOLVE_ROOT = r'C:/Users/sathi/PycharmProjects/Hevolve'
IOS_ROOT = r'C:/Users/sathi/StudioProjects/Nunba-Companion-iOS'


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as fh:
        return fh.read()


# ─── P3a: SSE refuses user_id=None broadcasts for personal topics ────
def test_p3a_sse_refuses_userless_broadcast_for_personal_topic():
    """Behavioural: invoke EventBus.emit on a personal topic with no
    user_id in the payload.  broadcast_sse_safe must NOT be called —
    that's the privacy guard.  A warning should be logged instead."""
    with _snapshot_modules('core.platform.events'):
        if 'core.platform.events' in sys.modules:
            del sys.modules['core.platform.events']
        from core.platform import events  # noqa: WPS433
        with patch.object(events, 'broadcast_sse_safe') as broadcast_mock:
            bus = events.EventBus()
            # Personal topic (chat.social) with NO user_id in payload.
            # P3a privacy guard should refuse the SSE leg.
            bus.emit('chat.social', {'type': 'notification', 'msg': 'hi'})

        broadcast_mock.assert_not_called(), (
            "P3a regression: SSE broadcast must be refused when data "
            "has no user_id and topic is not in _SSE_GLOBAL_PREFIXES."
        )


def test_p3a_sse_allows_userless_broadcast_for_global_topic():
    """Sibling check: a topic in the global allowlist (community.*)
    SHOULD broadcast without a user_id — those are intentionally
    org-wide."""
    with _snapshot_modules('core.platform.events'):
        if 'core.platform.events' in sys.modules:
            del sys.modules['core.platform.events']
        from core.platform import events
        with patch.object(events, 'broadcast_sse_safe') as broadcast_mock:
            bus = events.EventBus()
            bus.emit('community.feed', {'post_id': 'p-1', 'body': 'public'})

        broadcast_mock.assert_called_once()
        args, kwargs = broadcast_mock.call_args
        assert args[0] == 'community.feed'
        assert kwargs.get('user_id') is None


def test_p3a_sse_allows_personal_topic_when_user_id_present():
    """The guard only fires when user_id is missing — the normal
    per-user case must keep working."""
    with _snapshot_modules('core.platform.events'):
        if 'core.platform.events' in sys.modules:
            del sys.modules['core.platform.events']
        from core.platform import events
        with patch.object(events, 'broadcast_sse_safe') as broadcast_mock:
            bus = events.EventBus()
            bus.emit('chat.social', {
                'type': 'notification',
                'user_id': 'user-7',
                'msg': 'hi',
            })
        broadcast_mock.assert_called_once()
        _, kwargs = broadcast_mock.call_args
        assert kwargs.get('user_id') == 'user-7'


# ─── P3a expansion (2026-05-29): host/infra telemetry whitelist ──────

import pytest as _pytest


@_pytest.mark.parametrize('topic', [
    'system.health.snapshot',     # ×11393 refused pre-fix
    'system.pressure',
    'system.optimization.applied',
    'resource.mode_changed',
    'resource.proactive_action',
    'model.unloaded',
    'catalog.updated',
    'app.registered',
])
def test_p3a_sse_allows_infra_telemetry_without_user_id(topic):
    """Host/infra telemetry (CPU/RAM/health/model-lifecycle/resource)
    carries NO user or agent identifier and feeds the admin ops
    dashboards — it MUST broadcast without a user_id.  Pre-fix the
    guard refused ~13,200 of these and the real-time admin panels
    went dark (log review 2026-05-29)."""
    with _snapshot_modules('core.platform.events'):
        if 'core.platform.events' in sys.modules:
            del sys.modules['core.platform.events']
        from core.platform import events
        with patch.object(events, 'broadcast_sse_safe') as broadcast_mock:
            bus = events.EventBus()
            bus.emit(topic, {'health_score': 0.9, 'cpu_pct': 40})
        broadcast_mock.assert_called_once(), (
            f"infra telemetry topic {topic!r} must broadcast to SSE "
            f"without a user_id — it has no per-user payload")
        _, kwargs = broadcast_mock.call_args
        assert kwargs.get('user_id') is None


@_pytest.mark.parametrize('topic', [
    'agent.action.completed',     # carries agent_id + goal_id
    'action_state.changed',
    'inference.completed',
    'memory.item_added',
])
def test_p3a_sse_still_refuses_agent_scoped_without_user_id(topic):
    """Regression guard: agent/goal/memory-scoped topics carry an
    agent_id/goal_id and on a multi-tenant node a global broadcast
    would leak cross-user activity metadata.  They MUST stay refused
    until the PUBLISHER stamps the owning user_id — do NOT whitelist
    these as infra-global.  This test fails loudly if someone adds
    'agent.'/'inference.'/'memory.' to _SSE_GLOBAL_PREFIXES."""
    with _snapshot_modules('core.platform.events'):
        if 'core.platform.events' in sys.modules:
            del sys.modules['core.platform.events']
        from core.platform import events
        with patch.object(events, 'broadcast_sse_safe') as broadcast_mock:
            bus = events.EventBus()
            bus.emit(topic, {'agent_id': 'skill.local.x', 'goal_id': 'g1'})
        broadcast_mock.assert_not_called(), (
            f"agent-scoped topic {topic!r} must NOT broadcast without a "
            f"user_id — whitelisting it would leak cross-user metadata "
            f"on a multi-tenant node")


def test_p3a_agent_scoped_broadcasts_when_user_id_present():
    """The agent-scoped topics DO broadcast once the publisher stamps
    the owning user_id (the correct fix path) — per-user routing."""
    with _snapshot_modules('core.platform.events'):
        if 'core.platform.events' in sys.modules:
            del sys.modules['core.platform.events']
        from core.platform import events
        with patch.object(events, 'broadcast_sse_safe') as broadcast_mock:
            bus = events.EventBus()
            bus.emit('agent.action.completed', {
                'agent_id': 'skill.local.x', 'goal_id': 'g1',
                'user_id': 'owner-42',
            })
        broadcast_mock.assert_called_once()
        _, kwargs = broadcast_mock.call_args
        assert kwargs.get('user_id') == 'owner-42'


# ─── P3b: mark_read stamps read_at + mark_dismissed exists ───────────
def test_p3b_mark_read_sets_read_at():
    """Behavioural: stub the DB session + SQLAlchemy event listener,
    call mark_read, assert the update statement includes read_at +
    is_read in the .update() dict."""
    stub_models = MagicMock()
    stub_models.Notification = MagicMock()
    stub_auth = MagicMock()
    with _snapshot_and_restore_services(), patch.dict(sys.modules, {
        'integrations.social.models': stub_models,
        'integrations.social.auth': stub_auth,
    }):
        if 'integrations.social.services' in sys.modules:
            del sys.modules['integrations.social.services']
        from integrations.social import services

        captured_update = {}
        sentinel_now = object()
        # Patch both `func` (so func.now() returns our sentinel) AND
        # `event` (so event.listen doesn't blow up on a MagicMock db).
        with patch.object(services, 'func',
                          MagicMock(now=lambda: sentinel_now)), \
             patch.object(services, 'event', MagicMock()):
            fake_db = MagicMock()
            fake_db.query.return_value.filter.return_value.update.side_effect = (
                lambda d, **kw: captured_update.update(d)
            )
            services.NotificationService.mark_read(
                fake_db, ['n1'], 'user-7')

        # Capture column references BEFORE the snapshot restore so the
        # assertions below resolve against the stub services we used
        # (these MagicMock identities are what landed in captured_update).
        read_at_col = services.Notification.read_at
        is_read_col = services.Notification.is_read

    assert read_at_col in captured_update, (
        "P3b regression: mark_read must include Notification.read_at "
        "in the .update() dict so analytics can answer 'unread since X'."
    )
    assert is_read_col in captured_update, (
        "P3b regression: mark_read must STILL set is_read=True — the "
        "boolean discriminator is what /notifications?unread=true "
        "queries on."
    )


def test_p3b_mark_dismissed_exists_and_sets_dismissed_at():
    """mark_dismissed is a NEW method.  Verify it exists, sets
    dismissed_at, does NOT flip is_read, and fires the cross-device
    fan-out so other devices remove the row from view."""
    stub_models = MagicMock()
    stub_models.Notification = MagicMock()
    stub_auth = MagicMock()
    with _snapshot_and_restore_services(), patch.dict(sys.modules, {
        'integrations.social.models': stub_models,
        'integrations.social.auth': stub_auth,
    }):
        if 'integrations.social.services' in sys.modules:
            del sys.modules['integrations.social.services']
        from integrations.social import services
        assert hasattr(services.NotificationService, 'mark_dismissed'), (
            "P3b regression: mark_dismissed method must exist for "
            "distinguishing 'user dismissed' from 'user read'."
        )

        captured_update = {}
        listeners_registered = []
        fake_event = MagicMock()
        fake_event.listen.side_effect = (
            lambda target, name, fn, **kw: listeners_registered.append(
                (target, name, fn, kw))
        )
        with patch.object(services, 'event', fake_event), \
             patch.object(services, 'func', MagicMock(now=lambda: 'NOW')):
            fake_db = MagicMock()
            fake_db.query.return_value.filter.return_value.update.side_effect = (
                lambda d, **kw: captured_update.update(d)
            )
            services.NotificationService.mark_dismissed(
                fake_db, ['n1', 'n2'], 'user-7')

        dismissed_at_col = services.Notification.dismissed_at
        is_read_col = services.Notification.is_read

    assert dismissed_at_col in captured_update, (
        "P3b: mark_dismissed must set dismissed_at."
    )
    assert is_read_col not in captured_update, (
        "P3b: mark_dismissed must NOT flip is_read — dismissed is a "
        "distinct state.  If you want both, call mark_read separately."
    )
    after_commit = [l for l in listeners_registered if l[1] == 'after_commit']
    assert after_commit, (
        "P3b: mark_dismissed must register after_commit listener for "
        "cross-device fan-out (same pipe mark_read uses)."
    )


def test_p3b_notification_model_exposes_read_at_dismissed_at():
    """The Notification ORM mapping HARTOS uses at runtime must
    surface read_at + dismissed_at columns + include them in
    to_dict so /notifications API responses carry the new state.

    Uses the canonical import path production uses
    (integrations.social.models.Notification) which resolves to
    sql.models.Notification in dev mode (Hevolve_Database installed)
    or _models_local.Notification in standalone mode — both must
    declare the columns or HARTOS hits 'Unknown column' after the
    v53 migration adds them to the DB.  Subprocess isolation avoids
    SQLAlchemy MetaData pollution from earlier tests."""
    code = textwrap.dedent('''
        import sys
        sys.path.insert(0, r'C:/Users/sathi/PycharmProjects/HARTOS')
        sys.path.insert(0, r'C:/Users/sathi/PycharmProjects/Hevolve_Database')
        from integrations.social.models import Notification
        cols = {c.name for c in Notification.__table__.columns}
        assert 'read_at' in cols, f'read_at missing from {cols}'
        assert 'dismissed_at' in cols, f'dismissed_at missing from {cols}'
        inst = Notification(id='x', user_id='u',
                            type='consent.test', message='')
        d = inst.to_dict()
        assert 'read_at' in d, 'read_at missing from to_dict'
        assert 'dismissed_at' in d, 'dismissed_at missing from to_dict'
        print('OK')
    ''')
    p = subprocess.run(
        [sys.executable, '-c', code],
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, (
        f"P3b model check failed in subprocess:\n"
        f"stdout={p.stdout}\nstderr={p.stderr}"
    )
    assert 'OK' in p.stdout


def test_p3b_v53_migration_registered():
    """The custom migration runner needs the v53 block.  Source guard
    (the migration runner side-effects the DB, which we don't want to
    fire in a unit test) — verify the registration is wired."""
    src = _read(f'{HARTOS_ROOT}/integrations/social/migrations.py')
    assert 'SCHEMA_VERSION = 53' in src, (
        "P3b: SCHEMA_VERSION must be bumped to 53 so run_migrations "
        "knows v53 is the current head."
    )
    assert 'if current < 53:' in src, (
        "P3b: migrations.py must include the v53 block."
    )
    assert 'ADD COLUMN read_at DATETIME' in src
    assert 'ADD COLUMN dismissed_at DATETIME' in src


# ─── P3c: APNsTokenStore source guard (Swift) ────────────────────────
def test_p3c_apns_token_store_registers_with_backend():
    """Source guard, NOT a behavioural test.  Honest label: Swift +
    URLSession + UserDefaults aren't unit-testable from Python.
    The behavioural verification happens in
    Nunba-Companion-iOS/ios/NunbaCompanionTests/APNsTokenStoreTests
    (XCTest infrastructure).  Here we only confirm the source contains
    the wire — registerWithBackendIfPossible + POST + the endpoint."""
    path = f'{IOS_ROOT}/ios/NunbaCompanion/Modules/APNsTokenStore.swift'
    if not os.path.exists(path):
        pytest.skip("APNsTokenStore.swift not present")
    src = _read(path)
    assert 'registerWithBackendIfPossible' in src, (
        "P3c: APNsTokenStore must expose registerWithBackendIfPossible "
        "so AppDelegate / token setter can call it."
    )
    assert '/update_fcm_token' in src, (
        "P3c: must POST to /update_fcm_token (canonical Hevolve_Database "
        "endpoint used by Android FCM token registration too)."
    )
    assert 'OnboardingModule.persistedUserId' in src, (
        "P3c: must guard the POST on a persisted user_id so we don't "
        "send a token without a user identity."
    )


# ─── P3d: Hevolve web NotificationsPage routes via deeplink ──────────
def test_p3d_hevolve_web_resolve_link_follows_message_deeplink():
    """Behavioural via Node: extract the resolveLink helper from the
    Hevolve web NotificationsPage source, run it with three inputs,
    assert routing decisions match the P3d contract."""
    page_path = (f'{HEVOLVE_ROOT}/src/components/Social/Notifications/'
                 f'NotificationsPage.js')
    if not os.path.exists(page_path):
        pytest.skip(f"NotificationsPage.js not present at {page_path}")

    js = '''
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
// Extract resolveLink — it lives inside NotificationsPage's body, so
// we slice from "const resolveLink = (notif) =>" to the matching
// "};" line followed by an empty line.
const m = src.match(/const resolveLink = \\(notif\\) => \\{[\\s\\S]+?\\n  \\};/);
if (!m) { console.error('resolveLink not extractable'); process.exit(2); }
const fn = new Function('return ' + m[0].replace(/^const resolveLink = /, ''))();

// 1. notif.link wins (legacy).
const r1 = fn({link: '/legacy/path'});
if (r1 !== '/legacy/path') { console.error('FAIL link winner: ' + r1); process.exit(3); }

// 2. message JSON deeplink wins when no top-level link.
const r2 = fn({
  type: 'consent.channel_pair_code',
  target_id: 'whatsapp',
  message: JSON.stringify({deeplink: '/admin/channels/whatsapp/pair'}),
});
if (r2 !== '/admin/channels/whatsapp/pair') { console.error('FAIL JSON deeplink: ' + r2); process.exit(4); }

// 3. consent.* fallback with no deeplink.
const r3 = fn({
  type: 'consent.channel_pair_code',
  target_id: 'whatsapp',
  message: '{}',
});
if (r3 !== '/admin/consent/whatsapp') { console.error('FAIL consent fallback: ' + r3); process.exit(5); }

// 4. No link, no deeplink, no consent — null.
const r4 = fn({type: 'random.unknown'});
if (r4 !== null) { console.error('FAIL null default: ' + r4); process.exit(6); }

console.log('OK');
'''
    p = subprocess.run(
        ['node', '-e', js, page_path],
        capture_output=True, text=True, timeout=10)
    assert p.returncode == 0, (
        f"P3d behavioural test failed:\nstdout={p.stdout}\n"
        f"stderr={p.stderr}"
    )


def test_p3d_hevolve_web_optimistic_mark_read():
    """The handleClick edit also adds optimistic mark-read (mirrors
    P1-S2 in Nunba landing-page).  Verify the optimistic state
    mutation precedes the await on markRead."""
    page_path = (f'{HEVOLVE_ROOT}/src/components/Social/Notifications/'
                 f'NotificationsPage.js')
    if not os.path.exists(page_path):
        pytest.skip("NotificationsPage.js not present")
    src = _read(page_path)
    # Crude but honest: find the handleClick body, ensure setNotifications
    # is called before the markRead await.
    m = re.search(
        r'const handleClick = async \(notif\) => \{([\s\S]+?)\n  \};', src)
    assert m, "handleClick not located in NotificationsPage.js"
    body = m.group(1)
    set_idx = body.find('setNotifications((prev) =>')
    await_idx = body.find('await notificationsApi.markRead')
    assert set_idx >= 0 and await_idx >= 0, (
        "P3d: handleClick must contain both the optimistic "
        "setNotifications and the markRead await."
    )
    assert set_idx < await_idx, (
        "P3d: optimistic setNotifications must precede await markRead — "
        "otherwise the unread dot flicker is not instant."
    )
