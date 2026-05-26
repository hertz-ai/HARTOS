"""P1 cross-device notification state sync — behavioural tests.

Mirror of test_consent_fanout_p0.py's discipline: import the real
code, mock the boundary, call the function, assert observable
side-effects.

Run:
    pytest tests/unit/test_consent_fanout_p1.py -v --noconftest
"""
from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


# ─── P1-S1: on_notification_read publishes via WAMP + SSE ────────────
def test_p1s1_on_notification_read_publishes_chat_social_and_sse():
    """Call the real on_notification_read with the publisher mocked,
    assert both publish_event and broadcast_sse_safe were called with
    the canonical topic/event name + the expected payload shape."""
    stub_events = MagicMock()
    with patch.dict(sys.modules, {'core.platform.events': stub_events}):
        # Fresh import so the mocked events module wins.
        import importlib
        if 'integrations.social.realtime' in sys.modules:
            del sys.modules['integrations.social.realtime']
        from integrations.social import realtime  # noqa: WPS433

        with patch.object(realtime, 'publish_event') as mp:
            realtime.on_notification_read('user-42', ['n1', 'n2'])

        # WAMP fan-out: chat.social topic, payload with type + ids
        mp.assert_called_once()
        args, kwargs = mp.call_args
        assert args[0] == 'chat.social', (
            f"P1-S1: expected publish_event topic 'chat.social' but "
            f"got {args[0]!r}"
        )
        payload = args[1]
        assert payload['type'] == 'notification.read'
        assert payload['ids'] == ['n1', 'n2']
        assert payload['user_id'] == 'user-42'
        assert kwargs.get('user_id') == 'user-42'

        # SSE fan-out via the patched core.platform.events.
        stub_events.broadcast_sse_safe.assert_called_once()
        sse_args, sse_kwargs = stub_events.broadcast_sse_safe.call_args
        assert sse_args[0] == 'notification.read', (
            f"P1-S1: expected SSE event name 'notification.read' but "
            f"got {sse_args[0]!r}"
        )
        assert sse_args[1] == {'user_id': 'user-42', 'ids': ['n1', 'n2']}
        assert sse_kwargs.get('user_id') == 'user-42'


def test_p1s1_on_notification_read_silent_noop_on_empty():
    """Edge case: empty ids should not publish anything — clients
    shouldn't see a phantom 'mark zero rows' event."""
    stub_events = MagicMock()
    with patch.dict(sys.modules, {'core.platform.events': stub_events}):
        if 'integrations.social.realtime' in sys.modules:
            del sys.modules['integrations.social.realtime']
        from integrations.social import realtime
        with patch.object(realtime, 'publish_event') as mp:
            realtime.on_notification_read('user-42', [])
            realtime.on_notification_read('user-42', None)
        mp.assert_not_called()
        stub_events.broadcast_sse_safe.assert_not_called()


# ─── P1-S1: mark_read / mark_all_read call on_notification_read ──────
def test_p1s1_mark_read_registers_after_commit_callback():
    """mark_read must register an after_commit hook that fires
    on_notification_read.  Behavioural: patch the SQLAlchemy event
    listener, call mark_read, capture the listener registration."""
    stub_models = MagicMock()
    stub_models.Notification = MagicMock()
    stub_auth = MagicMock()
    with patch.dict(sys.modules, {
        'integrations.social.models': stub_models,
        'integrations.social.auth': stub_auth,
    }):
        if 'integrations.social.services' in sys.modules:
            del sys.modules['integrations.social.services']
        from integrations.social import services

        listeners_registered = []
        fake_event = MagicMock()
        fake_event.listen.side_effect = (
            lambda target, name, fn, **kw: listeners_registered.append(
                (target, name, fn, kw))
        )
        with patch.object(services, 'event', fake_event):
            fake_db = MagicMock()
            services.NotificationService.mark_read(
                fake_db, ['n1', 'n2'], 'user-42')

        # After-commit listener must have been registered with the
        # caller's session and the canonical SQLAlchemy event name.
        after_commit_listeners = [
            l for l in listeners_registered if l[1] == 'after_commit'
        ]
        assert after_commit_listeners, (
            "P1-S1: mark_read must register an after_commit listener "
            "to fan out the read-state change."
        )
        # And the callback must call on_notification_read when invoked.
        cb = after_commit_listeners[0][2]
        # Patch the realtime module so the callback's lazy import lands
        # on our stub.
        stub_realtime = MagicMock()
        with patch.dict(sys.modules,
                        {'integrations.social.realtime': stub_realtime}):
            cb(MagicMock())  # invoke the after_commit fn
        stub_realtime.on_notification_read.assert_called_once_with(
            'user-42', ['n1', 'n2'])


def test_p1s1_mark_all_read_collects_ids_before_flipping():
    """mark_all_read must collect the affected ids BEFORE the bulk
    update (otherwise the after_commit hook can't tell who to notify)."""
    stub_models = MagicMock()
    stub_auth = MagicMock()
    with patch.dict(sys.modules, {
        'integrations.social.models': stub_models,
        'integrations.social.auth': stub_auth,
    }):
        if 'integrations.social.services' in sys.modules:
            del sys.modules['integrations.social.services']
        from integrations.social import services

        fake_db = MagicMock()
        # The select-then-update pattern: query returns N rows
        row1, row2 = MagicMock(id='n7'), MagicMock(id='n8')
        fake_db.query.return_value.filter.return_value.all.return_value = (
            [row1, row2]
        )

        listeners_registered = []
        fake_event = MagicMock()
        fake_event.listen.side_effect = (
            lambda target, name, fn, **kw: listeners_registered.append(
                (target, name, fn, kw))
        )
        with patch.object(services, 'event', fake_event):
            services.NotificationService.mark_all_read(fake_db, 'user-42')

        # Listener must have been registered.
        after_commit_listeners = [
            l for l in listeners_registered if l[1] == 'after_commit'
        ]
        assert after_commit_listeners, (
            "P1-S1: mark_all_read must also register the after_commit "
            "fan-out — 'Mark all read' should propagate too."
        )
        cb = after_commit_listeners[0][2]
        stub_realtime = MagicMock()
        with patch.dict(sys.modules,
                        {'integrations.social.realtime': stub_realtime}):
            cb(MagicMock())
        stub_realtime.on_notification_read.assert_called_once_with(
            'user-42', ['n7', 'n8'])


