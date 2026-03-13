#!/usr/bin/env python3
"""
HART OS CLI - Command-line interface for managing HART OS services.

Usage:
    hart status          Show all service states + node identity
    hart start           Start all HART OS services
    hart stop            Stop all HART OS services
    hart restart         Restart all HART OS services
    hart logs [SERVICE]  View service logs (default: all)
    hart join PEER_URL   Join an existing hive network
    hart provision HOST  Provision HART OS on a remote machine
    hart health          Node health report (tier, peers, trust)
    hart update          Update HART OS to latest version
    hart node-id         Print this node's Ed25519 public key
    hart version         Show HART OS version and build info
    hart theme           Show active theme
    hart theme list      List all theme presets
    hart theme set ID    Apply a theme OS-wide
    hart shell open ID   Open a desktop panel
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

HART_VERSION = "1.0.0"
CONFIG_DIR = "/etc/hart"
DATA_DIR = "/var/lib/hart"
INSTALL_DIR = "/opt/hart"

SERVICES = [
    "hart-backend",
    "hart-discovery",
    "hart-agent-daemon",
    "hart-vision",
    "hart-llm",
]


def get_backend_port():
    """Read backend port from env file."""
    env_path = os.path.join(CONFIG_DIR, "hart.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("HARTOS_BACKEND_PORT="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return int(val)
    return 6777


def api_get(path):
    """GET request to local backend."""
    port = get_backend_port()
    try:
        req = urllib.request.Request(f"http://localhost:{port}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def api_post(path, data=None):
    """POST request to local backend."""
    port = get_backend_port()
    try:
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def run_cmd(cmd, check=False):
    """Run a shell command and return output."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "timeout", 1


def cmd_status(_args):
    """Show status of all HART OS services."""
    print(f"\033[36mHART OS {HART_VERSION}\033[0m\n")

    # Node ID
    pub_key_path = os.path.join(DATA_DIR, "node_public.key")
    if os.path.exists(pub_key_path):
        with open(pub_key_path, "rb") as f:
            node_id = f.read().hex()[:16]
        print(f"  Node ID:  {node_id}...")
    else:
        print("  Node ID:  not generated")

    print(f"  Backend:  http://localhost:{get_backend_port()}")
    print()

    # Service statuses
    max_name = max(len(s) for s in SERVICES)
    for svc in SERVICES:
        output, rc = run_cmd(f"systemctl is-active {svc}.service")
        status = output if rc == 0 else "inactive"

        if status == "active":
            color = "\033[32m"  # green
            symbol = "●"
        elif status == "activating":
            color = "\033[33m"  # yellow
            symbol = "◐"
        else:
            color = "\033[90m"  # gray
            symbol = "○"

        print(f"  {color}{symbol}\033[0m {svc:<{max_name}}  {color}{status}\033[0m")

    # Backend health
    health = api_get("/status")
    if health:
        print(f"\n  Backend responding: yes")
    else:
        print(f"\n  Backend responding: \033[31mno\033[0m")


def cmd_start(_args):
    """Start all HART OS services."""
    print("Starting HART OS services...")
    os.system("sudo systemctl start hart.target")
    print("Done. Run 'hart status' to check.")


def cmd_stop(_args):
    """Stop all HART OS services."""
    print("Stopping HART OS services...")
    os.system("sudo systemctl stop hart.target")
    print("Done.")


def cmd_restart(_args):
    """Restart all HART OS services."""
    print("Restarting HART OS services...")
    os.system("sudo systemctl restart hart.target")
    print("Done. Run 'hart status' to check.")


def cmd_logs(args):
    """View HART OS service logs."""
    service = args.service if args.service else "hart-*"
    unit_flag = f"-u {service}" if service != "hart-*" else "-u 'hart-*'"
    lines = args.lines or 50
    cmd = f"journalctl {unit_flag} -n {lines} --no-pager"
    if args.follow:
        cmd += " -f"
    os.system(cmd)


def cmd_join(args):
    """Join an existing hive network."""
    peer_url = args.peer_url
    print(f"Joining hive at {peer_url}...")
    result = api_post("/api/social/peers/announce", {"peer_url": peer_url})
    if result and "error" not in result:
        print(f"Join request sent successfully.")
    else:
        print(f"Failed: {result}")


