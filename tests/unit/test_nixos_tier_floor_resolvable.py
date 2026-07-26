"""Regression guards for the real-HW Tier-ladder boot (journal 2026-06-24): the
cage FLOOR must be launchable, and the shell server must be up before the paint-
watchdog fires. These are config-shape guards — the behavioural proof is the
session-supervisor + floor-lock nixosTests in CI (which can't run on the Windows
dev box). Each locks down one root cause the live boot hit:

  1. cage floor 'command not found' (rc=127, crash-loop on the floor): the
     supervisor's cageCommand option default is the BARE "hart-shell-session",
     which is NOT on the greetd selector's PATH. hart-liquid-ui must point it at
     the ABSOLUTE store path (like compCommand/swayCommand already do).
  2. :6800 shell server started too late: it ordered `after hart-model-bus`
     (model loading is slow), so the glass host's 30s /health wait outran the
     watchdog and Tier-1/2 dropped to cage. The shell server degrades gracefully
     without the model bus, so it must NOT order after it (only `wants` it).
  3. The 20s shell-paint watchdog was SHORTER than the host's 30s :6800 wait, so a
     tier legitimately waiting for the backend was killed mid-wait. The desktop
     watchdog must outlast that wait.
"""
import pathlib
import re

_NIXOS = pathlib.Path(__file__).resolve().parents[2] / "nixos"


def _read(rel: str) -> str:
    return (_NIXOS / rel).read_text(encoding="utf-8")


def test_cage_floor_command_is_absolute_store_path():
    liquid = _read("modules/hart-liquid-ui.nix")
    # hart-liquid-ui must set the supervisor's cage floor to the ABSOLUTE launcher,
    # not leave it as the bare "hart-shell-session" the greetd selector can't resolve.
    assert re.search(
        r"hart\.sessionSupervisor\.cageCommand\s*=.*kioskLauncher.*hart-shell-session",
        liquid, re.S), (
        "hart-liquid-ui must point hart.sessionSupervisor.cageCommand at the ABSOLUTE "
        "${kioskLauncher}/bin/hart-shell-session — the bare 'hart-shell-session' "
        "default isn't on the greetd selector PATH, so the floor crash-loops with "
        "'command not found' (rc=127).")


def test_liquid_ui_does_not_block_on_model_bus():
    liquid = _read("modules/hart-liquid-ui.nix")
    # The hart-liquid-ui service's `after = [...]` must NOT list hart-model-bus
    # (which would delay :6800 behind slow model loading).
    m = re.search(
        r"systemd\.services\.hart-liquid-ui\s*=\s*\{.*?\bafter\s*=\s*\[([^\]]*)\]",
        liquid, re.S)
    assert m, "could not locate the hart-liquid-ui service `after = [...]`"
    after = m.group(1)
    assert "hart-model-bus" not in after, (
        ":6800 (hart-liquid-ui) must NOT order `after hart-model-bus` — it serves the "
        "shell without the model bus (degrades to static UI), and waiting for model "
        "loading made the glass host's /health wait outrun the paint-watchdog so "
        "Tier-1/2 dropped to cage.")
    # …but it must still `wants` it, so the model bus starts concurrently (the
    # generative UI activates once it appears).
    assert re.search(r"wants\s*=\s*\[[^\]]*hart-model-bus", liquid), (
        "hart-liquid-ui should still `wants` hart-model-bus (started concurrently, "
        "not ordered-after).")


def test_desktop_watchdog_outlasts_shell_health_wait():
    desktop = _read("configurations/desktop.nix")
    host = _read("modules/hart-layer-shell-host.nix")
    # The GTK4 host waits `for i in $(seq 1 N)` seconds for :6800/health before it
    # can paint its first frame; the watchdog must be strictly longer than that.
    wm = re.search(r"for i in \$\(seq 1 (\d+)\)", host)
    host_wait = int(wm.group(1)) if wm else 30
    sm = re.search(r"shellPaintTimeoutSeconds\s*=\s*(\d+)", desktop)
    assert sm, "desktop.nix must set hart.sessionSupervisor.shellPaintTimeoutSeconds"
    watchdog = int(sm.group(1))
    assert watchdog > host_wait, (
        f"shellPaintTimeoutSeconds ({watchdog}s) must exceed the glass host's :6800 "
        f"/health wait ({host_wait}s) — otherwise a tier legitimately waiting for the "
        f"backend is killed mid-wait and drops to cage (the real-HW 2026-06-24 boot).")


def test_gtk4_host_ld_preloads_gtk4_layer_shell():
    host = _read("modules/hart-layer-shell-host.nix")
    # gtk4-layer-shell interposes libwayland-client's symbols, so it MUST load BEFORE
    # libwayland; pulled in lazily via the GI typelib it loads too late and
    # `LayerShell.init_for_window` silently fails ("GtkWindow is not a layer surface") ->
    # the GTK4 host never anchors the BACKGROUND desktop -> Tier-1 (hart-comp) + Tier-2
    # (sway) are declared HUNG and fall to cage (the real-HW 2026-06-24 boot). The launcher
    # must LD_PRELOAD the runtime .so to force early load (gtk4-layer-shell's own fix).
    assert re.search(r"libgtk4-layer-shell\.so\*.*LD_PRELOAD", host, re.S), (
        "the GTK4 layer-shell host launcher must LD_PRELOAD libgtk4-layer-shell.so "
        "(globbed from the gtk4-layer-shell store path) before exec'ing the python host — "
        "without it the layer surface never initialises and Tier-1/Tier-2 drop to cage.")


def test_sway_tier2_enables_touchpad_tap():
    host = _read("modules/hart-layer-shell-host.nix")
    # libinput defaults tap-to-click OFF, so a light tap on a laptop touchpad clicks
    # nothing (the real-HW 2026-06-24 "taps not registering"). When sway hosts Tier-2 it
    # must enable tap on touchpads (hart-comp Tier-1 does the same in udev.rs).
    assert re.search(r'input\s+"type:touchpad"\s*\{[^}]*\btap\s+enabled', host, re.S), (
        'the sway Tier-2 host config must enable touchpad tap-to-click '
        '(input "type:touchpad" { tap enabled }) — libinput defaults it off.')