# ─── P1-S2 / P1-S3: JS behaviour via Node sandbox ────────────────────
def _node_assert(js: str, *, label: str, files: dict | None = None) -> None:
    """Run a Node one-shot script.  files = {var_name: absolute_path}
    gets prepended to argv so the script can read the actual JS source
    files."""
    args = ['node', '-e', js]
    if files:
        args.extend(files.values())
    p = subprocess.run(args, capture_output=True, text=True, timeout=15)
    assert p.returncode == 0, (
        f"{label} behavioural test failed:\nstdout={p.stdout}\n"
        f"stderr={p.stderr}"
    )


def test_p1s2_bell_handle_item_click_is_optimistic():
    """Pure-function extract of NotificationBell handlers: simulate
    that the optimistic state mutation happens BEFORE the markRead
    await resolves."""
    bell_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', '..', '..',
        'Nunba-HART-Companion/landing-page/src/components/Common/'
        'NotificationBell.js'))
    if not os.path.exists(bell_path):
        pytest.skip(f"NotificationBell.js not present")

    js = '''
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
// Pull out the handleItemClick function body — verify the order of
// operations is: (1) setItems filter, (2) setUnreadCount decrement,
// (3) await markRead.  React state setters are called BEFORE the
// await, which is what "optimistic" means in practice.
const m = src.match(/const handleItemClick = async \\(notification\\) => \\{([\\s\\S]+?)\\n  \\};/);
if (!m) { console.error('handleItemClick not found'); process.exit(2); }
const body = m[1];
const setItemsIdx = body.indexOf('setItems(prev => prev.filter');
const setCountIdx = body.indexOf('setUnreadCount(prev => Math.max(0, prev - 1))');
const awaitIdx   = body.indexOf('await notificationsApi.markRead');
if (setItemsIdx < 0 || setCountIdx < 0 || awaitIdx < 0) {
  console.error('missing one of: optimistic setItems / setUnread / await markRead');
  process.exit(3);
}
if (!(setItemsIdx < awaitIdx && setCountIdx < awaitIdx)) {
  console.error('FAIL: optimistic mutations must precede await');
  process.exit(4);
}
console.log('OK');
'''
    _node_assert(js, label='P1-S2 handleItemClick',
                 files={'bell': bell_path})


def test_p1s2_bell_subscribes_notification_read():
    bell_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', '..', '..',
        'Nunba-HART-Companion/landing-page/src/components/Common/'
        'NotificationBell.js'))
    if not os.path.exists(bell_path):
        pytest.skip("NotificationBell.js not present")
    js = '''
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const sub = /realtimeService\\.on\\(\\s*['"]notification\\.read['"]/.test(src);
const filterItems = /setItems\\(prev => prev\\.filter\\(n => !idSet\\.has\\(String\\(n\\.id\\)\\)\\)\\)/.test(src);
const decrement = /setUnreadCount\\(prev => Math\\.max\\(0, prev - ids\\.length\\)\\)/.test(src);
if (!sub) { console.error('FAIL: no realtimeService.on(notification.read)'); process.exit(2); }
if (!filterItems) { console.error('FAIL: ids set filter on items missing'); process.exit(3); }
if (!decrement) { console.error('FAIL: unread count decrement missing'); process.exit(4); }
console.log('OK');
'''
    _node_assert(js, label='P1-S2 subscribe', files={'bell': bell_path})


def test_p1s3_paircode_overlay_marks_read_on_expiry():
    overlay_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', '..', '..',
        'Nunba-HART-Companion/landing-page/src/components/AgentOverlay/'
        'AgentOverlay.jsx'))
    if not os.path.exists(overlay_path):
        pytest.skip("AgentOverlay.jsx not present")
    js = '''
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
// Extract the PairCodeOverlay function source.
const m = src.match(/function PairCodeOverlay\\([\\s\\S]+?\\n\\}/);
if (!m) { console.error('PairCodeOverlay not found'); process.exit(2); }
const body = m[0];
// The new effect must fire markRead when remaining hits 0 and
// notification_id is present.
if (!/notificationId\\s*=\\s*data\\s*&&\\s*data\\.notification_id/.test(body)) {
  console.error('FAIL: notification_id not read from data');
  process.exit(3);
}
if (!/remaining === 0 && notificationId && !expireDispatchedRef\\.current/.test(body)) {
  console.error('FAIL: expiry guard missing');
  process.exit(4);
}
if (!/notificationsApi\\.markRead\\(\\[notificationId\\]\\)/.test(body)) {
  console.error('FAIL: notificationsApi.markRead not called on expiry');
  process.exit(5);
}
console.log('OK');
'''
    _node_assert(js, label='P1-S3 expiry markRead',
                 files={'overlay': overlay_path})
