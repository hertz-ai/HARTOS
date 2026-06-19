#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════════════════════
# HART-comp M2 — STAGE F: the REAL liquid-glass shell as a wlr-layer-shell
# BACKGROUND surface, hosted by GTK4 + WebKitGTK-6.0 + gtk4-layer-shell,
# pointed at HART-comp's wayland socket.
# ════════════════════════════════════════════════════════════════════════════
#
# This is the standalone-WSL adaptation of nixos/modules/hart-layer-shell-host.nix
# (the Phase-4 host python, lines 120-218). It is NOT a second shell renderer —
# it re-hosts the SAME served shell (LiquidUIService render_desktop_shell +
# /shell/static) the nix module serves. Z-ORDER MODEL (1): ONE layer-shell surface
# (one WebView), overlays/orb co-planar, BACKGROUND layer, exclusive zone 0.
#
# Co-located in-process (matches the steward-authoritative desktop model): the
# shell SERVER (LiquidUIService/waitress) runs in a daemon thread of THIS process,
# and the GTK4 WebKit host runs on the main thread. One process, one lifecycle —
# which also sidesteps the cross-process detach flakiness of this WSL harness.
#
# Run as a layer-shell client of HART-comp:
#   WAYLAND_DISPLAY=<hart-comp socket>  (e.g. wayland-2)
#   GI_TYPELIB_PATH=<gtk4-layer-shell typelib dir>
#   LD_LIBRARY_PATH=<gtk4-layer-shell lib dir>
import os
import sys
import threading
import time
import urllib.request

REPO = os.environ.get("HART_REPO", "/mnt/c/Users/sathi/PycharmProjects/HARTOS")
if REPO not in sys.path:
    sys.path.insert(0, REPO)
os.environ.setdefault("HART_DATA_DIR", "/tmp/m2-hart-data")
os.makedirs(os.environ["HART_DATA_DIR"], exist_ok=True)

# Authoritative diagnostics file — python writes + flushes each milestone here so we
# can read it independent of shell-stdout flakiness in this WSL harness.
STATUS = os.environ.get("HART_GLASS_STATUS", "/tmp/m2-glass-status.log")


