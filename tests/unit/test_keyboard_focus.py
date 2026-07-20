"""Behavioral tests for the GTK4 layer-shell glass-shell host keyboard focus.

LIVE-OS #2: keyboard/typing was dead because the wlr-layer-shell WebView never
reliably grabbed focus. On a BACKGROUND layer-shell surface with
KeyboardMode.ON_DEMAND the compositor does not auto-route keys to the surface,
and GTK's grab_focus() is a no-op on a widget that is not yet realized/mapped,
so a single post-present() grab can fire before the surface exists and the caret
stays dead. The fix wires grab_focus() to the realize AND map signals and to a
pointer-press gesture, while keeping the layer surface's keyboard-interactivity
request (ON_DEMAND) so the compositor routes keys once we hold focus.

These are NOT grep tests. We extract the ACTUAL embedded GTK4 host program from
``nixos/modules/hart-layer-shell-host.nix`` (the same source bytes Nix renders
into ``python -c "..."``), mock ONLY the GTK/WebKit/gtk4-layer-shell boundary
(unavailable on a Windows dev box and on a headless CI runner), exec the real
program, build the real ``GlassShellLayer``, then drive the real signal handlers
and assert the observable side effect: ``WebView.grab_focus()`` is called.

Boundary mocked: ``gi`` / ``gi.repository`` (Gtk, WebKit, Gtk4LayerShell, Gdk).
Everything tested below is the host's own Python, unmodified.
"""

import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NIX_FILE = REPO_ROOT / "nixos" / "modules" / "hart-layer-shell-host.nix"


def _extract_host_program() -> str:
    """Pull the embedded ``python -c "..."`` host program out of the nix module.

    The program is authored flush-left inside a Nix ``''`` string, delimited by
    the ``/bin/python -c "`` launch line and a lone closing ``"`` line. The two
    Nix interpolations it carries (the WebKit hardware-accel policy enum and the
    LiquidUI port) are resolved to concrete tokens so the result parses as pure
    Python without changing any behavior under test.
    """
    text = NIX_FILE.read_text(encoding="utf-8")
    lines = text.splitlines()

    start = None
    for i, ln in enumerate(lines):
        if '/bin/python -c "' in ln:
            start = i + 1
            break
    assert start is not None, "could not find the embedded `python -c` host program"

    end = None
    for j in range(start, len(lines)):
        if lines[j].strip() == '"':
            end = j
            break
    assert end is not None, "could not find the closing quote of the host program"

    prog = "\n".join(lines[start:end])
    # ${if ui.preferHardwareGL then "ON_DEMAND" else "NEVER"} -> an enum attr name.
    prog = re.sub(r"\$\{if[^}]*\}", "NEVER", prog)
    # ${liquidPort} (and any other simple interpolation) -> a port literal.
    prog = re.sub(r"\$\{[^}]*\}", "6800", prog)
    assert "${" not in prog, "unresolved Nix interpolation left in host program"
    return prog


def _load_host(monkeypatch, tmp_path):
    """Exec the real host program against a mocked GTK stack.

    Returns ``(GlassShellLayer_instance, Gtk, WebKit, LayerShell)``.
    """
    Gtk = MagicMock(name="Gtk")
    WebKit = MagicMock(name="WebKit")
    LayerShell = MagicMock(name="Gtk4LayerShell")
    Gdk = MagicMock(name="Gdk")

    gi_mod = types.ModuleType("gi")
    gi_mod.require_version = MagicMock(name="require_version")
    repo = types.ModuleType("gi.repository")
    repo.Gtk = Gtk
    repo.WebKit = WebKit
    repo.Gtk4LayerShell = LayerShell
    repo.Gdk = Gdk
    gi_mod.repository = repo

    monkeypatch.setitem(sys.modules, "gi", gi_mod)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    # Keep the first-paint marker write inside the test sandbox if it ever fires.
    monkeypatch.setenv("HART_SHELL_READY_FLAG", str(tmp_path / "shell-ready"))

    prog = _extract_host_program()
    ns: dict = {}
    exec(compile(prog, str(NIX_FILE), "exec"), ns)  # noqa: S102 - our own source

    assert "GlassShellLayer" in ns, "host program no longer defines GlassShellLayer"
    shell = ns["GlassShellLayer"](MagicMock(name="app"))
    return shell, Gtk, WebKit, LayerShell


