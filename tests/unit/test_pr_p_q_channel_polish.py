"""
PR P + PR Q — server-side tests.

Covers the HARTOS-side changes that landed during the connect-entry /
during-connect polish (PR P) and the channel-health banner (PR Q):

  - PR P.4 register_channel adapter probe + toast emit on failure
  - PR P.5 disconnect_channel agent tool
  - PR P.6 reconnect_channel agent tool
  - PR Q  channel_unhealthy in VALID_COMMAND_TYPES
  - PR Q  emit_channel_unhealthy fans out to all the user's devices

Client-side (Liquid UI BannerCard / ToastCard / Cancel buttons / inbox
empty-state CTA / replyTo divert) is covered by the matching jest
suite in Hevolve_React_Native/__tests__/Phase7c/prPqChannelPolish.test.js.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

# ─── PR Q — fleet_command surface ──────────────────────────────────


class TestChannelUnhealthyFleetCommand:
    """``channel_unhealthy`` is a first-class fleet command type and
    fans out to every active device the user has linked.
    """

    def test_channel_unhealthy_is_in_valid_types(self):
        from integrations.social.fleet_command import VALID_COMMAND_TYPES
        assert 'channel_unhealthy' in VALID_COMMAND_TYPES, (
            "channel_unhealthy must be in the validation allow-list, "
            "otherwise push_command rejects it before queueing"
        )

    def test_emit_channel_unhealthy_no_devices_returns_zero(self):
        from integrations.social.fleet_command import emit_channel_unhealthy
        db = MagicMock()
        # No devices linked → query returns []
        db.query.return_value.filter_by.return_value.all.return_value = []
        n = emit_channel_unhealthy(
            db, user_id=42, channel_type='slack', reason='401',
        )
        assert n == 0

    def test_emit_channel_unhealthy_queues_one_per_device(self):
        """When the user has N devices, we push N signed commands."""
        from integrations.social.fleet_command import emit_channel_unhealthy

        # Two fake devices with device_id attributes.
        d1 = MagicMock(device_id='aabbccdd11')
        d2 = MagicMock(device_id='eeff001122')
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [d1, d2]

        # Patch push_command so we don't write to the DB / sign.
        push_calls = []

        def fake_push(_db, *, node_id, cmd_type, params):
            push_calls.append((node_id, cmd_type, params))
            return {'id': len(push_calls), 'cmd_type': cmd_type}

        with patch(
            'integrations.social.fleet_command.FleetCommandService.push_command',
            side_effect=fake_push,
        ):
            n = emit_channel_unhealthy(
                db, user_id=7, channel_type='telegram',
                reason='token revoked', binding_id=99,
            )

        assert n == 2
        assert len(push_calls) == 2
        # Both calls carry the channel_unhealthy cmd_type + the same params.
        for node_id, cmd_type, params in push_calls:
            assert cmd_type == 'channel_unhealthy'
            assert params['channel_type'] == 'telegram'
            assert params['reason'] == 'token revoked'
            assert params['binding_id'] == 99
        # And the right devices.
        assert {c[0] for c in push_calls} == {'aabbccdd11', 'eeff001122'}

    def test_emit_channel_unhealthy_default_reason(self):
        """Empty/falsey reason becomes a sensible default."""
        from integrations.social.fleet_command import emit_channel_unhealthy

        d = MagicMock(device_id='x' * 12)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [d]
        captured = []
        with patch(
            'integrations.social.fleet_command.FleetCommandService.push_command',
            side_effect=lambda _d, *, node_id, cmd_type, params: captured.append(params) or {'id': 1},
        ):
            emit_channel_unhealthy(db, user_id=1, channel_type='discord', reason='')
        assert captured[0]['reason'] == 'authentication expired'

    def test_emit_channel_unhealthy_skips_device_without_id(self):
        """A DeviceBinding row lacking both ``device_id`` and ``node_id``
        is silently skipped (defensive: corrupted DB row shouldn't break
        the fan-out for the other devices)."""
        from integrations.social.fleet_command import emit_channel_unhealthy

        good = MagicMock(device_id='gooddevice1', node_id=None)
        bad = MagicMock(device_id=None, node_id=None)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [good, bad]

        captured = []
        with patch(
            'integrations.social.fleet_command.FleetCommandService.push_command',
            side_effect=lambda _d, *, node_id, cmd_type, params: (
                captured.append(node_id) or {'id': 1}
            ),
        ):
            n = emit_channel_unhealthy(db, user_id=1, channel_type='line', reason='x')
        assert n == 1, "only the device with a valid id should be queued"
        assert captured == ['gooddevice1']

    def test_emit_channel_unhealthy_db_lookup_failure_returns_zero(self):
        """DB lookup throwing must not propagate — caller's primary
        path (the channel registration) must never fail because of
        the fan-out."""
        from integrations.social.fleet_command import emit_channel_unhealthy
        db = MagicMock()
        db.query.side_effect = RuntimeError("DB gone")
        n = emit_channel_unhealthy(db, user_id=1, channel_type='slack', reason='x')
        assert n == 0


# ─── PR P.5 / P.6 — disconnect / reconnect agent tools ────────────


class TestDisconnectReconnectChannelTools:
    """The two new agent tools must be registered in
    ``build_channel_tool_closures`` and follow the documented contract:
      - disconnect: flip binding.is_active = False, emit toast
      - reconnect:  if inactive binding exists, flip it back on;
                    otherwise instruct the user to run Connect_Channel
                    (no parallel onboarding path).
    """

    def _build_tools(self, user_id=42):
        from integrations.channels.agent_tools import build_channel_tool_closures
        return build_channel_tool_closures({'user_id': user_id, 'prompt_id': None}) or []

    def _get(self, tools, name):
        return next(
            (t[2] for t in tools
             if isinstance(t, tuple) and len(t) >= 3 and t[0] == name),
            None,
        )

    def test_disconnect_channel_is_registered(self):
        fn = self._get(self._build_tools(), 'disconnect_channel')
        assert fn is not None, "disconnect_channel tool must be registered"
        assert callable(fn)

    def test_reconnect_channel_is_registered(self):
        fn = self._get(self._build_tools(), 'reconnect_channel')
        assert fn is not None, "reconnect_channel tool must be registered"
        assert callable(fn)

    def test_disconnect_unknown_channel_returns_error_text(self):
        fn = self._get(self._build_tools(), 'disconnect_channel')
        out = fn('nonexistent-channel-xyz')
        assert 'Unknown channel' in out

    def test_reconnect_unknown_channel_returns_error_text(self):
        fn = self._get(self._build_tools(), 'reconnect_channel')
        out = fn('nonexistent-channel-xyz')
        assert 'Unknown channel' in out

    def test_disconnect_then_reconnect_known_channel_happy_path(self, monkeypatch):
        """End-to-end:
          1. precondition: an *active* binding row exists for the user.
          2. disconnect → row.is_active flips False, returns success copy.
          3. reconnect → row.is_active flips True, returns success copy.
        """
        # Patch the DB + binding model to a single fake row owned by user 42.
        fake_row = MagicMock(
            user_id='42', channel_type='discord', is_active=True,
        )
        fake_db = MagicMock()
        fake_db.query.return_value.filter_by.return_value.first.return_value = fake_row

        def fake_get_db():
            return fake_db

        with patch(
            'integrations.social.models.get_db', fake_get_db,
        ), patch(
            'integrations.channels.agent_tools.'
            '_get_user_id_from_threadlocal',
            return_value=42,
        ):
            tools = self._build_tools()
            disconnect = self._get(tools, 'disconnect_channel')
            reconnect = self._get(tools, 'reconnect_channel')

            # Disconnect
            out1 = disconnect('discord')
            assert 'disconnected' in out1.lower()
            assert fake_row.is_active is False

            # Reconnect path 1: existing inactive binding flips back on.
            out2 = reconnect('discord')
            assert 'reconnected' in out2.lower() or 'reactivated' in out2.lower()
            assert fake_row.is_active is True

    def test_reconnect_when_no_binding_exists_returns_connect_instruction(
        self, monkeypatch,
    ):
        """No inactive row → tool must direct the user back through the
        standard Connect_Channel flow rather than silently failing."""
        fake_db = MagicMock()
        fake_db.query.return_value.filter_by.return_value.first.return_value = None

        with patch(
            'integrations.social.models.get_db', return_value=fake_db,
        ), patch(
            'integrations.channels.agent_tools.'
            '_get_user_id_from_threadlocal',
            return_value=42,
        ):
            tools = self._build_tools()
            reconnect = self._get(tools, 'reconnect_channel')
            out = reconnect('telegram')
            assert 'Connect_Channel' in out or 'connect' in out.lower()


# ─── PR P.4 — register_channel adapter probe fires async ──────────


class TestAdapterProbeBackgroundThread:
    """Probe must run in a daemon thread so the agent-tool return is
    not blocked by adapter.connect()'s network latency."""

    def test_probe_thread_is_daemon_and_named(self, monkeypatch):
        """When an adapter is in the registry, register_channel spawns
        a daemon thread (so app shutdown isn't blocked by an in-flight
        probe) named ``channel-probe-<type>`` (so it's findable in a
        thread dump).
        """
        import threading
        from integrations.channels.agent_tools import build_channel_tool_closures

        spawned = []
        real_thread_init = threading.Thread.__init__

        def capture_init(self, *args, **kwargs):
            spawned.append({
                'name': kwargs.get('name'),
                'daemon': kwargs.get('daemon'),
                'target': kwargs.get('target'),
            })
            return real_thread_init(self, *args, **kwargs)

        # Fake adapter that has a connect() coroutine.
        class FakeAdapter:
            async def connect(self):
                return True

        class FakeRegistry:
            def get(self, name):
                return FakeAdapter()

        fake_api = MagicMock()
        fake_api._channels = {}

        with patch(
            'integrations.channels.registry.get_registry',
            return_value=FakeRegistry(),
        ), patch(
            'integrations.channels.admin.api.get_api', return_value=fake_api,
        ), patch.object(threading.Thread, '__init__', capture_init):
            tools = build_channel_tool_closures({'user_id': 1, 'prompt_id': None})
            register = next(
                (t[2] for t in tools if t[0] == 'register_channel'), None,
            )
            assert register is not None
            register('discord', '{"bot_token": "fake-xxx"}')

        # At least one thread spawn must be the probe.
        probe_threads = [s for s in spawned if s['name'] == 'channel-probe-discord']
        assert len(probe_threads) >= 1, (
            "expected a daemon thread named 'channel-probe-discord' to be "
            "spawned by register_channel after the binding write"
        )
        assert probe_threads[0]['daemon'] is True, (
            "probe thread must be daemon=True so app shutdown isn't blocked"
        )
