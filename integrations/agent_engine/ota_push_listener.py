"""
OTA Push Listener — node-side receiver that turns a CENTRAL push into the
SAME apply the boot poll uses, over the EXISTING fabric (no new transport).

Trigger model (NODE side):
  - POLL central ONLY on boot (the hart-ota-check OnBootSec timer) and when
    the user runs `hart-ota check` — there is NO periodic interval poll.
  - RECEIVE a CENTRAL push at any time → this module.

How the push arrives (REUSED, nothing new):
  The queen-bee central account fans an approved build out as a SIGNED
  ``firmware_update`` FleetCommand (integrations.social.fleet_command
  .FleetCommandService.push_broadcast, driven by POST /api/ota/publish in
  integrations.social.api_fleet_update).  That command rides the EXISTING
  MessageBus 'fleet.command' topic — whose transport legs are the gossip /
  WAMP (Crossbar) / PeerLink fabric (core.peer_link.message_bus).  The
  durable DB queue is the offline fallback drained on boot.

  On a NixOS HART node the long-lived backend is ``hart-backend`` (waitress),
  NOT ``embedded_main`` — so the embedded-loop subscriber
  (embedded_main._subscribe_fleet_commands) never runs here.  This module is
  the node-side subscriber for the NixOS/desktop/server topology: the
  ``hart-ota-push`` systemd unit runs ``run_push_listener()``.

What it does on a verified OTA push (REUSED apply path):
  Verify the command signature with the SAME authority check the fleet bus
  uses (FleetCommandService.verify_command_signature → verify_tier_authorization
  required_tier='regional', anchor MASTER_PUBLIC_KEY_HEX).  Then kick the
  EXISTING ``hart-ota-check`` service (``systemctl start hart-ota-check.service``)
  so the push converges on the EXACT same staged pipeline → autoApply
  ``nixos-rebuild switch --flake`` → ``|| nixos-rebuild switch --rollback``
  the boot poll uses.  Central only chooses WHICH commit; the node's local
  SIGN/CANARY gates still run — a push NEVER force-applies past canary, and
  this module NEVER touches the master private key.

DRY:
  - Transport         → core.peer_link.message_bus.MessageBus (existing)
  - Authority check   → FleetCommandService.verify_command_signature (existing)
  - Self node id      → fleet_command._get_self_node_id (existing)
  - Apply             → the existing hart-ota-check unit (existing)
  No second updater, no parallel pointer protocol, no new socket.
"""
import logging
import os
import subprocess
import threading

logger = logging.getLogger('hevolve_social')

# The systemd unit that owns the (privileged) staged apply.  A push only
# *kicks* this unit — the pipeline, canary gate, atomic generation switch and
# auto-rollback all live in hart-ota.nix, unchanged.  Overridable for tests.
OTA_CHECK_UNIT = os.environ.get('HART_OTA_CHECK_UNIT', 'hart-ota-check.service')

# OTA-class command types that should kick the apply.  ``firmware_update`` is
# the existing central→node update command (VALID_COMMAND_TYPES already
# contains it); we accept an explicit ``os_update`` alias too so a future
# NixOS-specific push needn't overload firmware semantics.
OTA_PUSH_CMD_TYPES = frozenset({'firmware_update', 'os_update'})


def _self_node_id() -> str:
    """This node's id — REUSE the fleet bus helper (single source)."""
    try:
        from integrations.social.fleet_command import _get_self_node_id
        return _get_self_node_id()
    except Exception:
        return 'unknown'


def _kick_ota_check() -> bool:
    """Start the EXISTING hart-ota-check unit — the same apply the boot poll runs.

    Returns True if the start command exited 0.  Best-effort + bounded: a
    push must never hang the listener (Gate 7 — subprocess with timeout).
    """
    try:
        r = subprocess.run(
            ['systemctl', 'start', OTA_CHECK_UNIT],
            timeout=30, capture_output=True, text=True,
        )
        if r.returncode == 0:
            logger.info("OTA push: kicked %s (central push → staged apply)",
                        OTA_CHECK_UNIT)
            return True
        logger.warning("OTA push: `systemctl start %s` exit %d: %s",
                       OTA_CHECK_UNIT, r.returncode, (r.stderr or '').strip())
        return False
    except FileNotFoundError:
        # Not a systemd host (e.g. dev box) — nothing to kick.
        logger.debug("OTA push: systemctl not found; skipping apply kick")
        return False
    except Exception as e:
        logger.warning("OTA push: apply kick failed: %s", e)
        return False


