{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — Phase 4: glass shell as a real wlr-layer-shell BACKGROUND surface
#           (the GTK3 → GTK4 WebKitGTK host-window port)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (ROADMAP.md Phase 4 + HART_OS_NATIVE_ARCHITECTURE §L2 + §7.4):
#
#   The cage Tier-3 floor (hart-liquid-ui.nix) runs the glass shell as a GTK3
#   single FULLSCREEN `Gtk.Window` (WebKit2-4.1). That is the audited never-fail
#   floor and it is kept VERBATIM. It is NOT a true wlr-layer-shell surface — it
#   cannot anchor the desktop BELOW agent-placed native toplevels.
#
#   This module is the budgeted GTK3 → GTK4 host-window port the architecture
#   names: a GTK4 + WebKitGTK-6.0 + `gtk4-layer-shell` host window pointed at the
#   SAME served shell (`LiquidUIService :6800` `render_desktop_shell` +
#   `/shell/static`, JS served UNCHANGED), anchored as a BACKGROUND layer with
#   exclusive zone 0 — so native windows (Phase 5) sit ABOVE the desktop while
#   the shell still IS the desktop. It re-litigates the never-fail software-GL
#   paint floor on an UNPROVEN GTK4 stack with its OWN broken-GPU paint proof —
#   this is NOT a config flag and NOT "same WebView", it is a real toolkit port.
#
# ── Z-ORDER MODEL — PICKED IN CODE: MODEL (1) ─────────────────────────────────
#   ROADMAP Phase 4 demands ONE z-order model be chosen in code. We choose
#   MODEL (1): a SINGLE layer-shell surface (one WebView) anchored BACKGROUND,
#   with the voice orb / A2UI overlays / hero CO-PLANAR inside that same WebView.
#
#   Consequence, stated HONESTLY (architecture §7.4): native app windows sit
#   ABOVE the orb/overlays — the orb does NOT float over a focused native window.
#   We accept that limitation DELIBERATELY because it is the ONLY model that keeps
#   the shell's JS UNCHANGED. Model (2) (two WebViews: a background desktop + a
#   top-layer overlay surface) would SEVER the implicit single-window sharing of
#   the `window.*` globals every shell module reaches across (`openPanel`,
#   `acSend`, `HartSession`, `toggleVoice`, `renderAgentOverlay`) and force a real
#   cross-WebView message bus — at which point "JS untouched" is false and the bus
#   must be budgeted. The task contract here is "JS unchanged", so Model (1) is the
#   correct (and only consistent) choice. Promoting the orb to its own top layer is
#   a future Model-(2) upgrade with its own budget, NOT this port.
#
# NEVER-FAIL POSITION (ROADMAP §6 tiering — INVARIANT):
#   Tier-1 = HART-comp (Smithay) → Tier-2 = sway (hart-sway-tier1.nix) →
#   Tier-3/FLOOR = cage + forced-software-GL (hart-liquid-ui.nix, GTK3 path,
#   audited bit-for-bit). This GTK4 layer-shell host is an L2 host-window the
#   higher tiers can adopt; it becomes a greeter-selectable session ONLY, and
#   ONLY after its broken-GPU paint proof passes in CI. **defaultSession STAYS
#   cage.** Nothing here flips it. A GTK4-host crash drops to the GTK3 cage Tier-3
#   under the Phase-1 supervisor (the Phase-4 nixosTest proves this).
#
# STATUS: AUTHORED ON A WINDOWS DEV BOX — NOT BOOTED HERE.
#   No Wayland/wlroots/gtk4-layer-shell/WebKitGTK paint can run on Windows. This
#   Nix expression + the embedded GTK4 host script are authored + structurally
#   validated (test_phase4_layer_shell_host.py + this module's source-guards); the
#   real boot + layer-shell anchoring + the GTK4 broken-GPU paint proof are ALL
#   VM/CI-pending (Linux nixosTest on llvmpipe software-GL, or local QEMU-KVM).
#   Opt-in, default OFF.
#
# DRY / no-parallel-path:
#   - REUSES the SAME served shell (`render_desktop_shell` + `/shell/static`,
#     :6800) the cage floor serves — there is ONE renderer of the HTML/JS. The
#     SECOND host WINDOW (GTK4 vs GTK3) is the explicitly-budgeted toolkit port,
#     NOT a second copy of the shell. The GTK3 cage path is untouched/verbatim.
#   - REUSES the SAME software-GL hardening contract as cage Tier-3 + sway Tier-2
#     + hart-comp (WLR_RENDERER_ALLOW_SOFTWARE / LIBGL_ALWAYS_SOFTWARE /
#     WEBKIT_DISABLE_DMABUF_RENDERER / WEBKIT_DISABLE_COMPOSITING_MODE /
#     HardwareAccelerationPolicy.NEVER) — a kiosk MUST paint on any GPU.
#   - REUSES the SAME `:6800`→Nunba-SPA fallback URL probe the cage wrapper uses,
#     so the GTK4 host is NEVER a blank surface (the dead-husk lesson).

let
  cfg = config.hart;
  ui = config.hart.liquidUI;
  host = config.hart.layerShellHost;

  liquidPort = toString (ui.port or 6800);
  nunbaPort = toString (config.hart.nunba.port or 5000);

  # GObject-introspection typelibs the GTK4 host's gi.require_version needs. The
  # GTK4 stack is a DIFFERENT typelib set than the cage GTK3 floor: GTK4 (Gtk-4.0),
  # WebKitGTK 6.0 (WebKit-6.0, NOT WebKit2-4.1), and gtk4-layer-shell
  # (Gtk4LayerShell-1.0). Same makeSearchPathOutput "out" lesson as the cage floor:
  # several of these (glib, gtk4, ...) have a non-`out` DEFAULT output, so plain
  # makeSearchPath points at the wrong store path and `gi` fails with
  # "Typelib file for namespace 'GObject' not found" (the glass-shell SIGABRT class
  # #99-103). gtk4-layer-shell ships its typelib in `out`.
  giTypelibPath = lib.makeSearchPathOutput "out" "lib/girepository-1.0" (with pkgs; [
    glib gobject-introspection gtk4 webkitgtk_6_0 gtk4-layer-shell
    pango gdk-pixbuf graphene harfbuzz libsoup_3 cairo
  ]);

  # ── The GTK4 layer-shell glass-shell host (Phase-4 L2 host window) ──
  # Mirrors the cage wrapper's URL probe + software-GL hardening (one contract)
  # but hosts the SAME served shell as a GTK4 wlr-layer-shell BACKGROUND surface
  # instead of a GTK3 fullscreen window. The python below is the GTK4/WebKit-6.0
  # equivalent of hart-liquid-ui.nix's GlassShell — the budgeted port, not a copy
  # of the shell.
  layerShellHost = pkgs.writeShellScriptBin "hart-glass-shell-gtk4" ''
    set -euo pipefail
    URL="http://localhost:${liquidPort}"
    for i in $(seq 1 30); do
      if ${pkgs.curl}/bin/curl -sf "$URL/health" >/dev/null 2>&1; then break; fi
      sleep 1
    done
    if ! ${pkgs.curl}/bin/curl -sf "$URL/health" >/dev/null 2>&1; then
      # LiquidUI down — fall back to the Nunba SPA so the surface is never blank
      # (the SAME dead-husk-avoidance the cage floor uses).
      if ${pkgs.curl}/bin/curl -sf "http://localhost:${nunbaPort}/" >/dev/null 2>&1; then
        URL="http://localhost:${nunbaPort}"
      fi
    fi
    # GI typelibs for the GTK4 / WebKit-6.0 / gtk4-layer-shell python below.
    export GI_TYPELIB_PATH="${giTypelibPath}"
    # WebKitGTK robustness on fresh-ISO boots (VM / software GL / no GPU): the
    # DMABUF renderer + GL compositing crash on a GL-less display — exactly the
    # first-boot / live-USB / llvmpipe case. Disable both so a GTK4 host that
    # cannot paint never takes the session down. SAME contract as the cage floor;
    # this is the GTK4 path's OWN broken-GPU proof, not an inherited assumption.
    ${lib.optionalString (!ui.preferHardwareGL) "export WEBKIT_DISABLE_DMABUF_RENDERER=1\nexport WEBKIT_DISABLE_COMPOSITING_MODE=1"}
    # ── GTK4/GSK SOFTWARE RENDERER (the real-HW paint-hang fix) ──────────────────
    # THE difference between this GTK4 host and the GTK3 cage floor: GTK4 draws via
    # GSK, whose DEFAULT renderer is GL (gl/ngl) — it spins up its OWN GL context on
    # the layer-shell surface, SEPARATE from WebKit's (the WEBKIT_DISABLE_* above
    # only governs WebKitGTK's compositor, NOT GTK4's own GSK renderer). On llvmpipe
    # that GL context resolves to software GL and PAINTS (why the CI nixosTest +
    # this host both work there); on a REAL GPU driver GSK's GL/EGL/GBM context
    # creation HANGS on the layer-shell surface, so the compositor cursor shows on a
    # black screen and the first frame never presents (the "pointer-only" boot).
    # The GTK3 cage floor is IMMUNE because GTK3 has no GSK — it paints via cairo
    # directly. Pin GSK to the cairo (software) renderer + disable GDK's GL so the
    # GTK4 host paints on ANY GPU, exactly the never-fail floor the cage/sway/hart-
    # comp paths hold. Gated on !preferHardwareGL like the WEBKIT_DISABLE_* belt, so
    # the hardware-GL opt-in still gets GSK's GL renderer. Software GL forced in the
    # session launcher (LIBGL_ALWAYS_SOFTWARE) is the belt for the hardware-GL case;
    # GSK_RENDERER=cairo here is the suspenders that NEVER touches a GL context.
    ${lib.optionalString (!ui.preferHardwareGL) "export GSK_RENDERER=cairo\nexport GDK_GL=disable"}
    export HART_SHELL_URL="$URL"
    # Shell-paint readiness marker (the session-supervisor's HUNG-tier guard): the
    # GTK4 host touches this once the WebView finishes its first load, telling the
    # paint-watchdog this Tier-2 surface is HEALTHY so it is NOT dropped as a hang.
    # THIS is the host the "pointer-only" regression hung in — without the marker
    # the watchdog would time out and escalate to cage; with it a working tier
    # stays up. The supervisor passes HART_SHELL_READY_FLAG; default to the pinned
    # /run/hart contract path so a bare (supervisor-less) launch is harmless.
    export HART_SHELL_READY_FLAG="''${HART_SHELL_READY_FLAG:-/run/hart/session/shell-ready}"
    exec ${cfg.package.python}/bin/python -c "
import gi, os
gi.require_version('Gtk', '4.0')
# WebKitGTK 6.0 is the GTK4 binding; the namespace is 'WebKit' (NOT 'WebKit2',
# which is the GTK3 4.1 binding the cage floor uses). This is the toolkit port.
gi.require_version('WebKit', '6.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, WebKit, Gtk4LayerShell as LayerShell

SHELL_URL = os.environ.get('HART_SHELL_URL', 'http://localhost:${liquidPort}')
READY_FLAG = os.environ.get('HART_SHELL_READY_FLAG', '/run/hart/session/shell-ready')


def _signal_painted():
    # Touch the first-paint marker the session-supervisor watches. Best-effort:
    # a missing dir / permission error must NEVER crash the shell (the supervisor
    # degrades safely: a missing marker escalates DOWN to the cage floor).
    try:
        os.makedirs(os.path.dirname(READY_FLAG), exist_ok=True)
        with open(READY_FLAG, 'w'):
            pass
    except OSError:
        pass


class GlassShellLayer:
    # The glass shell as a GTK4 wlr-layer-shell BACKGROUND surface.
    #
    # Z-ORDER MODEL (1) - picked in code: ONE layer-shell surface (one WebView),
    # overlays/orb co-planar inside it, anchored to all 4 edges as the BACKGROUND
    # layer with exclusive zone 0. Native toplevels (Phase 5) sit ABOVE this; the
    # orb does NOT float over a focused native window (the honest Model-1 limit).
    # The served shell HTML/JS is UNCHANGED - that is the whole reason for Model 1.

    def __init__(self, app):
        self._win = Gtk.ApplicationWindow(application=app)

        # ── Make this a wlr-layer-shell surface BEFORE the window is realized ──
        # init_for_window must run before present(); it converts the toplevel
        # into a zwlr_layer_surface_v1 the compositor stacks by layer.
        LayerShell.init_for_window(self._win)
        # BACKGROUND layer == the desktop wallpaper plane: below every native
        # toplevel. This is what makes the shell the DESKTOP rather than an app.
        LayerShell.set_layer(self._win, LayerShell.Layer.BACKGROUND)
        # Anchor to all four edges => the surface spans the whole output (the
        # desktop fills the screen) without us hardcoding a pixel size.
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                     LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self._win, edge, True)
        # Exclusive zone 0: the desktop does NOT reserve space away from other
        # surfaces (it is the backdrop, not a panel/bar). Per the Phase-4 spec.
        LayerShell.set_exclusive_zone(self._win, 0)
        # The background desktop should not steal keyboard focus from native
        # windows on top of it; ON_DEMAND lets the shell take focus only when the
        # user actually interacts with it (clicks the orb / a panel).
        # NOTE: ON_DEMAND on a BACKGROUND layer means the surface is NOT given
        # keyboard focus automatically by the compositor — typing only works once
        # the surface holds focus. So ON_DEMAND MUST be paired with an explicit
        # self._webview.grab_focus() after present() (below); without that grab,
        # the layer-shell surface never accepts keystrokes and the caret/typing
        # are dead even though the WebView renders.
        LayerShell.set_keyboard_mode(
            self._win, LayerShell.KeyboardMode.ON_DEMAND)

        # ── The WebView: same served shell, GTK4/WebKit-6.0 host ──
        webview = WebKit.WebView()
        # Signal first paint to the session-supervisor when the page finishes
        # loading — the GTK4/WebKit-6.0 load-changed signal mirrors the GTK3 cage
        # floor's. This marks Tier-2 HEALTHY so the paint-watchdog does NOT drop it.
        webview.connect('load-changed', self._on_load_changed)
        webview.load_uri(SHELL_URL)
        s = webview.get_settings()
        s.set_enable_javascript(True)
        s.set_enable_developer_extras(True)
        # NEVER (not ON_DEMAND): a fresh ISO / live-USB / VM often has only
        # software GL (llvmpipe). Forcing GPU accel there crashes WebKitGTK and
        # takes the shell session down. Correctness/robustness over a few fps —
        # the EXACT lesson the cage GTK3 floor encodes, re-applied on GTK4. The
        # WEBKIT_DISABLE_* env above is the belt; this is the suspenders.
        s.set_hardware_acceleration_policy(
            WebKit.HardwareAccelerationPolicy.${if ui.preferHardwareGL then "ON_DEMAND" else "NEVER"})
        self._webview = webview

        # GTK4: set_child (the GTK3 container .add() is gone); key events via an
        # EventControllerKey emitting 'key-pressed' (the GTK3 window key-press
        # SIGNAL is gone in GTK4, so we do NOT connect it on the window).
        self._win.set_child(webview)
        keyctl = Gtk.EventControllerKey.new()
        keyctl.connect('key-pressed', self._on_key)
        self._win.add_controller(keyctl)
        # GTK4: present() (no .show_all()); layer-shell sizes it to the anchors.
        self._win.present()
        # Explicitly grab keyboard focus into the WebView after present(). With
        # KeyboardMode.ON_DEMAND on a background layer-shell surface the
        # compositor does NOT auto-focus us, so without this grab left-clicks
        # land on a focus-less surface and typing/caret never work.
        self._webview.grab_focus()

    def _on_load_changed(self, _webview, event):
        # Touch the first-paint marker once the WebView finishes its first load.
        #
        # This is the GTK4/WebKit-6.0 mirror of the GTK3 cage floor's
        # _on_load_changed. WITHOUT it the connected load-changed handler does not
        # exist, _signal_painted() is NEVER called, /run/hart/session/shell-ready
        # never fires, and the session-supervisor's paint-watchdog times this Tier-2
        # surface out as HUNG and drops to the cage floor - the EXACT shell-ready-
        # never-fires half of the pointer-only regression. LoadEvent.FINISHED is the
        # WebKitGTK-6.0 enum (same name as the GTK3 WebKit2 binding). Re-grab focus on
        # first paint so typing works once the page JS has run (mirrors the cage
        # floor + the m2 WSL reference host).
        if event == WebKit.LoadEvent.FINISHED:
            _signal_painted()
            self._webview.grab_focus()

    def _on_key(self, _ctrl, keyval, _keycode, _state):
        from gi.repository import Gdk
        if keyval == Gdk.KEY_F12:
            self._webview.get_inspector().show()
            return True
        return False


def _on_activate(app):
    app.__hart_shell = GlassShellLayer(app)


# GTK4 uses GtkApplication.run as the loop — the GTK3 bare main loop is gone.
app = Gtk.Application(application_id='ai.hart.GlassShellLayer')
app.connect('activate', _on_activate)
app.run(None)
"
  '';

  # ── GTK4-layer-shell session launcher ──
  # Forces software rendering (paint on any GPU — SAME contract as the cage floor
  # + sway + hart-comp) and runs the GTK4 layer-shell host as the compositor's
  # single client. This is the L2 host-window session a higher tier (sway/hart-
  # comp) or the operator can select; it is NOT the default. Like the cage
  # launcher it wraps the renderer so wlroots/Mesa never touch a broken GPU GL
  # path. The GTK4 host needs a layer-shell-capable compositor (sway/hart-comp);
  # running it directly under cage is unsupported (cage implements no
  # zwlr_layer_shell_v1) — under cage the GTK3 floor is the renderer.
  sessionLauncher = pkgs.writeShellScriptBin "hart-glass-shell-gtk4-session" ''
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WLR_NO_HARDWARE_CURSORS=1
    ${lib.optionalString (!ui.preferHardwareGL) "export LIBGL_ALWAYS_SOFTWARE=1"}
    # sway hosts the layer-shell surface; the GTK4 host is sway's startup client.
    # Software-GL forced above so it paints on any GPU. (A bare hart-comp variant
    # selects the same host binary once its software-render path is VM-proven.)
    exec ${pkgs.sway}/bin/sway -c ${swayHostConfig}
  '';

  # sway single-output config whose ONLY client is the GTK4 layer-shell host. No
  # bars / launchers — sway exists to give the layer-shell surface a compositor
  # that implements zwlr_layer_shell_v1 (cage does not). Mirrors the sway-Tier-1
  # kiosk config shape; software-GL is forced in the launcher env above.
  swayHostConfig = pkgs.writeText "hart-gtk4-layer-host.conf" ''
    # HART OS GTK4 layer-shell host — single client (the GTK4 glass shell).
    # Generated; do not edit on the live ISO. See hart-layer-shell-host.nix.
    default_border none
    default_floating_border none
    gaps inner 0
    gaps outer 0
    # Launch the GTK4 layer-shell host as sway's startup client. It anchors itself
    # as the BACKGROUND layer (exclusive zone 0) via gtk4-layer-shell — so it is
    # the desktop, not a fullscreen app. Native toplevels (Phase 5) map above it.
    exec ${layerShellHost}/bin/hart-glass-shell-gtk4
  '';

  sessionDesktop = pkgs.writeText "hart-glass-gtk4.desktop" ''
    [Desktop Entry]
    Name=HART OS (GTK4 layer-shell)
    Comment=AI-native glass shell as a wlr-layer-shell desktop surface (L2 host port)
    Exec=${sessionLauncher}/bin/hart-glass-shell-gtk4-session
    Type=Application
    DesktopNames=HART-OS-gtk4
  '';
  # passthru.providedSessions REQUIRED by services.displayManager.sessionPackages
  # (session id must match the wayland-sessions/*.desktop basename) — the SAME
  # lesson hart-liquid-ui.nix's kioskSession + hart-sway-tier1.nix's swaySession +
  # hart-comp.nix's compSession encode. Without it, nix flake-check fails with
  # "did not specify any session names".
  hostSession = pkgs.runCommand "hart-glass-gtk4-wayland-session"
    { passthru.providedSessions = [ "hart-glass-gtk4" ]; } ''
      install -Dm644 ${sessionDesktop} $out/share/wayland-sessions/hart-glass-gtk4.desktop
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.layerShellHost = {
    enable = lib.mkEnableOption ''
      HART OS Phase-4 GTK4 layer-shell glass-shell host: the budgeted GTK3 → GTK4
      WebKitGTK host-window port that re-hosts the SAME served shell as a real
      wlr-layer-shell BACKGROUND surface (exclusive zone 0, JS unchanged). Opt-in,
      default OFF; does NOT flip defaultSession (cage GTK3 stays the Tier-3 floor
      until this GTK4 path's broken-GPU paint proof passes in CI). Registers a
      greeter-selectable session + the L2 host-window the higher tiers adopt.
    '';
  };

  # ═══════════════════════════════════════════════════════════
  # Config
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf host.enable {
    # The GTK4 layer-shell host REUSES the canonical served shell (single
    # renderer of the HTML/JS, no parallel path). It is therefore only coherent
    # when hart.liquidUI is enabled with the webkit renderer (the SAME assertion
    # sway-Tier-1 + hart-comp make). Fail EVAL loudly otherwise rather than ship a
    # blank session or a second shell renderer.
    assertions = [
      {
        assertion = ui.enable && (ui.renderer == "webkit");
        message =
          "hart.layerShellHost.enable requires hart.liquidUI.enable = true with "
          + "renderer = \"webkit\" — the GTK4 layer-shell host re-hosts the SAME "
          + "served glass shell (:6800 render_desktop_shell + /shell/static); it is "
          + "a host-window port, not a second shell renderer. Enable LiquidUI or "
          + "disable layerShellHost.";
      }
    ];

    # The GTK4 host binary + its software-GL session launcher + sway (the layer-
    # shell-capable compositor that hosts it). gtk4-layer-shell + webkitgtk_6_0 +
    # gtk4 land in the closure via giTypelibPath above; sway pulls wlroots.
    environment.systemPackages = [
      layerShellHost
      sessionLauncher
      pkgs.gtk4-layer-shell   # the wlr-layer-shell binding for GTK4 (Gtk4LayerShell-1.0)
      pkgs.webkitgtk_6_0      # WebKitGTK 6.0 — the GTK4 WebView (WebKit-6.0)
      pkgs.gtk4
      pkgs.sway               # layer-shell-capable compositor host (cage is not)
    ];

    # Register the opt-in GTK4 layer-shell session. desktop.nix keeps the default
    # session on cage ("hart-shell"); this is ADDITIVE — a selectable session +
    # the L2 host-window rung. cage (GTK3) + sway + GNOME all stay selectable (the
    # full never-fail ladder). This module NEVER assigns defaultSession.
    services.displayManager.sessionPackages = [ hostSession ];

    # ── Integration with the Phase-1 tier-drop supervisor: BE Tier-2 ──
    # Repoint the supervisor's Tier-2 `swayCommand` at THIS module's
    # `hart-glass-shell-gtk4-session` (sway hosting the GTK4 + WebKitGTK-6.0 +
    # gtk4-layer-shell host) so Tier-2 is a TRUE layer-shell desktop running the
    # SAME glass host as Tier-1 hart-comp — not bare sway and not the GTK3 cage
    # fullscreen window. The cage GTK3 host stays Tier-3 underneath either way; a
    # GTK4-host crash OR hang (the shell-paint watchdog) drops to it (the Phase-4 +
    # paint-watchdog nixosTests prove the drop lands on cage and the shell paints).
    #
    # mkOverride 900 (stronger than mkDefault=1000) so this wins over BOTH the
    # supervisor's bare-sway option default AND hart-sway-tier1.nix's mkDefault
    # `hart-sway-session` — when the GTK4 layer-shell host is enabled it IS the
    # Tier-2 session. An explicit operator `hart.sessionSupervisor.swayCommand`
    # (priority < 900, e.g. mkForce) still wins. Gated on the supervisor being
    # enabled so this is a pure no-op when the ladder is off (the supervisor option
    # always exists — its module is imported unconditionally — but writing the
    # value only matters when greetd drives the boot).
    hart.sessionSupervisor.swayCommand = lib.mkIf cfg.sessionSupervisor.enable
      (lib.mkOverride 900 "${sessionLauncher}/bin/hart-glass-shell-gtk4-session");

    # Floor invariant the supervisor honors: a GTK4-host crash/hang can NEVER drop
    # below the cage GTK3 Tier-3 floor.
  };
}
