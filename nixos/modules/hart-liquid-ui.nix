{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS LiquidUI — AI Generates the Interface
# ═══════════════════════════════════════════════════════════════
#
# Traditional OS: developers build static UIs, users click buttons.
# HART OS: AI generates the interface in real-time based on context.
#
# When a model is available, the entire UI becomes adaptive:
#   - File browser groups files by semantic meaning, not alphabet
#   - Settings shows what you're likely looking for first
#   - Dashboard explains WHY the GPU is busy, not just the %
#   - Voice says "your marketing agent finished" instead of beeping
#
# When no model is available, it falls back gracefully:
#   LLM available → generative UI (best experience)
#   No LLM        → Nunba static UI (React SPA)
#   No GUI         → terminal dashboard (textual TUI)
#   Edge/headless  → Conky metrics only
#
# Multi-modal output:
#   Screen  → WebKit2 renderer (GTK), streaming components
#   Voice   → TTS via Model Bus → PipeWire → speaker
#   Terminal → Rich TUI (textual library)
#   Haptic  → Vibration patterns (phone, via Android bridge)
#
# Architecture:
#
#   ┌─────────────────────────────────────────────────────────┐
#   │                   User Interaction                       │
#   │  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────────┐ │
#   │  │Screen│  │Voice │  │Touch │  │Haptic│  │Terminal  │ │
#   │  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘  └────┬─────┘ │
#   │  ┌──┴─────────┴────────┴─────────┴─────────────┴─────┐ │
#   │  │              LiquidUI Engine                        │ │
#   │  │  ┌──────────┐  ┌───────────┐  ┌────────────────┐  │ │
#   │  │  │ Context  │→ │ LLM Gen  │→ │ Renderer       │  │ │
#   │  │  │ Engine   │  │ (via Bus) │  │ (WebKit/TUI)   │  │ │
#   │  │  └──────────┘  └───────────┘  └────────────────┘  │ │
#   │  │                                                     │ │
#   │  │  ┌──────────┐  ┌───────────┐  ┌────────────────┐  │ │
#   │  │  │ Agent    │  │ World     │  │ Fallback:      │  │ │
#   │  │  │ A2UI     │  │ Model     │  │ Nunba/Conky    │  │ │
#   │  │  └──────────┘  └───────────┘  └────────────────┘  │ │
#   │  └─────────────────────────────────────────────────────┘ │
#   │              ↕ Model Bus ↕                               │
#   │  ┌─────────────────────────────────────────────────────┐ │
#   │  │  LLM │ Vision │ TTS │ STT │ Mesh Peers             │ │
#   │  └─────────────────────────────────────────────────────┘ │
#   └─────────────────────────────────────────────────────────┘

let
  cfg = config.hart;
  ui = config.hart.liquidUI;

  # ── The glass shell renderer (single source) ──
  # Fullscreen WebKit2 window onto the LiquidUI server.  If LiquidUI (:port) is
  # not up yet it waits, then falls back to the Nunba SPA (:nunba.port) so the
  # screen is NEVER the bare GNOME desktop or a blank page.  Used by BOTH the
  # kiosk session (cage, below) and the in-GNOME app launcher — one renderer,
  # no duplicate copies.
  nunbaPort = toString (config.hart.nunba.port or 5000);
  # GObject-introspection typelibs the glass shell's gi.require_version needs.
  # pygobject3 (the `gi` module) is in cfg.package.python, but the Gtk-3.0 /
  # WebKit2-4.1 *typelibs* live in these packages and must be on GI_TYPELIB_PATH
  # — the cage kiosk session sets no such path, so without this every
  # gi.require_version() raises and the shell window dies on launch.
  # makeSearchPathOutput "out": the GObject / GLib / Gtk typelibs live in each
  # package's `out` output, but several of these (glib, gtk3, ...) have a
  # non-`out` DEFAULT output (bin/dev), so plain makeSearchPath pointed at the
  # wrong store path and `gi` failed with "Typelib file for namespace 'GObject',
  # version '2.0' not found" — the glass-shell SIGABRT (status=6) on the ISO.
  giTypelibPath = lib.makeSearchPathOutput "out" "lib/girepository-1.0" (with pkgs; [
    glib gobject-introspection gtk3 webkitgtk_4_1
    pango gdk-pixbuf atk harfbuzz libsoup_3 cairo
  ]);
  # GStreamer capture plugins for the cage floor's mic/getUserMedia path. WebKit2
  # (like the GTK4 host) routes MediaStream capture through GStreamer, and a bare
  # cage session sets NO GST_PLUGIN_SYSTEM_PATH_1_0 -> the `valve`/`pulsesrc`
  # elements are invisible -> a mic click SIGSEGVs WebKitWebProcess (the real-HW
  # 2026-07-18 "clicking the mic hung the entire cage" incident). This back-ports
  # the GTK4 host's fix (hart-layer-shell-host.nix:99-120) to the never-fail floor
  # so the floor is safe on the same path. makeSearchPathOutput "out", NOT plain
  # makeSearchPath: gstreamer core's DEFAULT output is `bin`, so plain makeSearchPath
  # resolves an empty plugin dir and the elements stay invisible.
  # The GStreamer packages live under pkgs.gst_all_1 (NOT top-level pkgs -- the
  # bare `gstreamer` attr does not exist, which broke iso-desktop with "undefined
  # variable 'gstreamer'"). Mirror the GTK4 host exactly, incl. pipewiresrc for the
  # PipeWire desktop.
  gstCapturePlugins = (with pkgs.gst_all_1; [
    gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad
  ]) ++ [ pkgs.pipewire ];
  gstPluginPath = lib.makeSearchPathOutput "out" "lib/gstreamer-1.0" gstCapturePlugins;
  glassShell = pkgs.writeShellScriptBin "hart-glass-shell" ''
    set -euo pipefail
    URL="http://localhost:${toString ui.port}"
    # --max-time IS LOAD-BEARING (task #8, item 2.2). `curl -sf` has NO total
    # timeout: a backend that ACCEPTS the TCP connection and then never answers
    # — the half-up case, which is exactly what a still-initialising Flask app
    # looks like — blocks this forever, and with it the whole boot wait. The
    # retry loop below never gets a second iteration, so "wait up to 30s" was
    # really "wait forever" on the one failure it exists to survive.
    #
    # 5s is generous for a LOCAL /health. If the loop exhausts, the fall-back
    # below picks the Nunba SPA rather than hanging — and a premature fallback
    # to a working surface beats a shell that never appears, which is the whole
    # never-blank principle this script is built on.
    for i in $(seq 1 30); do
      if ${pkgs.curl}/bin/curl -sf --connect-timeout 2 --max-time 5 "$URL/health" >/dev/null 2>&1; then break; fi
      sleep 1
    done
    if ! ${pkgs.curl}/bin/curl -sf --connect-timeout 2 --max-time 5 "$URL/health" >/dev/null 2>&1; then
      # LiquidUI down — fall back to the Nunba SPA so the shell is never blank.
      if ${pkgs.curl}/bin/curl -sf --connect-timeout 2 --max-time 5 "http://localhost:${nunbaPort}/" >/dev/null 2>&1; then
        URL="http://localhost:${nunbaPort}"
      fi
    fi
    # GI typelibs for the GTK/WebKit2 python below (see giTypelibPath note).
    export GI_TYPELIB_PATH="${giTypelibPath}"
    # GStreamer capture plugins so the mic/getUserMedia path finds pulsesrc/valve
    # instead of SIGSEGV-ing WebKitWebProcess (see gstPluginPath note; back-port of
    # the GTK4 host fix to the never-fail floor).
    export GST_PLUGIN_SYSTEM_PATH_1_0="${gstPluginPath}"
    # WebKitGTK robustness on fresh-ISO boots (VM / software GL / no GPU): the
    # DMABUF renderer + GL compositing crash on a GL-less display, which is
    # exactly the first-boot / live-USB case. Disable both so a shell that
    # cannot paint never takes down the whole session.
    ${lib.optionalString (!ui.preferHardwareGL) "export WEBKIT_DISABLE_DMABUF_RENDERER=1\nexport WEBKIT_DISABLE_COMPOSITING_MODE=1"}
    ${lib.optionalString ui.runOnboardingInKiosk ''
    # First-run identity ceremony (opt-in; default off). `hart-onboarding`
    # self-gates via --check (exits 0 if already onboarded). timeout + || true
    # bound any hang/crash so the shell ALWAYS comes up — the ceremony can never
    # permanently block the kiosk. Validate on a real ISO boot before enabling.
    if command -v hart-onboarding >/dev/null 2>&1; then
      ${pkgs.coreutils}/bin/timeout 300 hart-onboarding || true
    fi
''}
    export HART_SHELL_URL="$URL"
    # ── Publish the SOFTWARE render rung (the auto-fallback ladder floor) ──────────
    # cage Tier-3 is the GTK3 software floor: no GSK, WebKit compositing off. If a
    # higher GPU rung (vulkan Tier-1 / webkit-cairo Tier-2) wrote /run/hart/session/shell-render
    # and then the paint-watchdog dropped to cage, the file would still name the GPU
    # rung -> the backend would emit body.gpu-hardware while cage cannot animate/blur,
    # re-arming the ~500ms-lag class. Reassert `software` here so the backend renders
    # the calm opaque floor (gpu-software + webkit-flat) that matches this host.
    mkdir -p /run/hart/session 2>/dev/null || true
    printf '%s' software > /run/hart/session/shell-render 2>/dev/null || true
    # Shell-paint readiness marker (the session-supervisor's HUNG-tier guard
    # consumes it): the host touches this once the WebView finishes loading its
    # first frame. The supervisor passes HART_SHELL_READY_FLAG; default to the
    # pinned /run/hart contract path so a bare launch (no supervisor) is harmless.
    export HART_SHELL_READY_FLAG="''${HART_SHELL_READY_FLAG:-/run/hart/session/shell-ready}"
    exec ${cfg.package.python}/bin/python -c "
import gi, os
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gtk, WebKit2

READY_FLAG = os.environ.get('HART_SHELL_READY_FLAG', '/run/hart/session/shell-ready')


def _signal_painted():
    # Touch the first-paint marker the session-supervisor watches. Best-effort:
    # a missing /run/hart dir or a permission error must NEVER crash the shell -
    # the supervisor degrades safely (it escalates DOWN on a missing marker).
    try:
        os.makedirs(os.path.dirname(READY_FLAG), exist_ok=True)
        with open(READY_FLAG, 'w'):
            pass
    except OSError:
        pass


class GlassShell(Gtk.Window):
    def __init__(self):
        super().__init__(title='HART OS')
        self.set_default_size(1280, 800)
        webview = WebKit2.WebView()
        # Signal first paint to the supervisor when the page finishes loading
        # (the WebView is realized + content presented). This is what tells the
        # paint-watchdog this tier is HEALTHY so it is NOT dropped as a hang.
        webview.connect('load-changed', self._on_load_changed)
        # getUserMedia (the voice orb / mic) raises a WebKit permission-request;
        # with NO handler the promise hangs FOREVER, so clicking the orb froze
        # the shell with no error (real-HW 2026-07-18). This is the trusted
        # local kiosk shell (its own served origin, no third-party content), and
        # tapping the orb IS the user's intent to talk, so auto-grant the media
        # (mic/camera) request; any other permission class is denied by default.
        webview.connect('permission-request', self._on_permission_request)
        # If the web process dies (mic-capture SIGSEGV, OOM, codec, GPU), the surface
        # stays mapped but renders nothing and shell-ready has already passed -- so
        # crash the HOST: the session-supervisor counts it and relaunches the tier
        # with a fresh web process, and a repeat-crash walks the ladder. Without this
        # the cage floor froze blank forever (back-port of hart-layer-shell-host.nix).
        webview.connect('web-process-terminated', self._on_web_process_terminated)
        webview.load_uri(os.environ.get('HART_SHELL_URL', 'http://localhost:${toString ui.port}'))
        s = webview.get_settings()
        s.set_enable_javascript(True)
        s.set_enable_developer_extras(True)
        # Enable the MediaStream API so navigator.mediaDevices.getUserMedia even
        # EXISTS in the kiosk WebView (off by default in WebKitGTK) -- the voice
        # orb needs it to open the mic.
        try:
            s.set_enable_media_stream(True)
        except Exception as _e:
            print('hart-liquid-ui: enable_media_stream unavailable: %s' % _e, flush=True)
        # NEVER (not ALWAYS): a fresh ISO / live-USB / VM often has only
        # software GL (llvmpipe). Forcing GPU accel there crashes WebKitGTK and
        # takes down the shell session. Correctness/robustness over a few fps.
        s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.${if ui.preferHardwareGL then "ON_DEMAND" else "NEVER"})
        self._webview = webview
        self.add(webview)
        self.connect('destroy', Gtk.main_quit)
        self.connect('key-press-event', self._on_key)
        self.show_all()
        self.fullscreen()
        # The cage WebView must explicitly grab keyboard focus after the window
        # is shown/fullscreened — without this the WebView never receives focus,
        # so left-clicks land on a focus-less surface and typing/caret never work
        # (dead UI on the live USB). Focus the actual web content, not the window.
        webview.grab_focus()

    def _on_load_changed(self, _webview, event):
        if event == WebKit2.LoadEvent.FINISHED:
            _signal_painted()

    def _on_web_process_terminated(self, _webview, reason):
        # The web process died (mic-capture SIGSEGV / OOM / codec / GPU). The surface
        # stays mapped but renders NOTHING and shell-ready already passed, so the only
        # honest move is to crash the HOST: the supervisor counts it and relaunches
        # the tier with a fresh web process; a repeat-crash loop walks the ladder.
        import sys as _sys, os as _os
        print('[hart-glass-shell] WebKitWebProcess TERMINATED (%s) -- exiting so the '
              'supervisor relaunches the tier' % reason, file=_sys.stderr)
        _sys.stderr.flush()
        _os._exit(1)

    def _on_permission_request(self, _webview, request):
        # Auto-grant mic/camera for the trusted local shell so getUserMedia does
        # not hang (the orb-freeze bug); deny everything else. Return True to say
        # the request was handled. Never raise -- an unhandled exception here
        # would drop the signal back to WebKit's default (hang) and could crash
        # the shell.
        try:
            if isinstance(request, WebKit2.UserMediaPermissionRequest):
                request.allow()
                return True
            request.deny()
            return True
        except Exception as _e:
            print('hart-liquid-ui: permission-request handling failed: %s' % _e, flush=True)
            try:
                request.deny()
            except Exception:
                pass
            return True

    def _on_key(self, widget, event):
        from gi.repository import Gdk
        if event.keyval == Gdk.KEY_F12:
            self._webview.get_inspector().show()
            return True
        return False

