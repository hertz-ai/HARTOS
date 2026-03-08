#!/usr/bin/env python3
"""
HART OS D-Bus Service - Bridges D-Bus IPC to the Flask backend.

Exposes HART OS functionality to native Linux desktop applications:
  - com.hart.Agent.Chat(prompt) -> forwards to /chat
  - com.hart.Agent.Status() -> node health
  - com.hart.Agent.Consent(command_id, approved) -> fleet consent
  - com.hart.Agent.GoalCreate(type, title, description) -> create goal
  - Signal: ConsentRequired(command_id, action, description)

Install:
  cp com.hart.Agent.conf /etc/dbus-1/system.d/
  systemctl enable hart-dbus.service
"""

import json
import logging
import urllib.request
import urllib.error

try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
except ImportError:
    print("ERROR: python3-dbus and python3-gi required.")
    print("  apt install python3-dbus python3-gi gir1.2-glib-2.0")
    raise SystemExit(1)

logger = logging.getLogger("hart-dbus")

# Read port from env file, fallback to 6777
BACKEND_PORT = 6777
try:
    with open('/etc/hart/hart.env') as _ef:
        for _line in _ef:
            if _line.startswith('HARTOS_BACKEND_PORT='):
                BACKEND_PORT = int(_line.strip().split('=', 1)[1])
                break
except (FileNotFoundError, ValueError):
    pass

BUS_NAME = "com.hart.Agent"
OBJ_PATH = "/com/hart/Agent"
IFACE = "com.hart.Agent"

# Polling interval for fleet commands (seconds)


def _api_request(method, path, data=None):
    """Make HTTP request to local backend."""
    url = f"http://localhost:{BACKEND_PORT}{path}"
    body = json.dumps(data).encode() if data else None
    headers = {"Content-Type": "application/json"} if data else {}

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


class HartAgentService(dbus.service.Object):
    """D-Bus service object exposing HART OS agent functionality."""

    def __init__(self, bus):
        bus_name = dbus.service.BusName(BUS_NAME, bus)
        super().__init__(bus_name, OBJ_PATH)
        logger.info("HART OS D-Bus service registered at %s", OBJ_PATH)

    @dbus.service.method(IFACE, in_signature="ss", out_signature="s")
    def Chat(self, prompt, user_id="dbus_user"):
        """Send a chat message to the HART agent."""
        result = _api_request("POST", "/chat", {
            "user_id": user_id,
            "prompt_id": "default",
            "prompt": str(prompt),
        })
        return json.dumps(result)

    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def Status(self):
        """Get node status."""
        result = _api_request("GET", "/status")
        return json.dumps(result)

    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def Health(self):
        """Get detailed node health."""
        result = _api_request("GET", "/api/social/dashboard/health")
        return json.dumps(result)

    @dbus.service.method(IFACE, in_signature="sb", out_signature="s")
    def Consent(self, command_id, approved):
        """Respond to an agent consent request.

        Calls FleetCommandService.ack_command() directly since there's
        no HTTP endpoint for fleet ack.
        """
        try:
            import sys
            sys.path.insert(0, '/opt/hart')
            from integrations.social.fleet_command import FleetCommandService
            from integrations.social.models import get_db
            db = get_db()
            try:
                # Read node ID for the ack
                node_id = "dbus_local"
                try:
                    with open("/var/lib/hart/node_public.key", "rb") as f:
                        node_id = f.read().hex()[:16]
                except FileNotFoundError:
                    pass

                result = FleetCommandService.ack_command(
                    db, int(command_id), node_id, success=bool(approved))
                db.commit()
                return json.dumps(result or {"status": "acknowledged"})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({"error": str(e)})

    @dbus.service.method(IFACE, in_signature="sss", out_signature="s")
    def GoalCreate(self, goal_type, title, description):
        """Create a new agent goal."""
        result = _api_request("POST", "/api/goals", {
            "goal_type": str(goal_type),
            "title": str(title),
            "description": str(description),
        })
        return json.dumps(result)

    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def ListPeers(self):
        """List connected peers."""
        result = _api_request("GET", "/api/social/peers")
        return json.dumps(result)

    @dbus.service.method(IFACE, in_signature="", out_signature="s")
    def NodeId(self):
        """Get this node's identity."""
        try:
            with open("/var/lib/hart/node_public.key", "rb") as f:
                return f.read().hex()
        except FileNotFoundError:
            return "not-initialized"

    # -- Signals --

    @dbus.service.signal(IFACE, signature="sss")
    def ConsentRequired(self, command_id, action, description):
        """Emitted when an agent needs user approval."""
        logger.info("ConsentRequired signal: cmd=%s action=%s", command_id, action)

    @dbus.service.signal(IFACE, signature="ss")
    def GoalCompleted(self, goal_id, result_summary):
        """Emitted when an agent goal completes."""
        logger.info("GoalCompleted signal: goal=%s", goal_id)

    @dbus.service.signal(IFACE, signature="s")
    def PeerJoined(self, node_id):
        """Emitted when a new peer joins the hive."""
        logger.info("PeerJoined signal: node=%s", node_id)