def handle_push(cmd: dict, self_node_id: str = '') -> bool:
    """Handle ONE fleet command pushed over the existing fabric.

    Returns True iff this was a verified OTA-class command for THIS node and
    the apply was kicked.  All other commands (other targets, non-OTA types,
    bad signatures) are ignored here — they are handled by their own existing
    consumers, NOT re-implemented in the updater.

    Args:
        cmd: the FleetCommand dict as published on 'fleet.command'
             (carries cmd_type, params, signature, issued_by, target_node_id).
        self_node_id: this node's id; resolved if empty.
    """
    if not isinstance(cmd, dict):
        return False

    cmd_type = cmd.get('cmd_type', '')
    if cmd_type not in OTA_PUSH_CMD_TYPES:
        return False  # not ours — other consumers own non-OTA commands

    me = self_node_id or _self_node_id()
    target = cmd.get('target_node_id', '')
    if target and me and target != me:
        return False  # push aimed at a different node

    # Same authority check the fleet bus uses — central/regional Ed25519
    # signature anchored at MASTER_PUBLIC_KEY_HEX.  An unsigned / unauthorized
    # push is rejected: a node NEVER applies an unverified central command.
    try:
        from integrations.social.fleet_command import FleetCommandService
        if not FleetCommandService.verify_command_signature(cmd):
            logger.warning("OTA push: rejected %s — invalid/unauthorized signature",
                           cmd_type)
            return False
    except Exception as e:
        logger.warning("OTA push: signature verify unavailable, refusing: %s", e)
        return False

    logger.info("OTA push: verified %s from %s — triggering staged apply",
                cmd_type, (cmd.get('issued_by', '') or '?')[:8])
    return _kick_ota_check()


def drain_pending(self_node_id: str = '') -> int:
    """Apply any DURABLE (offline-queued) OTA pushes for this node, once.

    The realtime leg (the in-process backend subscriber + this listener's
    bus.subscribe) only catches pushes that arrive while connected.  A push
    sent while the node was OFF is persisted as a pending FleetCommand row
    (the offline-first durable fallback).  On start we drain those exactly
    like embedded_main._drain_fleet_commands does — REUSING
    FleetCommandService.get_pending_commands (which itself re-verifies each
    issuer) — and route OTA-class commands through the same gated kick.

    Returns the number of OTA pushes that kicked the apply (0 if none / DB
    unavailable — never raises; an unreachable DB must not crash the unit).
    """
    me = self_node_id or _self_node_id()
    try:
        from integrations.social.models import get_db
        from integrations.social.fleet_command import FleetCommandService
    except Exception as e:
        logger.debug("OTA push: durable drain unavailable: %s", e)
        return 0

    kicked = 0
    try:
        db = get_db()
        try:
            for cmd in FleetCommandService.get_pending_commands(db, me):
                if cmd.get('cmd_type') in OTA_PUSH_CMD_TYPES:
                    if handle_push(cmd, self_node_id=me):
                        kicked += 1
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug("OTA push: durable drain error: %s", e)
    if kicked:
        logger.info("OTA push: applied %d offline-queued central push(es)", kicked)
    return kicked


def run_push_listener(block: bool = True):
    """Drain durable OTA pushes, then subscribe to the EXISTING fabric.

    The ExecStart entrypoint of the ``hart-ota-push`` systemd unit.  It adds
    NO transport — it (1) drains any offline-queued central OTA push from the
    durable FleetCommand queue (catches pushes sent while the node was off),
    then (2) attaches one handler to the MessageBus 'fleet.command' topic every
    other fleet consumer already uses.  Both legs converge OTA pushes on the
    existing hart-ota-check apply path.  Mirrors embedded_main's drain→subscribe
    order so there is one node-side fleet-receive shape, not two.

    Args:
        block: keep the process alive after subscribing (systemd Type=simple).
               Tests pass block=False to subscribe-and-return.
    """
    me = _self_node_id()

    # 1. Durable drain (offline-queued pushes) — once, at start.
    try:
        drain_pending(me)
    except Exception as e:
        logger.debug("OTA push: initial drain skipped: %s", e)

    # 2. Realtime subscribe to the existing bus topic.
    from core.peer_link.message_bus import get_message_bus
    bus = get_message_bus()

    def _on_fleet_command(topic, data):
        try:
            handle_push(data, self_node_id=me)
        except Exception as e:
            logger.error("OTA push handler error: %s", e)

    bus.subscribe('fleet.command', _on_fleet_command)
    logger.info("OTA push listener: subscribed to fleet.command (node %s)",
                (me or '?')[:8])

    if not block:
        return _on_fleet_command

    # Idle forever — the bus delivers on its own transport threads.  A plain
    # Event().wait() parks this thread with no busy-spin (Gate 7 / the
    # resource_governor busy-spin lesson: never sleep-loop a core).
    threading.Event().wait()


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    # `--drain-only`  : apply offline-queued central pushes once, then exit
    #                   (the hart-ota-push boot oneshot — the realtime leg lives
    #                   in hart-backend's bootstrap_local_subscribers).
    # default (block) : drain then subscribe and stay up (embedded/headless).
    if '--drain-only' in sys.argv[1:]:
        drain_pending()
    else:
        run_push_listener(block=True)