def _signal_handlers(connect_mock):
    """Map signal-name -> handler from a mock's recorded ``.connect`` calls."""
    return {
        call.args[0]: call.args[1]
        for call in connect_mock.call_args_list
        if len(call.args) >= 2
    }


def test_keyboard_interactivity_is_requested(monkeypatch, tmp_path):
    """The layer surface must request keyboard-interactivity (on-demand or
    exclusive) so the compositor will route keys to it once it holds focus."""
    _shell, _Gtk, _WebKit, LayerShell = _load_host(monkeypatch, tmp_path)

    LayerShell.set_keyboard_mode.assert_called()
    mode = LayerShell.set_keyboard_mode.call_args.args[1]
    assert mode in (
        LayerShell.KeyboardMode.ON_DEMAND,
        LayerShell.KeyboardMode.EXCLUSIVE,
    ), "keyboard mode must be ON_DEMAND or EXCLUSIVE so keys are routed to the shell"


def test_grab_focus_wired_to_realize_signal(monkeypatch, tmp_path):
    """realize -> grab_focus(): focus is taken the moment the surface has a
    backing GdkSurface (earliest point a grab can stick)."""
    shell, _Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    handlers = _signal_handlers(shell._win.connect)
    assert "realize" in handlers, "host must connect the window 'realize' signal"

    shell._webview.grab_focus.reset_mock()
    handlers["realize"](shell._win)
    shell._webview.grab_focus.assert_called_once()


def test_grab_focus_wired_to_map_signal(monkeypatch, tmp_path):
    """map -> grab_focus(): the reliable focus point. grab_focus() on an
    unmapped widget is a no-op, so the map-time re-grab is what actually makes
    the first keystrokes land."""
    shell, _Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    handlers = _signal_handlers(shell._win.connect)
    assert "map" in handlers, "host must connect the window 'map' signal"

    shell._webview.grab_focus.reset_mock()
    handlers["map"](shell._win)
    shell._webview.grab_focus.assert_called_once()


def test_grab_focus_wired_to_pointer_press(monkeypatch, tmp_path):
    """A pointer-press gesture -> grab_focus(): clicking the orb / command bar
    re-asserts focus even after it drifted to a native window on top."""
    shell, Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    gesture = Gtk.GestureClick.new.return_value
    handlers = _signal_handlers(gesture.connect)
    assert "pressed" in handlers, "host must connect a click gesture 'pressed' signal"

    shell._webview.grab_focus.reset_mock()
    # GestureClick 'pressed' callback signature: (gesture, n_press, x, y).
    handlers["pressed"](gesture, 1, 0.0, 0.0)
    shell._webview.grab_focus.assert_called_once()


def test_grab_focus_after_load_finished(monkeypatch, tmp_path):
    """Regression guard for the pre-existing behavior: focus is re-grabbed once
    the page finishes loading (so typing works after the shell JS has run)."""
    shell, _Gtk, WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    shell._webview.grab_focus.reset_mock()
    shell._on_load_changed(shell._webview, WebKit.LoadEvent.FINISHED)
    shell._webview.grab_focus.assert_called_once()


def test_grab_focus_called_during_construction(monkeypatch, tmp_path):
    """The post-present() grab (the pre-existing path) still fires at build."""
    shell, _Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)
    shell._webview.grab_focus.assert_called()


def test_pointer_press_does_not_steal_the_click(monkeypatch, tmp_path):
    """The focus gesture must observe presses without claiming them, so a click
    still reaches the web content. We assert it runs in the CAPTURE phase and
    never forces the gesture into the CLAIMED state."""
    shell, Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    gesture = Gtk.GestureClick.new.return_value
    gesture.set_propagation_phase.assert_called_once_with(
        Gtk.PropagationPhase.CAPTURE
    )
    # The handler only grabs focus; it must not set the gesture state (which is
    # how a gesture would swallow the press away from the WebView).
    handlers = _signal_handlers(gesture.connect)
    gesture.set_state.reset_mock()
    handlers["pressed"](gesture, 1, 0.0, 0.0)
    gesture.set_state.assert_not_called()


# ── Boundary coverage: missing-tool / offline / error paths ──────────────────