def _get_dbus_node_id():
    """Read this node's ID from key file."""
    try:
        with open("/var/lib/hart/node_public.key", "rb") as f:
            return f.read().hex()[:16]
    except FileNotFoundError:
        return "dbus_local"


def _drain_offline_fleet_commands(service):
    """One-time drain of commands queued in DB while offline."""
    try:
        import sys
        if '/opt/hart' not in sys.path:
            sys.path.insert(0, '/opt/hart')
        from integrations.social.fleet_command import FleetCommandService
        from integrations.social.models import get_db

        node_id = _get_dbus_node_id()
        db = get_db()
        try:
            commands = FleetCommandService.get_pending_commands(db, node_id)
            for cmd in commands:
                _emit_consent_if_needed(service, cmd)
            if commands:
                logger.info("Fleet: drained %d offline-queued commands", len(commands))
        finally:
            db.close()
    except ImportError:
        logger.debug("Fleet command service not available yet")
    except Exception as e:
        logger.debug("Fleet drain: %s", e)


def _emit_consent_if_needed(service, cmd):
    """Emit D-Bus ConsentRequired signal for consent-type commands."""
    cmd_id = str(cmd.get('id', ''))
    action = cmd.get('cmd_type', cmd.get('command_type', 'unknown'))
    params = cmd.get('params', cmd.get('payload', {}))
    desc = params.get('description', action) if isinstance(params, dict) else action
    if action in ('agent_consent', 'tts_stream'):
        service.ConsentRequired(cmd_id, action, desc)


def _subscribe_fleet_messagebus(service):
    """Subscribe to fleet.command via MessageBus for instant delivery."""
    try:
        import sys
        if '/opt/hart' not in sys.path:
            sys.path.insert(0, '/opt/hart')
        from core.peer_link.message_bus import get_message_bus

        node_id = _get_dbus_node_id()
        msg_bus = get_message_bus()

        def _on_fleet_command(topic, data):
            if not isinstance(data, dict):
                return
            target = data.get('target_node_id', '')
            if target and target != node_id:
                return
            _emit_consent_if_needed(service, data)

        msg_bus.subscribe('fleet.command', _on_fleet_command)
        logger.info("Fleet: subscribed to MessageBus (instant delivery)")
    except Exception as e:
        logger.debug("Fleet: MessageBus subscription failed: %s", e)


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    service = HartAgentService(bus)

    # Subscribe to MessageBus for instant fleet command delivery
    _subscribe_fleet_messagebus(service)

    # One-time drain of commands queued while offline
    _drain_offline_fleet_commands(service)

    logger.info("HART OS D-Bus service running on system bus")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