def status(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        with open(STATUS, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
    print(msg, flush=True)

PORT = int(os.environ.get("HART_LIQUID_PORT", "6800"))
SHELL_URL = f"http://127.0.0.1:{PORT}"

# ── 1. Boot the SAME served glass shell (LiquidUIService) in a daemon thread ──
from integrations.agent_engine.liquid_ui_service import LiquidUIService  # noqa: E402

_svc = LiquidUIService(port=PORT)
_app = _svc._create_flask_app()


def _serve_shell():
    from waitress import serve
    serve(_app, host="127.0.0.1", port=PORT, threads=4)


threading.Thread(target=_serve_shell, daemon=True, name="liquid-ui").start()

# Wait for /health (the SAME readiness gate the nix host's curl loop uses).
_ready = False
for _ in range(60):
    try:
        if urllib.request.urlopen(f"{SHELL_URL}/health", timeout=3).status == 200:
            _ready = True
            break
    except Exception:
        pass
    time.sleep(0.25)
status(f"shell server ready={_ready} at {SHELL_URL}")
# Confirm the dead-husk-critical asset actually serves before we point a WebView
# at it (never trust inline render — fetch /shell/static for real).
try:
    _js = urllib.request.urlopen(f"{SHELL_URL}/shell/static/hartDesktop.js", timeout=3)
    status(f"/shell/static/hartDesktop.js -> {_js.status}")
except Exception as e:
    status(f"static probe FAILED: {e!r}")

# ── 2. The GTK4 WebKit layer-shell host (mirrors hart-layer-shell-host.nix) ──
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")          # GTK4 binding (NOT WebKit2 / GTK3)
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, WebKit, Gtk4LayerShell as LayerShell  # noqa: E402


class GlassShellLayer:
    """The glass shell as a GTK4 wlr-layer-shell BACKGROUND surface (Model 1)."""

    def __init__(self, app):
        self._win = Gtk.ApplicationWindow(application=app)

        # init_for_window MUST run before present() — converts the toplevel into a
        # zwlr_layer_surface_v1 the compositor stacks by layer.
        LayerShell.init_for_window(self._win)
        # BACKGROUND == the desktop wallpaper plane: below every native toplevel.
        LayerShell.set_layer(self._win, LayerShell.Layer.BACKGROUND)
        # Anchor all four edges => the surface spans the whole output.
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                     LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self._win, edge, True)
        # Exclusive zone 0: the desktop is the backdrop, reserves no space.
        LayerShell.set_exclusive_zone(self._win, 0)
        # ON_DEMAND + an explicit grab_focus() after present() (the typing-dead fix).
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.ON_DEMAND)

        webview = WebKit.WebView()
        webview.load_uri(SHELL_URL)
        s = webview.get_settings()
        s.set_enable_javascript(True)
        s.set_enable_developer_extras(True)
        # NEVER: software-GL/llvmpipe paint floor (the cage/sway/hart-comp contract).
        s.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.NEVER)
        self._webview = webview

        self._win.set_child(webview)
        keyctl = Gtk.EventControllerKey.new()
        keyctl.connect("key-pressed", self._on_key)
        self._win.add_controller(keyctl)
        self._win.present()
        self._webview.grab_focus()
        # AUTHORITATIVE assertion: did init_for_window actually make this a layer
        # surface? gtk4-layer-shell exposes is_layer_window(); if the LD_PRELOAD
        # hook failed this is False and the window is a normal xdg-toplevel.
        try:
            is_layer = bool(LayerShell.is_layer_window(self._win))
        except Exception as e:
            is_layer = f"is_layer_window errored: {e!r}"
        status(f"GTK4 window presented; is_layer_window={is_layer} "
               f"(BACKGROUND, anchored 4 edges, exclusive-zone 0)")
        # Re-grab focus after the WebView's first paint so typing works, and log
        # when the page finishes loading (the orb/hero JS has run).
        webview.connect("load-changed", self._on_load)

    def _on_load(self, _wv, event):
        from gi.repository import WebKit as _Wk
        if event == _Wk.LoadEvent.FINISHED:
            status("WebView load FINISHED (glass shell HTML+JS executed)")
            self._webview.grab_focus()
            # Give the shell a moment to run its first paint (orb sine-wave, hero,
            # wallpaper gradient), then self-screenshot the HOST (sway) output via
            # grim and quit. Doing the capture from INSIDE the living process is the
            # only reliable path in this WSL harness (a detached heavy GTK4+WebKit
            # process cannot be kept alive across shell calls).
            from gi.repository import GLib
            GLib.timeout_add(int(os.environ.get("HART_SHOT_DELAY_MS", "3500")),
                             self._screenshot_and_quit)

    def _screenshot_and_quit(self):
        import subprocess
        out = os.environ.get("HART_SHOT_PATH", "/tmp/m2-glass-shell.png")
        host_disp = os.environ.get("HART_HOST_DISPLAY", "wayland-1")
        # grim talks to the SWAY host (which implements zwlr_screencopy_v1) — it
        # composites HART-comp full-screen, so the capture shows HART-comp painting
        # the glass shell. Use a CLEAN env (the LD_PRELOAD'd libgtk4-layer-shell.so
        # must NOT be inherited by grim — it would try to make grim a layer surface).
        env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        env["WAYLAND_DISPLAY"] = host_disp
        try:
            r = subprocess.run(["grim", out], env=env,
                               capture_output=True, text=True, timeout=15)
            status(f"grim rc={r.returncode} out={out} err={r.stderr.strip()[:120]}")
        except Exception as e:
            status(f"grim FAILED: {e!r}")
        self._win.get_application().quit()
        return False  # one-shot

    def _on_key(self, _ctrl, keyval, _keycode, _state):
        from gi.repository import Gdk
        if keyval == Gdk.KEY_F12:
            self._webview.get_inspector().show()
            return True
        return False


def _on_activate(app):
    app.__hart_shell = GlassShellLayer(app)


app = Gtk.Application(application_id="ai.hart.GlassShellLayer")
app.connect("activate", _on_activate)
app.run(None)