def test_key_controller_wired_so_keys_reach_the_focused_shell(monkeypatch, tmp_path):
    """Keyboard-interactivity is configured at the TOOLKIT level, not only the
    layer surface: grabbing focus is useless unless a key controller is attached
    to deliver the keystrokes. Assert an EventControllerKey is connected to
    'key-pressed' AND added to the window, then drive the real handler to prove a
    key actually reaches the host (a non-F12 key propagates, i.e. returns False)."""
    shell, Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    keyctl = Gtk.EventControllerKey.new.return_value
    handlers = _signal_handlers(keyctl.connect)
    assert "key-pressed" in handlers, "host must connect a key controller 'key-pressed'"
    shell._win.add_controller.assert_any_call(keyctl)

    # EventControllerKey 'key-pressed' signature: (controller, keyval, keycode,
    # state). An arbitrary (non-F12) key must propagate -> handler returns False.
    assert handlers["key-pressed"](keyctl, 65, 38, 0) is False


def test_load_changed_ignores_non_finished_event(monkeypatch, tmp_path):
    """Boundary: while the page is STILL loading (event != FINISHED), the host
    must NOT grab focus early and must NOT touch the first-paint readiness marker
    (a premature touch would tell the paint-watchdog the tier is healthy before it
    has painted). Only the FINISHED event drives the grab + the marker."""
    shell, _Gtk, WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)
    ready_flag = tmp_path / "shell-ready"

    shell._webview.grab_focus.reset_mock()
    # WebKit is a MagicMock, so LoadEvent.STARTED is a distinct sentinel that is
    # NOT equal to LoadEvent.FINISHED -> the FINISHED branch must be skipped.
    shell._on_load_changed(shell._webview, WebKit.LoadEvent.STARTED)

    shell._webview.grab_focus.assert_not_called()
    assert not ready_flag.exists(), "readiness marker must not fire before load FINISHED"


def test_signal_painted_survives_unwritable_ready_flag(monkeypatch, tmp_path):
    """Error boundary (missing dir / read-only /run / ENOSPC / permission denied):
    writing the first-paint marker MUST never crash the shell. The host wraps the
    write in ``except OSError: pass`` so the supervisor degrades safely (a missing
    marker escalates DOWN to the cage floor) instead of taking the session down.
    Prove it: make the marker write raise OSError and confirm load-FINISHED still
    grabs focus and does NOT propagate the error."""
    shell, _Gtk, WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    def _boom(*_a, **_k):
        raise OSError("simulated: ready-flag dir is read-only / missing")

    # The host's _signal_painted() does os.makedirs(...) then open(..., 'w'). Make
    # the very first filesystem call fail; the except OSError must swallow it.
    monkeypatch.setattr("os.makedirs", _boom)

    shell._webview.grab_focus.reset_mock()
    # Must NOT raise even though the marker write fails underneath.
    shell._on_load_changed(shell._webview, WebKit.LoadEvent.FINISHED)
    # Focus is still grabbed: graceful degradation, not a crash.
    shell._webview.grab_focus.assert_called_once()


def test_focus_signals_connected_before_present(monkeypatch, tmp_path):
    """Regression-ordering guard. grab_focus() on an unmapped widget is a no-op,
    so the realize/map handlers MUST be connected BEFORE present() — otherwise the
    map signal fires during present() with no handler attached and the first
    keystrokes die (exactly the 'typing is dead' regression). Assert, on the real
    construction sequence, that the 'realize' and 'map' connects precede present()."""
    shell, _Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    calls = shell._win.mock_calls
    present_idx = next(
        i for i, c in enumerate(calls) if c[0] == "present"
    )
    realize_idx = next(
        i for i, c in enumerate(calls)
        if c[0] == "connect" and c.args and c.args[0] == "realize"
    )
    map_idx = next(
        i for i, c in enumerate(calls)
        if c[0] == "connect" and c.args and c.args[0] == "map"
    )
    assert realize_idx < present_idx, "'realize' must be connected before present()"
    assert map_idx < present_idx, "'map' must be connected before present()"


def test_webview_loads_offline_fallback_url_when_handed_one(monkeypatch, tmp_path):
    """Offline boundary: when LiquidUI (:6800) is down the bash wrapper probes and
    hands the host a fallback URL via ``HART_SHELL_URL`` (the Nunba SPA) so the
    surface is NEVER blank. The host must honour that env var and point the WebView
    at it — proving the never-blank-surface contract survives the toolkit port."""
    monkeypatch.setenv("HART_SHELL_URL", "http://localhost:5000")
    shell, _Gtk, _WebKit, _LayerShell = _load_host(monkeypatch, tmp_path)

    shell._webview.load_uri.assert_called_once_with("http://localhost:5000")


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    sys.exit(pytest.main([__file__, "-v"]))
