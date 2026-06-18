"""
Unit tests for the node-side OTA push listener
(integrations/agent_engine/ota_push_listener.py).

These prove the PUSH half of the node trigger model behaviourally — they import
the REAL handle_push / run_push_listener, mock the boundaries (the signature
authority check, subprocess, and the MessageBus), call the real functions, and
assert observable effects (subprocess argv, bus.subscribe call, return values).
No grep/source-shape assertions.

Covered:
  - a verified firmware_update for this node kicks `systemctl start
    hart-ota-check.service` (the SAME apply the boot poll uses)
  - an UNVERIFIED (forged) push is refused — no kick
  - a non-OTA command type is ignored (other consumers own it)
  - a push targeted at a DIFFERENT node is ignored
  - an `os_update` alias is also accepted
  - the kick is bounded (subprocess.run called with a timeout)
  - run_push_listener subscribes to the EXISTING 'fleet.command' fabric (no new
    transport) and returns the handler without blocking when block=False
  - a non-systemd host (FileNotFoundError) degrades gracefully (no raise)
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.agent_engine import ota_push_listener as L  # noqa: E402


NODE = "thisnode00000000"


def _push(cmd_type="firmware_update", target="", **params):
    cmd = {
        "cmd_type": cmd_type,
        "issued_by": "central00central0",
        "signature": "sig",
        "params": params or {"update_url": "github:hertz-ai/HARTOS/abc",
                             "release_hash": "abc", "channel": "stable"},
    }
    if target:
        cmd["target_node_id"] = target
    return cmd


@pytest.fixture
def verified():
    """Force the fleet authority check True (central/regional signature OK)."""
    with patch(
        "integrations.social.fleet_command.FleetCommandService.verify_command_signature",
        return_value=True,
    ):
        yield


@pytest.fixture
def forged():
    """Force the fleet authority check False (forged/unauthorized push)."""
    with patch(
        "integrations.social.fleet_command.FleetCommandService.verify_command_signature",
        return_value=False,
    ):
        yield


class TestVerifiedPushKicksSameApply:

    def test_verified_firmware_update_kicks_check_unit(self, verified):
        with patch.object(L.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=0, stderr="")
            kicked = L.handle_push(_push(), self_node_id=NODE)

        assert kicked is True
        # Converges on the EXACT unit the boot poll uses — not a second updater.
        argv = mrun.call_args[0][0]
        assert argv == ["systemctl", "start", L.OTA_CHECK_UNIT]
        assert L.OTA_CHECK_UNIT == "hart-ota-check.service"

    def test_kick_is_bounded_with_timeout(self, verified):
        """A push must never hang the listener — the kick has a timeout."""
        with patch.object(L.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=0, stderr="")
            L.handle_push(_push(), self_node_id=NODE)
        assert mrun.call_args.kwargs.get("timeout") is not None

    def test_os_update_alias_also_kicks(self, verified):
        with patch.object(L.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=0, stderr="")
            kicked = L.handle_push(_push(cmd_type="os_update"), self_node_id=NODE)
        assert kicked is True
        assert mrun.called

    def test_untargeted_push_applies_to_this_node(self, verified):
        """No target_node_id = broadcast → this node applies."""
        with patch.object(L.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=0, stderr="")
            assert L.handle_push(_push(target=""), self_node_id=NODE) is True

    def test_targeted_at_this_node_applies(self, verified):
        with patch.object(L.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=0, stderr="")
            assert L.handle_push(_push(target=NODE), self_node_id=NODE) is True


class TestRefusedPushes:

    def test_forged_push_is_refused(self, forged):
        """An unverified central push must NOT kick the apply."""
        with patch.object(L.subprocess, "run") as mrun:
            kicked = L.handle_push(_push(), self_node_id=NODE)
        assert kicked is False
        mrun.assert_not_called()

    def test_non_ota_command_is_ignored(self, verified):
        """Non-OTA commands are owned by their own consumers, not the updater."""
        with patch.object(L.subprocess, "run") as mrun:
            kicked = L.handle_push(_push(cmd_type="config_update"), self_node_id=NODE)
        assert kicked is False
        mrun.assert_not_called()

    def test_push_for_other_node_is_ignored(self, verified):
        with patch.object(L.subprocess, "run") as mrun:
            kicked = L.handle_push(_push(target="othernode0000000"), self_node_id=NODE)
        assert kicked is False
        mrun.assert_not_called()

    def test_non_dict_payload_is_ignored(self):
        with patch.object(L.subprocess, "run") as mrun:
            assert L.handle_push("not-a-dict", self_node_id=NODE) is False
        mrun.assert_not_called()


class TestKickFailureModes:

    def test_kick_returns_false_on_nonzero_exit(self, verified):
        with patch.object(L.subprocess, "run") as mrun:
            mrun.return_value = MagicMock(returncode=1, stderr="boom")
            assert L.handle_push(_push(), self_node_id=NODE) is False

    def test_no_systemd_host_degrades_gracefully(self, verified):
        """On a non-systemd host the kick is a no-op, never a raise."""
        with patch.object(L.subprocess, "run", side_effect=FileNotFoundError):
            # Must not raise; returns False (nothing kicked).
            assert L.handle_push(_push(), self_node_id=NODE) is False


class TestListenerSubscribesExistingFabric:

    def test_run_push_listener_subscribes_to_fleet_command(self):
        """The listener attaches ONE handler to the existing bus topic — it adds
        no new transport. block=False returns the handler without parking."""
        fake_bus = MagicMock()
        with patch("core.peer_link.message_bus.get_message_bus", return_value=fake_bus):
            with patch.object(L, "_self_node_id", return_value=NODE):
                handler = L.run_push_listener(block=False)

        assert fake_bus.subscribe.called
        topic = fake_bus.subscribe.call_args[0][0]
        assert topic == "fleet.command"
        assert callable(handler)

    def test_subscribed_handler_routes_to_handle_push(self):
        """The handler the listener registers must funnel bus messages through
        handle_push (so a real push on the fabric reaches the apply path)."""
        fake_bus = MagicMock()
        with patch("core.peer_link.message_bus.get_message_bus", return_value=fake_bus):
            with patch.object(L, "_self_node_id", return_value=NODE):
                with patch.object(L, "drain_pending"):
                    handler = L.run_push_listener(block=False)

        with patch.object(L, "handle_push") as mhandle:
            handler("fleet.command", _push())
        assert mhandle.called
        # Handler must pass this node's id through for targeting.
        assert mhandle.call_args.kwargs.get("self_node_id") == NODE

    def test_run_listener_drains_durable_before_subscribe(self):
        """drain→subscribe order (mirrors embedded_main): offline-queued pushes
        are applied on start, then the realtime subscription attaches."""
        fake_bus = MagicMock()
        with patch("core.peer_link.message_bus.get_message_bus", return_value=fake_bus):
            with patch.object(L, "_self_node_id", return_value=NODE):
                with patch.object(L, "drain_pending") as mdrain:
                    L.run_push_listener(block=False)
        assert mdrain.called
        assert fake_bus.subscribe.called


class TestDurableDrain:
    """drain_pending applies offline-queued central pushes via the SAME gate."""

    def _db_ctx(self, pending):
        """A fake get_db() returning a session whose get_pending_commands the
        FleetCommandService static returns `pending`."""
        fake_db = MagicMock()
        return fake_db

    def test_drain_kicks_for_queued_ota_push(self):
        pending = [_push()]  # one offline-queued firmware_update
        fake_db = MagicMock()
        with patch("integrations.social.models.get_db", return_value=fake_db):
            with patch(
                "integrations.social.fleet_command.FleetCommandService.get_pending_commands",
                return_value=pending,
            ):
                with patch.object(L, "handle_push", return_value=True) as mhandle:
                    n = L.drain_pending(self_node_id=NODE)
        assert n == 1
        assert mhandle.called
        fake_db.commit.assert_called_once()
        fake_db.close.assert_called_once()

    def test_drain_ignores_non_ota_queued_commands(self):
        pending = [_push(cmd_type="config_update"), _push(cmd_type="halt")]
        fake_db = MagicMock()
        with patch("integrations.social.models.get_db", return_value=fake_db):
            with patch(
                "integrations.social.fleet_command.FleetCommandService.get_pending_commands",
                return_value=pending,
            ):
                with patch.object(L, "handle_push") as mhandle:
                    n = L.drain_pending(self_node_id=NODE)
        assert n == 0
        mhandle.assert_not_called()

    def test_drain_survives_db_unavailable(self):
        """No DB (e.g. gossip-only mode) → 0, never a raise."""
        with patch("integrations.social.models.get_db", side_effect=Exception("no db")):
            assert L.drain_pending(self_node_id=NODE) == 0


class TestBackendRealtimeLegWired:
    """The realtime leg lives in the backend's local_subscribers bootstrap —
    it must subscribe 'fleet.command' and route OTA pushes to handle_push."""

    def test_bootstrap_subscribes_fleet_command_to_handle_push(self):
        import core.peer_link.local_subscribers as ls

        fake_bus = MagicMock()
        # Reset the once-only guard so bootstrap actually runs in this test.
        with patch.object(ls, "_bootstrapped", False):
            with patch("core.peer_link.message_bus.get_message_bus", return_value=fake_bus):
                ls.bootstrap_local_subscribers()

        # The backend must subscribe a handler to the EXISTING 'fleet.command'
        # bus topic (the realtime push leg) — no new transport.
        fleet_handlers = [
            call.args[1] for call in fake_bus.subscribe.call_args_list
            if call.args and call.args[0] == "fleet.command"
        ]
        assert fleet_handlers, "backend did not subscribe to 'fleet.command'"

        # Drive the REAL registered handler with a verified firmware_update push
        # and assert it converges on the apply kick (subprocess start
        # hart-ota-check).  local_subscribers binds handle_push by name at
        # import, so we exercise the real handle_push end-to-end with the
        # authority gate forced True + the kick stubbed — proving the wiring,
        # not a patched stand-in.
        with patch(
            "integrations.social.fleet_command.FleetCommandService.verify_command_signature",
            return_value=True,
        ):
            with patch.object(L.subprocess, "run") as mrun:
                mrun.return_value = MagicMock(returncode=0, stderr="")
                fleet_handlers[0]("fleet.command", _push())
        assert mrun.called, "fleet.command handler did not converge on the apply kick"
        assert mrun.call_args[0][0] == ["systemctl", "start", L.OTA_CHECK_UNIT]
