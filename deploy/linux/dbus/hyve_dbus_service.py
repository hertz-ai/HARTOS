#!/usr/bin/env python3
"""
HyveOS D-Bus Service - Bridges D-Bus IPC to the Flask backend.

Exposes HyveOS functionality to native Linux desktop applications:
  - com.hyve.Agent.Chat(prompt) -> forwards to /chat
  - com.hyve.Agent.Status() -> node health
  - com.hyve.Agent.Consent(command_id, approved) -> fleet consent
  - com.hyve.Agent.GoalCreate(type, title, description) -> create goal
  - Signal: ConsentRequired(command_id, action, description)

Install:
  cp com.hyve.Agent.conf /etc/dbus-1/system.d/
  systemctl enable hyve-dbus.service
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

logger = logging.getLogger("hyve-dbus")

# Read port from env file, fallback to 6777
BACKEND_PORT = 6777
try:
    with open('/etc/hyve/hyve.env') as _ef:
        for _line in _ef:
            if _line.startswith('HYVE_BACKEND_PORT='):
                BACKEND_PORT = int(_line.strip().split('=', 1)[1])
                break
except (FileNotFoundError, ValueError):
    pass

BUS_NAME = "com.hyve.Agent"
OBJ_PATH = "/com/hyve/Agent"
IFACE = "com.hyve.Agent"

# Polling interval for fleet commands (seconds)
FLEET_POLL_INTERVAL = 10


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


class HyveAgentService(dbus.service.Object):
    """D-Bus service object exposing HyveOS agent functionality."""

    def __init__(self, bus):
        bus_name = dbus.service.BusName(BUS_NAME, bus)
        super().__init__(bus_name, OBJ_PATH)
        logger.info("HyveOS D-Bus service registered at %s", OBJ_PATH)

    @dbus.service.method(IFACE, in_signature="ss", out_signature="s")
    def Chat(self, prompt, user_id="dbus_user"):
        """Send a chat message to the Hyve agent."""
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
            sys.path.insert(0, '/opt/hyve')
            from integrations.social.fleet_command import FleetCommandService
            from integrations.social.models import get_db
            db = get_db()
            try:
                # Read node ID for the ack
                node_id = "dbus_local"
                try:
                    with open("/var/lib/hyve/node_public.key", "rb") as f:
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
            with open("/var/lib/hyve/node_public.key", "rb") as f:
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


def _poll_fleet_commands(service):
    """Poll for pending fleet commands and emit D-Bus ConsentRequired signals.

    Uses FleetCommandService directly (no HTTP endpoint needed).
    """
    try:
        import sys
        if '/opt/hyve' not in sys.path:
            sys.path.insert(0, '/opt/hyve')
        from integrations.social.fleet_command import FleetCommandService
        from integrations.social.models import get_db

        # Read local node ID
        node_id = "dbus_local"
        try:
            with open("/var/lib/hyve/node_public.key", "rb") as f:
                node_id = f.read().hex()[:16]
        except FileNotFoundError:
            pass

        db = get_db()
        try:
            commands = FleetCommandService.get_pending_commands(db, node_id)
            if commands:
                for cmd in commands:
                    cmd_id = str(cmd.get('id', ''))
                    action = cmd.get('command_type', 'unknown')
                    payload = cmd.get('payload', {})
                    desc = payload.get('description', action) if isinstance(payload, dict) else action
                    # Only emit for consent-type commands
                    if action in ('agent_consent', 'tts_stream'):
                        service.ConsentRequired(cmd_id, action, desc)
        finally:
            db.close()
    except ImportError:
        logger.debug("Fleet command service not available yet")
    except Exception as e:
        logger.debug("Fleet poll: %s", e)
    return True  # keep the GLib timeout active


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    service = HyveAgentService(bus)

    # Poll for fleet consent commands and emit signals
    GLib.timeout_add_seconds(FLEET_POLL_INTERVAL,
                             lambda: _poll_fleet_commands(service))

    logger.info("HyveOS D-Bus service running on system bus")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