GlassShell()
Gtk.main()
"
  '';

  # ── Kiosk session launcher ──
  # Wraps cage so the Wayland stack boots the glass shell on ANY GPU — including
  # broken / flaky drivers. A real NVIDIA box showed nouveau GSP init failing
  # (`gsp: fini failed, -110`) and cage crashing at startup ("failed to idle
  # channel"). wlroots ABORTS rather than use software rendering unless
  # WLR_RENDERER_ALLOW_SOFTWARE=1, so the compositor died and the whole session
  # with it. Force the entire kiosk to software rendering (Mesa llvmpipe +
  # wlroots pixman): the shell is 2D and renders fine in software, KMS scanout
  # still uses the kernel driver (the console text proves KMS works), and the
  # broken GPU GL/compute path is never touched. A kiosk MUST paint on any GPU.
  #
  # WLR_RENDERER=pixman IS THE MISSING HALF OF THAT SENTENCE. The comment above
  # has always said "Mesa llvmpipe + wlroots pixman", but the script only set
  # WLR_RENDERER_ALLOW_SOFTWARE, which PERMITS a software renderer without
  # SELECTING one — so wlroots still took its default GLES2 path and opened EGL
  # against the DRM node. With LIBGL_ALWAYS_SOFTWARE also set, Mesa then refuses
  # the combination outright, and the floor died (run 30848154453):
  #
  #   libEGL warning: Not allowed to force software rendering when API
  #                   explicitly selects a hardware device
  #   [ERROR] [render/egl.c:268]  Failed to initialize EGL
  #   [ERROR] [../cage.c:330]     Unable to create the wlroots renderer
  #   hart-session-supervisor: tier 'cage' exited rc=1 after 1s
  #   hart-session-supervisor: crash-loop on the floor ('cage') — cannot drop further
  #
  # "greeter exited without creating a session" then appeared 66x in
  # layer-shell-host-paint, 22x in desktop-shell-boot and 13x in the reboot
  # latch: ONE cause behind several failures, and the floor is exactly the tier
  # that must never have one.
  #
  # pixman is wlroots' CPU renderer and does not go through EGL at all, so the
  # refusal cannot arise. Gated on the SAME !preferHardwareGL condition as
  # LIBGL_ALWAYS_SOFTWARE: a box that wants hardware GL keeps the GLES2 path
  # untouched, and only the already-software path changes — where EGL was
  # failing anyway, so this cannot be worse.
  kioskLauncher = pkgs.writeShellScriptBin "hart-shell-session" ''
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WLR_NO_HARDWARE_CURSORS=1
    ${lib.optionalString (!ui.preferHardwareGL)
      "export LIBGL_ALWAYS_SOFTWARE=1\n    export WLR_RENDERER=pixman"}
    exec ${pkgs.cage}/bin/cage -- ${glassShell}/bin/hart-glass-shell
  '';

  # ── Kiosk Wayland session ──
  # cage runs ONLY the glass shell as the compositor's single client.  There is
  # no desktop, no app-grid, no GNOME beneath it — THIS is what makes
  # Nunba/LiquidUI the OS *shell* instead of an app layered on GNOME.  Registered
  # via services.displayManager.sessionPackages; desktop.nix sets it default and
  # keeps GNOME as a selectable fallback session.
  # The .desktop content (writeText keeps it heredoc-free + readable).
  sessionDesktop = pkgs.writeText "hart-shell.desktop" ''
    [Desktop Entry]
    Name=HART OS
    Comment=AI-native glass shell (Nunba / LiquidUI)
    Exec=${kioskLauncher}/bin/hart-shell-session
    Type=Application
    DesktopNames=HART-OS
  '';
  # services.displayManager.sessionPackages REQUIRES passthru.providedSessions
  # (the session id, matching the wayland-sessions/*.desktop basename) — without
  # it nixos flake-check fails with "did not specify any session names".  A
  # runCommand carries the passthru; writeTextFile cannot.
  kioskSession = pkgs.runCommand "hart-shell-wayland-session"
    { passthru.providedSessions = [ "hart-shell" ]; } ''
      install -Dm644 ${sessionDesktop} $out/share/wayland-sessions/hart-shell.desktop
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.liquidUI = {

    enable = lib.mkEnableOption "HART OS LiquidUI (AI-generated adaptive interface)";

    port = lib.mkOption {
      type = lib.types.port;
      default = 6800;
      description = "LiquidUI WebSocket server port";
    };

    renderer = lib.mkOption {
      type = lib.types.enum [ "webkit" "electron" "terminal" ];
      default = if (cfg.variant == "server" || cfg.variant == "edge")
                then "terminal"
                else "webkit";
      defaultText = lib.literalExpression ''
        if variant is server/edge then "terminal" else "webkit"
      '';
      description = ''
        UI renderer backend:
        - webkit: GTK WebKit2 (lightweight, desktop/phone)
        - electron: Chromium-based (heavier, more web compat)
        - terminal: Rich TUI via textual (headless/SSH)
      '';
    };

    voiceEnabled = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable voice input (STT) and output (TTS) via Model Bus";
    };

    hapticEnabled = lib.mkOption {
      type = lib.types.bool;
      default = (cfg.variant == "phone");
      defaultText = lib.literalExpression "true if phone variant";
      description = "Enable haptic feedback (phone only, via Android subsystem)";
    };

    theme = lib.mkOption {
      type = lib.types.enum [ "auto" "dark" "light" "high-contrast" ];
      default = "auto";
      description = "UI theme (auto follows system dark/light preference)";
    };

    preferHardwareGL = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Let the kiosk glass shell use HARDWARE GL acceleration (smoother glass
        blur on llvmpipe-bound systems) instead of forcing Mesa software
        rendering. DEFAULT FALSE — the forced-software path is what makes the
        shell paint on ANY GPU including broken/flaky drivers (the nouveau-GSP
        crash class #99-103 hardened against); with this off the kiosk is
        byte-identical to before this option. Enable ONLY on hardware with a
        known-good GPU driver: on a flaky GPU it re-introduces the WebKitGTK/cage
        crash. This is the documented lever for the software-GL-vs-glass-perf
        trade-off — robustness stays the default.
      '';
    };

    gpuDiagnostic = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        GPU RENDER DIAGNOSTIC MODE — root-cause the layer-shell vulkan/GSK hang
        (task #12) instead of avoiding it. DEFAULT FALSE (a normal build is
        byte-identical + un-bloated). When true:
          * the Tier-1 hart-comp session FORCES the vulkan rung
            (HART_SHELL_RENDER=vulkan) instead of the safe webkit-cairo default, so
            the hang is actually ATTEMPTED (every boot since 2026-07-20 silently
            skipped it);
          * the GTK4 host exports Vulkan validation layers + GSK/GDK/WebKit debug
            and dumps `vulkaninfo --summary` to the journal, so a real-HW boot
            CAPTURES the exact VK_ERROR_SURFACE_LOST_KHR / swapchain-recreate
            failure (hover the orb to trigger it) instead of us guessing;
          * ships vulkan-tools + vulkan-validation-layers.
        SAFE: the paint-watchdog still self-heals — a vulkan hang drops to the cage
        floor, so a diagnostic boot can NEVER brick. Workflow: flash a build with
        this ON, boot, hover the orb, pull the HARTJRNL, read the VK trace. Turn
        OFF for normal builds.
      '';
    };

    contextRefreshMs = lib.mkOption {
      type = lib.types.int;
      default = 2000;
      description = "How often to refresh context signals (milliseconds)";
    };

    enableA2UI = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable Agent-to-UI protocol (agents push UI components)";
    };

    runOnboardingInKiosk = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Run the "Light Your HART" first-boot identity ceremony (hart-onboarding)
        inside the cage KIOSK session, before the glass shell. DEFAULT FALSE — the
        ceremony is GTK4/libadwaita and must paint under the kiosk's software-GL;
        if it cannot, it could delay the shell up to the 300s timeout on first
        boot. The glass-shell wrapper guards it with the ceremony's own `--check`
        (skip if already onboarded) + `timeout 300` + `|| true`, so it can NEVER
        permanently block the kiosk — but ENABLE ONLY after verifying the ceremony
        paints on a real ISO boot. Off = the shell starts directly (current,
        byte-identical behaviour). Requires hart.onboarding (the variant default).
      '';
    };

    embedNunba = lib.mkOption {
      type = lib.types.bool;
      default = (config.hart.nunba.enable or false);
      defaultText = lib.literalExpression "true when nunba is enabled";
      description = "Embed Nunba React SPA inside LiquidUI panels (glass shell)";
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && ui.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Dependencies + runtime directories
    # ─────────────────────────────────────────────────────────
    {
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/liquid-ui 0750 hart hart -"
        "d /var/lib/hart/liquid-ui/cache 0750 hart hart -"     # Component cache
        "d /var/lib/hart/liquid-ui/templates 0750 hart hart -" # UI templates
        "d /var/lib/hart/liquid-ui/context 0750 hart hart -"   # Context snapshots
        "d /run/hart/liquid-ui 0750 hart hart -"
      ];

      # WebKit2 + renderer deps
      environment.systemPackages = lib.mkIf (ui.renderer == "webkit") [
        pkgs.gtk3
        pkgs.webkitgtk_4_1
        pkgs.gobject-introspection
      ];

      # The Phase-1 tier-drop supervisor's cage FLOOR launcher. Like hart-comp.nix
      # (compCommand) and hart-layer-shell-host.nix (swayCommand), point the floor at
      # the ABSOLUTE store path of the cage session launcher: the supervisor option
      # default is the BARE "hart-shell-session", which is NOT on the greetd selector's
      # PATH, so the floor died "command not found" (rc=127) and crash-looped on the
      # floor — the never-fail guarantee itself broken (real-HW boot 2026-06-24).
      # mkOverride 900 mirrors swayCommand; gated on the supervisor so it's a no-op off.
      hart.sessionSupervisor.cageCommand = lib.mkIf config.hart.sessionSupervisor.enable
        (lib.mkOverride 900 "${kioskLauncher}/bin/hart-shell-session");
    }

    # ─────────────────────────────────────────────────────────
    # LiquidUI Server — context engine + UI generation
    # ─────────────────────────────────────────────────────────
    {
      systemd.services.hart-liquid-ui = {
        description = "HART OS LiquidUI — AI-Generated Adaptive Interface";
        documentation = [ "https://github.com/hertz-ai/HARTOS" ];
        # :PORT must come up FAST: the glass-shell host blocks on :PORT/health for up
        # to 30s before it paints, and the session-supervisor's shell-paint watchdog
        # drops a tier that hasn't painted — so a slow :PORT made Tier-1/2 get killed
        # mid-wait and fall to cage (real-HW boot 2026-06-24). The shell server does
        # NOT need the model bus to render: it degrades to a static UI without it (see
        # the Model-Bus probe in ExecStart). `wants` STILL pulls the model bus in (the
        # generative UI activates once it appears), but it is DROPPED from `after` so
        # :PORT no longer waits behind model loading — they start concurrently.
        after = [ ];  # NOT hart.target: wantedBy=hart.target + after=hart.target is a cycle (see hart-model-bus.nix 2026-08-14)
        wants = [ "hart-model-bus.service" ];
        wantedBy = [ "hart.target" ];

        # Setting `.path` makes THIS the unit's ENTIRE PATH — /run/current-system/
        # sw/bin is NOT on it. So every binary the shell server execs must be
        # listed explicitly (the same minimal-unit-PATH bug class as the ISO boot
        # services that were missing awk/lspci/xxd/curl).
        #   - curl: the Model-Bus availability probe.
        #   - networkmanager (nmcli): the glass shell's Wi-Fi UI. The connectivity
        #     top-bar (/api/shell/connectivity/summary) and the Wi-Fi panel
        #     (/api/shell/network/wifi*, /api/shell/wifi/*) all exec bare `nmcli`;
        #     a returncode==0 is what flips wifi.available=True. Without nmcli on
        #     PATH the exec raises FileNotFoundError (caught + ignored), leaving
        #     available=False — which the UI renders as "Wi-Fi not available" even
        #     though the radio + NetworkManager are up. nmcli talks to the NM
        #     daemon over the system D-Bus (AF_UNIX is allowed; no PrivateNetwork),
        #     so read-only status works here without extra group membership.
        # The shell's settings endpoints SHELL OUT to system tools, and a systemd
        # unit gets ONLY this PATH -- not the login PATH the code was written
        # against. Every tool missing here becomes a 500 on one settings page and,
        # in the UI, the generic "couldn't load yet, it will appear once the
        # connection is restored" card. Measured on real HW 2026-08-12, all four
        # in one 15-minute window, each logged as a *swallowed Exception*:
        #     shell_audio          -> FileNotFoundError: 'pactl'
        #     shell_display        -> FileNotFoundError: 'xrandr'
        #     shell_network_status -> FileNotFoundError: 'ip'
        #     shell_power          -> FileNotFoundError: 'upower'
        # so Audio, Display, Network and Power all read as "offline" on a machine
        # whose audio, display, network and battery were all working perfectly.
        # This is the SAME defect class as the Flatpak "not available" bug
        # (aff3a8c): the capability is installed, the service just cannot see it.
        #
        # Attr-guarded (the drm_info pattern) so a nixpkgs rev lacking any of them
        # cannot break evaluation -- the endpoint then degrades exactly as it does
        # today instead of failing the build.
        path =
          # Guarded with `pkgs ? rustdesk`: I could not evaluate that attribute
          # against the pinned nixpkgs from the built image (no channel on the
          # node), so this must not be able to break the build. If rustdesk is
          # absent from pkgs the guard is simply not added -- and nothing can be
          # storming, because there is no binary for the bridge to find.
          lib.optional (pkgs ? rustdesk) (
          # FIRST on PATH so shutil.which('rustdesk') in
          # integrations/remote_desktop/rustdesk_bridge.py resolves to this guard
          # rather than the real binary.
          #
          # RustDesk 1.3.1 has no X11 DISPLAY to discover on a Wayland-only
          # session, so its daemon modes retry session detection forever:
          #   sh -c 'pgrep -a Xwayland | ps -u 1000 -f
          #          | xargs cat /proc/*/environ | tr | sed | grep | awk | tail'
          # Measured 2026-08-12: 2,374 of 2,410 processes created in 12 seconds
          # (98.5% of ALL process creation on the box), 179 forks/sec, package
          # pinned at 92C with 543ms of thermal throttling per 10s, and
          # 5.9 hours of cumulative throttle. That was the second GUI hang, and
          # it was NOT the compositor: hart-comp sat idle in do_epoll_wait
          # throughout while every core was being force-idled by
          # intel_powerclamp.
          #
          # pkill was not a fix: restarting hart-liquid-ui respawns it, because
          # HART owns its lifecycle (rustdesk_bridge.py:251,257,268), not systemd.
          # So refuse ONLY the self-daemonising modes, keep every query command,
          # and log each refusal -- a blocked capability the operator cannot see
          # is just a different silent failure.
          (pkgs.writeShellScriptBin "rustdesk" ''
            for a in "$@"; do
              case "$a" in
                --service|--server)
                  echo "hart: refused 'rustdesk $a' - daemon mode is disabled on a" \
                       "Wayland-only session (it fork-storms at ~179/sec and holds" \
                       "the package at 92C). Query commands still work." >&2
                  ${pkgs.util-linux}/bin/logger -t hart-rustdesk-guard \
                    "refused rustdesk $a (Wayland fork-storm guard)" || true
                  exit 0
                  ;;
              esac
            done
            exec ${pkgs.rustdesk}/bin/rustdesk "$@"
          '')
          )
          ++ (with pkgs; [ curl coreutils networkmanager ])
          ++ lib.optional (pkgs ? pulseaudio)   pkgs.pulseaudio    # pactl   - audio
          ++ lib.optional (pkgs ? iproute2)     pkgs.iproute2      # ip      - network
          ++ lib.optional (pkgs ? upower)       pkgs.upower        # upower  - battery/power
          ++ lib.optional (pkgs ? wireplumber)  pkgs.wireplumber   # wpctl   - audio (sibling of pactl)
          ++ lib.optional (pkgs ? alsa-utils)   pkgs.alsa-utils    # amixer  - the layer BELOW PipeWire
          ++ lib.optional (pkgs ? xorg && pkgs.xorg ? xrandr) pkgs.xorg.xrandr;  # xrandr - display

        environment = {
          HEVOLVE_DATA_DIR = cfg.dataDir;
          HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
          LIQUID_UI_PORT = toString ui.port;
          LIQUID_UI_RENDERER = ui.renderer;
          LIQUID_UI_THEME = ui.theme;
          LIQUID_UI_VOICE = if ui.voiceEnabled then "1" else "0";
          LIQUID_UI_HAPTIC = if ui.hapticEnabled then "1" else "0";
          # #151 transparent-windows: tell the shell renderer whether WebKit
          # accelerated COMPOSITING is on. The glass-shell host enables it ONLY when
          # preferHardwareGL=true; otherwise it forces WEBKIT_DISABLE_COMPOSITING_MODE
          # + HardwareAccelerationPolicy.NEVER and backdrop-filter:blur paints NOTHING,
          # so a translucent .glass/.panel reads SEE-THROUGH. render_desktop_shell()
          # reads this to tag <body webkit-flat> and solidify the glass when blur will
          # not composite. SAME value the host derives preferHardwareGL from -> one
          # source of truth, no drift between the renderer and the host.
          LIQUID_UI_PREFER_HW_GL = if ui.preferHardwareGL then "1" else "0";
          LIQUID_UI_CONTEXT_MS = toString ui.contextRefreshMs;
          LIQUID_UI_A2UI = if ui.enableA2UI then "1" else "0";
          # Audio probe (wpctl-first, pactl fallback in _volume_get). pipewire-pulse
          # serves the pulse socket in the LOGIN user's runtime dir; this unit runs as
          # `hart` (uid 992), so it needs to be pointed at it explicitly AND allowed
          # to traverse in (see ProtectHome/BindPaths + the tmpfiles ACL below).
          XDG_RUNTIME_DIR = "/run/user/1000";
          PULSE_SERVER = "unix:/run/user/1000/pulse/native";
          # NEVER let pactl autospawn a PulseAudio daemon. PipeWire owns the
          # devices, so an autospawned pulseaudio dies instantly with "Daemon
          # startup without any loaded modules" -- and because the UI polls audio
          # on a timer that measured 1028 failed startups and contributed to a
          # ~200 fork/sec storm that held the package at 92C.
          PULSE_CLIENTCONFIG = pkgs.writeText "hart-pulse-client.conf" ''
            ; PipeWire owns audio on HART OS. Autospawn would fork a doomed daemon.
            autospawn = no
          '';
          MODEL_BUS_HTTP_PORT = toString (config.hart.modelBus.ports.http or 6790);
          HARTOS_BACKEND_PORT = toString cfg.ports.backend;
          HART_THEME_DIR = "/run/current-system/sw/share/hart/conky-themes";
          HART_LIQUID_UI_PORT = toString ui.port;
          NUNBA_STATIC_DIR = lib.mkIf ui.embedNunba
            "${pkgs.callPackage ../packages/nunba.nix { inherit hartSrc; }}/lib/nunba/static";
          # When the native Nunba daemon runs (hart.nunba.enable), LiquidUI reverse-
          # proxies the FULL Nunba (Python + React) over its unix socket instead of
          # only serving the static React dist — same origin, no host port. The
          # static dir above stays the graceful FLOOR (same /nix/store artifact the
          # daemon serves, so it can't drift). Unset when the daemon is off → the
          # existing static-only path is byte-for-byte unchanged (zero regression).
          HART_NUNBA_SOCKET = lib.mkIf config.hart.nunba.enable config.hart.nunba.socket;
          PYTHONDONTWRITEBYTECODE = "1";
          PYTHONUNBUFFERED = "1";
        };

        serviceConfig = {
          Type = "notify";
          User = "hart";
          Group = "hart";
          SupplementaryGroups = [ "video" "render" ];

          ExecStart = pkgs.writeShellScript "hart-liquid-ui" ''
            set -euo pipefail

            echo "[HART OS LiquidUI] Starting adaptive interface engine"
            echo "[HART OS LiquidUI] Port: ${toString ui.port}"
            echo "[HART OS LiquidUI] Renderer: ${ui.renderer}"
            echo "[HART OS LiquidUI] Theme: ${ui.theme}"
            echo "[HART OS LiquidUI] Voice: ${if ui.voiceEnabled then "enabled" else "disabled"}"
            echo "[HART OS LiquidUI] Haptic: ${if ui.hapticEnabled then "enabled" else "disabled"}"
            echo "[HART OS LiquidUI] A2UI: ${if ui.enableA2UI then "enabled" else "disabled"}"

            # Check if Model Bus is available (LiquidUI degrades gracefully without it)
            if curl -sf --connect-timeout 2 --max-time 5 "http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}/v1/status" >/dev/null 2>&1; then
              echo "[HART OS LiquidUI] Model Bus: connected — generative UI active"
            else
              echo "[HART OS LiquidUI] Model Bus: not available — falling back to static UI"
            fi

            # ── Start Python LiquidUI daemon ──
            exec ${cfg.package.python}/bin/python -c "
            import sys, os
            sys.path.insert(0, '${cfg.package}')
            os.environ['HEVOLVE_DATA_DIR'] = '${cfg.dataDir}'

            from integrations.agent_engine.liquid_ui_service import LiquidUIService

            ui = LiquidUIService(
                port=${toString ui.port},
                renderer='${ui.renderer}',
                theme='${ui.theme}',
                voice_enabled=${if ui.voiceEnabled then "True" else "False"},
                haptic_enabled=${if ui.hapticEnabled then "True" else "False"},
                context_refresh_ms=${toString ui.contextRefreshMs},
                a2ui_enabled=${if ui.enableA2UI then "True" else "False"},
                model_bus_port=${toString (config.hart.modelBus.ports.http or 6790)},
                backend_port=${toString cfg.ports.backend},
            )

            import systemd.daemon
            systemd.daemon.notify('READY=1')

            ui.serve_forever()
            "
          '';

          Restart = "on-failure";
          RestartSec = 5;
          # No WatchdogSec: LiquidUIService.serve_forever() sends READY=1 once but
          # never periodic sd_notify(WATCHDOG=1), so systemd SIGABRT-killed the
          # server every 30s (status=6/ABRT watchdog loop on the ISO). NOTE: this
          # is the headless LiquidUI SERVER; the GObject typelib crash was the
          # separate cage glass-shell client (fixed via giTypelibPath above).

          # Resource limits — scale by variant
          Slice = "hart-agents.slice";
          MemoryMax = if cfg.variant == "edge" then "128M"
                      else if cfg.variant == "desktop" then "512M"
                      else "1G";
          MemoryHigh = if cfg.variant == "edge" then "96M"
                       else if cfg.variant == "desktop" then "384M"
                       else "768M";
          CPUWeight = if cfg.variant == "edge" then 20 else 80;
          TasksMax = if cfg.variant == "edge" then 16 else 128;
          IOWeight = if cfg.variant == "edge" then 20 else 80;

          # Security hardening
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          # ProtectHome=true masks /run/user ENTIRELY inside this unit's mount
          # namespace. Measured 2026-08-12: `nsenter -t $MAINPID -m ls /run/user`
          # showed `d--------- root root`, so the Audio settings page returned
          # {"sinks":[],"sources":[]} forever while `pactl` worked perfectly by
          # hand. No ACL or PULSE_SERVER set from OUTSIDE can reach through a
          # namespace mask -- that is why three earlier attempts failed.
          #
          # "tmpfs" keeps /home and /root hidden (the hardening that actually
          # matters for a service running as `hart`) while BindPaths punches
          # through exactly ONE socket directory. Least privilege preserved:
          # the unit still cannot read the human's home.
          ProtectHome = "tmpfs";
          BindPaths = [ "/run/user/1000/pulse:/run/user/1000/pulse" ];
          ReadWritePaths = [
            cfg.dataDir
            cfg.logDir
            "/run/hart/liquid-ui"
            "/var/lib/hart/liquid-ui"
          ];
          PrivateTmp = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
          SystemCallFilter = [ "@system-service" "@network-io" ];

          StandardOutput = "journal";
          StandardError = "journal";
          SyslogIdentifier = "hart-liquid-ui";
        };
      };
    }

    # ─────────────────────────────────────────────────────────
    # LiquidUI Desktop Renderer (user service, graphical)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf (ui.renderer == "webkit") {

      # Register the kiosk session ("HART OS") so the display manager can run the
      # glass shell as the *session* itself.  desktop.nix sets it as the default
      # session and keeps GNOME as a selectable fallback — THIS is what makes
      # Nunba/LiquidUI the OS shell instead of an app layered on GNOME.
      services.displayManager.sessionPackages = [ kioskSession ];

      # Legacy in-GNOME renderer: repointed at the single ``glassShell`` script
      # (no duplicate renderer) and NOT auto-started — the kiosk session launches
      # the glass shell directly, so auto-layering it on the GNOME fallback would
      # double-launch.  Kept as a manually-startable unit for debugging.
      systemd.user.services.hart-liquid-ui-renderer = {
        description = "HART OS LiquidUI Renderer (WebKit2)";
        after = [ "graphical-session.target" ];
        partOf = [ "graphical-session.target" ];
        wantedBy = [ ];  # kiosk session is the canonical launch; do not auto-layer on GNOME

        serviceConfig = {
          ExecStart = "${glassShell}/bin/hart-glass-shell";

          Restart = "on-failure";
          RestartSec = 3;
        };

        environment = {
          GDK_BACKEND = if cfg.variant == "phone" then "wayland" else "x11,wayland";
          GTK_THEME = lib.mkIf (ui.theme == "dark") "Adwaita:dark";
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # OFFLINE VOICE FLOOR — UNCONDITIONAL (task #7, item 0.5)
    # ─────────────────────────────────────────────────────────
    # Deliberately NOT behind ui.voiceEnabled. That option is one flag over
    # two concerns — its own description says "voice input (STT) and output
    # (TTS)" — so turning off the wake-word LISTENER also removed the ability
    # to SPEAK. Disabling a microphone should never mute the OS.
    #
    # Found in a VM (run 30848154453): desktop-boot sets voiceEnabled=false to
    # trim the listener user service, a legitimate thing to want, and the
    # offline-voice-floor subtest then failed on `command -v espeak-ng`. The
    # same would happen to any real node that runs the desktop without voice
    # input — a kiosk, or a user who simply does not want a hot mic — and it
    # would land on exactly the moment the OS is supposed to introduce itself
    # by talking.
    #
    # espeak-ng is small, needs no model and no download, so there is no cost
    # to making it always present. It is not the voice we want; it is the
    # voice that always works, which is the whole point of a floor. The good
    # voice still arrives via the Model Bus when a model does.
    { environment.systemPackages = [ pkgs.espeak-ng ]; }

    # ─────────────────────────────────────────────────────────
    # Voice I/O (when enabled)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf ui.voiceEnabled {

      # Audio tools for voice pipeline
      environment.systemPackages = with pkgs; [
        sox          # Audio manipulation (record, play, convert)
        alsa-utils   # arecord, aplay

        # The offline voice FLOOR (espeak-ng) used to live here and has moved
        # UP to an unconditional block — see the comment above this mkIf.
        # It stays in this module (one writer for "what voice needs"); it just
        # no longer depends on voice INPUT being on. The other entry in the
        # tree is hart-accessibility.nix behind screenReader.enable, which
        # defaults false and drags in Orca auto-starting at login — a screen
        # reader, not a fallback synthesizer.
      ];

      # Voice input listener (background, activated by wake word or push-to-talk)
      systemd.user.services.hart-voice-listener = {
        description = "HART OS Voice Input Listener";
        after = [ "hart-liquid-ui-renderer.service" "pipewire.service" ];
        wantedBy = [ "graphical-session.target" ];

        serviceConfig = {
          ExecStart = pkgs.writeShellScript "hart-voice-listen" ''
            set -euo pipefail

            MODEL_BUS="http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}"
            LIQUID_UI="http://localhost:${toString ui.port}"

            echo "[HART OS Voice] Listener active"

            # Check if STT model is available via Model Bus
            STT_AVAILABLE=$(curl -sf --connect-timeout 2 --max-time 5 "$MODEL_BUS/v1/models" 2>/dev/null | \
              ${pkgs.jq}/bin/jq -r '.models[]? | select(.type == "stt") | .id' || echo "")

            if [[ -z "$STT_AVAILABLE" ]]; then
              echo "[HART OS Voice] No STT model available — voice input disabled"
              exec sleep infinity
            fi

            echo "[HART OS Voice] STT model: $STT_AVAILABLE"
            echo "[HART OS Voice] Waiting for voice commands..."

            # Continuous listen loop (push-to-talk via LiquidUI button)
            exec ${cfg.package.python}/bin/python -c "
            import sys, os, time
            sys.path.insert(0, '${cfg.package}')
            print('[HART OS Voice] Python voice listener ready')
            # Voice activation handled by LiquidUI frontend (WebSocket events)
            while True:
                time.sleep(3600)
            "
          '';

          Restart = "on-failure";
          RestartSec = 10;
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # Agent-to-UI Protocol (A2UI)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf ui.enableA2UI {

      # D-Bus interface for agents to push UI components
      services.dbus.packages = [
        (pkgs.writeTextDir "share/dbus-1/system.d/com.hart.LiquidUI.conf" ''
          <?xml version="1.0" encoding="UTF-8"?>
          <!DOCTYPE busconfig PUBLIC
           "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
           "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
          <busconfig>
            <!-- HART OS LiquidUI: Agent-to-UI protocol -->
            <policy user="hart">
              <allow own="com.hart.LiquidUI"/>
              <allow send_destination="com.hart.LiquidUI"/>
              <allow send_interface="com.hart.LiquidUI.Agent"/>
            </policy>

            <policy group="hart">
              <allow send_destination="com.hart.LiquidUI"/>
              <allow send_interface="com.hart.LiquidUI.Agent"/>
            </policy>

            <!-- Any process can push UI updates (agents run as hart user anyway) -->
            <policy context="default">
              <allow send_destination="com.hart.LiquidUI"
                     send_interface="com.hart.LiquidUI.Agent"
                     send_member="PushComponent"/>
              <allow send_destination="com.hart.LiquidUI"
                     send_interface="com.hart.LiquidUI.Agent"
                     send_member="RequestApproval"/>
              <allow send_destination="com.hart.LiquidUI"
                     send_interface="com.hart.LiquidUI.Agent"
                     send_member="ShowProgress"/>
              <allow send_destination="com.hart.LiquidUI"
                     send_interface="com.hart.LiquidUI.Agent"
                     send_member="ShowNotification"/>
            </policy>
          </busconfig>
        '')
      ];
    })

    # ─────────────────────────────────────────────────────────
    # .desktop file for application launcher
    # ─────────────────────────────────────────────────────────
    (lib.mkIf (ui.renderer == "webkit") {

      environment.etc."xdg/autostart/hart-liquid-ui.desktop".text = ''
        [Desktop Entry]
        Type=Application
        Name=HART OS Desktop Shell
        Comment=HART OS Glass Desktop Shell
        Exec=${glassShell}/bin/hart-glass-shell
        Icon=hart
        Categories=System;
        StartupNotify=true
        X-GNOME-Autostart-enabled=false
      '';
    })
  ]);
}