def cmd_provision(args):
    """Provision HART OS on a remote machine via SSH."""
    host = args.host
    user = args.user or "root"
    print(f"Provisioning HART OS on {user}@{host}...")
    result = api_post(
        "/api/provision/deploy",
        {"target_host": host, "ssh_user": user},
    )
    if result and "error" not in result:
        print(f"Provisioning started. Track with: hart status")
        if "node_id" in result:
            print(f"Remote node ID: {result['node_id']}")
    else:
        print(f"Failed: {result}")


def cmd_health(_args):
    """Show node health report."""
    # Try dashboard endpoint
    health = api_get("/api/social/dashboard/health")
    if health:
        print(f"\033[36mNode Health Report\033[0m\n")
        for key, val in health.items():
            print(f"  {key}: {val}")
    else:
        # Fallback to basic status
        status = api_get("/status")
        if status:
            print(f"\033[36mNode Health Report\033[0m\n")
            print(f"  Status: running")
            print(f"  Port: {get_backend_port()}")
        else:
            print("Backend not responding. Run 'hart start' first.")


def cmd_update(_args):
    """Update HART OS to latest version."""
    print("Checking for updates...")
    # Pull latest from git if available
    if os.path.exists(os.path.join(INSTALL_DIR, ".git")):
        output, rc = run_cmd(f"cd {INSTALL_DIR} && git pull")
        if rc == 0:
            print(f"Updated: {output}")
            print("Restarting services...")
            os.system("sudo systemctl restart hart.target")
            print("Done.")
        else:
            print(f"Update failed: {output}")
    else:
        print("No git repository found. Manual update required.")
        print(f"  1. Download latest bundle")
        print(f"  2. Extract to {INSTALL_DIR}")
        print(f"  3. Run: sudo systemctl restart hart.target")


def cmd_node_id(_args):
    """Print this node's Ed25519 public key."""
    pub_key_path = os.path.join(DATA_DIR, "node_public.key")
    if os.path.exists(pub_key_path):
        with open(pub_key_path, "rb") as f:
            print(f.read().hex())
    else:
        print("Node identity not generated. Run install.sh first.")
        sys.exit(1)


def cmd_theme(args):
    """Manage OS-wide theme."""
    sub = args.theme_action

    if sub == "list":
        result = api_get("/api/social/theme/presets")
        if result and "presets" in result:
            presets = result["presets"]
            active = api_get("/api/social/theme/active") or {}
            active_id = active.get("theme", {}).get("id", "")
            print(f"\033[36mAvailable Themes\033[0m\n")
            for p in presets:
                marker = " \033[32m● active\033[0m" if p["id"] == active_id else ""
                accent = p.get("accent", "6C63FF")
                print(f"  {p['id']:<20} {p['name']:<20} #{accent}{marker}")
                if p.get("description"):
                    print(f"  {'':20} \033[90m{p['description']}\033[0m")
        else:
            print("Failed to fetch themes. Is the backend running?")

    elif sub == "set":
        theme_id = args.theme_id
        if not theme_id:
            print("Usage: hart theme set <theme-id>")
            return
        result = api_post("/api/social/theme/apply", {"theme_id": theme_id})
        if result and result.get("status") == "applied":
            print(f"\033[32mTheme \"{theme_id}\" applied OS-wide.\033[0m")
        else:
            error = result.get("error", "Unknown error") if result else "Backend not responding"
            print(f"\033[31mFailed: {error}\033[0m")

    else:
        # Default: show active theme
        result = api_get("/api/social/theme/active")
        if result and "theme" in result:
            t = result["theme"]
            colors = t.get("colors", {})
            font = t.get("font", {})
            shell = t.get("shell", {})
            print(f"\033[36mActive Theme\033[0m\n")
            print(f"  Name:       {t.get('name', 'Unknown')}")
            print(f"  ID:         {t.get('id', 'Unknown')}")
            print(f"  Category:   {t.get('category', '')}")
            print(f"  Accent:     #{colors.get('accent', '?')}")
            print(f"  Background: #{colors.get('background', '?')}")
            print(f"  Font:       {font.get('family', '?')} {font.get('size', '?')}px")
            print(f"  Blur:       {shell.get('blur_radius', '?')}px")
            print(f"  Opacity:    {shell.get('panel_opacity', '?')}")
        else:
            print("Failed to fetch active theme. Is the backend running?")


