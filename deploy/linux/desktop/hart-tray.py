#!/usr/bin/env python3
"""
HART OS System Tray - Desktop indicator showing node status.

Shows:
- Node status (green/yellow/red icon)
- Quick actions (open dashboard, view logs, restart)
- Consent popup when agent needs approval

Requires: pip install pystray Pillow
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: pystray and Pillow required.")
    print("  pip install pystray Pillow")
    sys.exit(1)


BACKEND_PORT = 6777
CHECK_INTERVAL = 15  # seconds


def _api_get(path):
    """GET request to local backend."""
    try:
        url = f"http://localhost:{BACKEND_PORT}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def create_icon(color="green"):
    """Create a colored circle icon with 'H' letter."""
    colors = {
        "green": (78, 205, 196),   # HART teal
        "yellow": (255, 215, 0),   # Warning
        "red": (255, 107, 107),    # Error
        "gray": (128, 128, 128),   # Offline
    }
    rgb = colors.get(color, colors["gray"])
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=rgb + (255,))

    # H letter in center — use a readable font
    try:
        from PIL import ImageFont
        font = None
        for font_path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
        ]:
            if os.path.exists(font_path):
                font = ImageFont.truetype(font_path, 32)
                break
        if font is None:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), "H", font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((32 - tw // 2, 32 - th // 2 - 2), "H",
                  fill=(255, 255, 255, 255), font=font)
    except Exception:
        # Absolute fallback: centered with default font
        draw.text((24, 18), "H", fill=(255, 255, 255, 255))
    return img


def open_dashboard(_icon=None, _item=None):
    """Open dashboard in default browser."""
    subprocess.Popen(
        ["xdg-open", f"http://localhost:{BACKEND_PORT}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def view_logs(_icon=None, _item=None):
    """Open terminal with logs."""
    terminal_cmds = [
        ["gnome-terminal", "--", "journalctl", "-u", "hart-*", "-f", "-n", "100"],
        ["xterm", "-e", "journalctl -u 'hart-*' -f -n 100"],
        ["konsole", "-e", "journalctl", "-u", "hart-*", "-f", "-n", "100"],
    ]
    for cmd in terminal_cmds:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            continue


def restart_services(_icon=None, _item=None):
    """Restart all HART OS services."""
    subprocess.run(
        ["pkexec", "systemctl", "restart", "hart.target"],
        capture_output=True,
    )


def stop_services(_icon=None, _item=None):
    """Stop all HART OS services."""
    subprocess.run(
        ["pkexec", "systemctl", "stop", "hart.target"],
        capture_output=True,
    )


def quit_tray(icon, _item=None):
    """Quit the tray indicator."""
    icon.stop()


class HartTray:
    """System tray indicator for HART OS."""

    def __init__(self):
        self.status = "unknown"
        self.node_id = self._get_node_id()
        self.icon = None
        self._running = True

    def _get_node_id(self):
        """Read node ID from public key file."""
        key_path = "/var/lib/hart/node_public.key"
        try:
            with open(key_path, "rb") as f:
                return f.read().hex()[:16]
        except FileNotFoundError:
            return "not-initialized"

    def _check_status(self):
        """Poll backend for status. Detects degraded state."""
        while self._running:
            result = _api_get("/status")
            if result:
                # Check for degraded state (backend up, other services down)
                health = _api_get("/api/social/dashboard/health")
                if health and isinstance(health.get('services'), dict):
                    svc_states = health['services']
                    down_count = sum(1 for v in svc_states.values()
                                     if v not in ('active', 'n/a'))
                    if down_count > 0:
                        new_status = "yellow"
                    else:
                        new_status = "green"
                else:
                    new_status = "green"
            else:
                new_status = "red"

            if new_status != self.status:
                self.status = new_status
                if self.icon:
                    self.icon.icon = create_icon(self.status)
                    status_labels = {
                        "green": "running",
                        "yellow": "degraded",
                        "red": "offline",
                    }
                    self.icon.title = f"HART OS - {status_labels.get(self.status, self.status)}"

            time.sleep(CHECK_INTERVAL)

    def _build_menu(self):
        """Build the tray context menu."""
        return pystray.Menu(
            pystray.MenuItem(
                f"HART OS - Node: {self.node_id}...",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Dashboard", open_dashboard, default=True),
            pystray.MenuItem("View Logs", view_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Services", restart_services),
            pystray.MenuItem("Stop Services", stop_services),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_tray),
        )

    def run(self):
        """Start the tray indicator."""
        # Start status polling in background
        status_thread = threading.Thread(target=self._check_status, daemon=True)
        status_thread.start()

        self.icon = pystray.Icon(
            name="hart-os",
            icon=create_icon("gray"),
            title="HART OS - starting...",
            menu=self._build_menu(),
        )
        self.icon.run()
        self._running = False


def main():
    tray = HartTray()
    tray.run()


if __name__ == "__main__":
    main()
