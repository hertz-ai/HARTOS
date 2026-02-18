#!/usr/bin/env python3
"""
HyveOS CLI - Command-line interface for managing HyveOS services.

Usage:
    hyve status          Show all service states + node identity
    hyve start           Start all HyveOS services
    hyve stop            Stop all HyveOS services
    hyve restart         Restart all HyveOS services
    hyve logs [SERVICE]  View service logs (default: all)
    hyve join PEER_URL   Join an existing hive network
    hyve provision HOST  Provision HyveOS on a remote machine
    hyve health          Node health report (tier, peers, trust)
    hyve update          Update HyveOS to latest version
    hyve node-id         Print this node's Ed25519 public key
    hyve version         Show HyveOS version and build info
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

HYVE_VERSION = "1.0.0"
CONFIG_DIR = "/etc/hyve"
DATA_DIR = "/var/lib/hyve"
INSTALL_DIR = "/opt/hyve"

SERVICES = [
    "hyve-backend",
    "hyve-discovery",
    "hyve-agent-daemon",
    "hyve-vision",
    "hyve-llm",
]


def get_backend_port():
    """Read backend port from env file."""
    env_path = os.path.join(CONFIG_DIR, "hyve.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("HYVE_BACKEND_PORT="):
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
    """Show status of all HyveOS services."""
    print(f"\033[36mHyveOS {HYVE_VERSION}\033[0m\n")

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
    """Start all HyveOS services."""
    print("Starting HyveOS services...")
    os.system("sudo systemctl start hyve.target")
    print("Done. Run 'hyve status' to check.")


def cmd_stop(_args):
    """Stop all HyveOS services."""
    print("Stopping HyveOS services...")
    os.system("sudo systemctl stop hyve.target")
    print("Done.")


def cmd_restart(_args):
    """Restart all HyveOS services."""
    print("Restarting HyveOS services...")
    os.system("sudo systemctl restart hyve.target")
    print("Done. Run 'hyve status' to check.")


def cmd_logs(args):
    """View HyveOS service logs."""
    service = args.service if args.service else "hyve-*"
    unit_flag = f"-u {service}" if service != "hyve-*" else "-u 'hyve-*'"
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
    """Provision HyveOS on a remote machine via SSH."""
    host = args.host
    user = args.user or "root"
    print(f"Provisioning HyveOS on {user}@{host}...")
    result = api_post(
        "/api/provision/deploy",
        {"target_host": host, "ssh_user": user},
    )
    if result and "error" not in result:
        print(f"Provisioning started. Track with: hyve status")
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
            print("Backend not responding. Run 'hyve start' first.")


def cmd_update(_args):
    """Update HyveOS to latest version."""
    print("Checking for updates...")
    # Pull latest from git if available
    if os.path.exists(os.path.join(INSTALL_DIR, ".git")):
        output, rc = run_cmd(f"cd {INSTALL_DIR} && git pull")
        if rc == 0:
            print(f"Updated: {output}")
            print("Restarting services...")
            os.system("sudo systemctl restart hyve.target")
            print("Done.")
        else:
            print(f"Update failed: {output}")
    else:
        print("No git repository found. Manual update required.")
        print(f"  1. Download latest bundle")
        print(f"  2. Extract to {INSTALL_DIR}")
        print(f"  3. Run: sudo systemctl restart hyve.target")


def cmd_node_id(_args):
    """Print this node's Ed25519 public key."""
    pub_key_path = os.path.join(DATA_DIR, "node_public.key")
    if os.path.exists(pub_key_path):
        with open(pub_key_path, "rb") as f:
            print(f.read().hex())
    else:
        print("Node identity not generated. Run install.sh first.")
        sys.exit(1)


def cmd_version(_args):
    """Show version info."""
    print(f"HyveOS {HYVE_VERSION}")
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
        prog="hyve",
        description="HyveOS CLI - Manage your agentic intelligence node",
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
    subparsers.add_parser("update", help="Update HyveOS")

    # node-id
    subparsers.add_parser("node-id", help="Print node public key")

    # version
    subparsers.add_parser("version", help="Show version info")

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
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