def cmd_shell(args):
    """Open a desktop panel via LiquidUI."""
    panel_id = args.panel_id
    if not panel_id:
        print("Usage: hart shell open <panel-id>")
        print("\nPanel IDs: feed, search, communities, campaigns, coding,")
        print("  tracker, agents_browse, agent_audit, resonance, regions,")
        print("  encounters, autopilot, notifications, backup, appearance,")
        print("  recipes, achievements, challenges, kids, seasons,")
        print("  admin, admin_users, admin_mod, admin_agents, ...")
        return

    liquid_ui_port = os.environ.get("HART_LIQUID_UI_PORT", "6800")
    try:
        body = json.dumps({"panel_id": panel_id}).encode()
        req = urllib.request.Request(
            f"http://localhost:{liquid_ui_port}/api/shell/open",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        if result.get("status") == "opened":
            print(f"Opened panel: {panel_id}")
        else:
            print(f"Response: {result}")
    except Exception as e:
        print(f"Failed to open panel (is LiquidUI running on port {liquid_ui_port}?): {e}")


def cmd_version(_args):
    """Show version info."""
    print(f"HART OS {HART_VERSION}")
    print(f"Install: {INSTALL_DIR}")
    print(f"Config:  {CONFIG_DIR}")
    print(f"Data:    {DATA_DIR}")

    # Check if running from ISO-installed distro
    os_release = "/etc/os-release"
    if os.path.exists(os_release):
        with open(os_release) as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    name = line.split("=", 1)[1].strip().strip('"')
                    print(f"OS:      {name}")
                    break


def main():
    parser = argparse.ArgumentParser(
        prog="hart",
        description="HART OS CLI - Manage your agentic intelligence node",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # status
    subparsers.add_parser("status", help="Show service status")

    # start/stop/restart
    subparsers.add_parser("start", help="Start all services")
    subparsers.add_parser("stop", help="Stop all services")
    subparsers.add_parser("restart", help="Restart all services")

    # logs
    logs_parser = subparsers.add_parser("logs", help="View service logs")
    logs_parser.add_argument("service", nargs="?", help="Service name (default: all)")
    logs_parser.add_argument("-n", "--lines", type=int, default=50, help="Number of lines")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow log output")

    # join
    join_parser = subparsers.add_parser("join", help="Join a hive network")
    join_parser.add_argument("peer_url", help="URL of peer to join")

    # provision
    prov_parser = subparsers.add_parser("provision", help="Provision remote machine")
    prov_parser.add_argument("host", help="Target host IP/hostname")
    prov_parser.add_argument("-u", "--user", default="root", help="SSH user")

    # health
    subparsers.add_parser("health", help="Node health report")

    # update
    subparsers.add_parser("update", help="Update HART OS")

    # node-id
    subparsers.add_parser("node-id", help="Print node public key")

    # version
    subparsers.add_parser("version", help="Show version info")

    # theme
    theme_parser = subparsers.add_parser("theme", help="Manage OS-wide theme")
    theme_parser.add_argument(
        "theme_action", nargs="?", default="show",
        choices=["show", "list", "set"],
        help="Theme sub-command (default: show active)",
    )
    theme_parser.add_argument("theme_id", nargs="?", help="Theme ID (for 'set')")

    # shell
    shell_parser = subparsers.add_parser("shell", help="Desktop shell commands")
    shell_sub = shell_parser.add_subparsers(dest="shell_action")
    shell_open = shell_sub.add_parser("open", help="Open a desktop panel")
    shell_open.add_argument("panel_id", nargs="?", help="Panel ID to open")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "logs": cmd_logs,
        "join": cmd_join,
        "provision": cmd_provision,
        "health": cmd_health,
        "update": cmd_update,
        "node-id": cmd_node_id,
        "version": cmd_version,
        "theme": cmd_theme,
        "shell": cmd_shell,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
