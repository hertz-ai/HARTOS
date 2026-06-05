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
  giTypelibPath = lib.makeSearchPath "lib/girepository-1.0" (with pkgs; [
    glib gobject-introspection gtk3 webkitgtk_4_1
    pango gdk-pixbuf atk harfbuzz libsoup_3 cairo
  ]);
  glassShell = pkgs.writeShellScriptBin "hart-glass-shell" ''
    set -euo pipefail
    URL="http://localhost:${toString ui.port}"
    for i in $(seq 1 30); do
      if ${pkgs.curl}/bin/curl -sf "$URL/health" >/dev/null 2>&1; then break; fi
      sleep 1
    done
    if ! ${pkgs.curl}/bin/curl -sf "$URL/health" >/dev/null 2>&1; then
      # LiquidUI down — fall back to the Nunba SPA so the shell is never blank.
      if ${pkgs.curl}/bin/curl -sf "http://localhost:${nunbaPort}/" >/dev/null 2>&1; then
        URL="http://localhost:${nunbaPort}"
      fi
    fi
    # GI typelibs for the GTK/WebKit2 python below (see giTypelibPath note).
    export GI_TYPELIB_PATH="${giTypelibPath}"
    # WebKitGTK robustness on fresh-ISO boots (VM / software GL / no GPU): the
    # DMABUF renderer + GL compositing crash on a GL-less display, which is
    # exactly the first-boot / live-USB case. Disable both so a shell that
    # cannot paint never takes down the whole session.
    ${lib.optionalString (!ui.preferHardwareGL) "export WEBKIT_DISABLE_DMABUF_RENDERER=1\nexport WEBKIT_DISABLE_COMPOSITING_MODE=1"}
    export HART_SHELL_URL="$URL"
    exec ${cfg.package.python}/bin/python -c "
import gi, os
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gtk, WebKit2

class GlassShell(Gtk.Window):
    def __init__(self):
        super().__init__(title='HART OS')
        self.set_default_size(1280, 800)
        webview = WebKit2.WebView()
        webview.load_uri(os.environ.get('HART_SHELL_URL', 'http://localhost:${toString ui.port}'))
        s = webview.get_settings()
        s.set_enable_javascript(True)
        s.set_enable_developer_extras(False)
        # NEVER (not ALWAYS): a fresh ISO / live-USB / VM often has only
        # software GL (llvmpipe). Forcing GPU accel there crashes WebKitGTK and
        # takes down the shell session. Correctness/robustness over a few fps.
        s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.${if ui.preferHardwareGL then "ON_DEMAND" else "NEVER"})
        self.add(webview)
        self.connect('destroy', Gtk.main_quit)
        self.show_all()
        self.fullscreen()

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
  kioskLauncher = pkgs.writeShellScriptBin "hart-shell-session" ''
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WLR_NO_HARDWARE_CURSORS=1
    ${lib.optionalString (!ui.preferHardwareGL) "export LIBGL_ALWAYS_SOFTWARE=1"}
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
    }

    # ─────────────────────────────────────────────────────────
    # LiquidUI Server — context engine + UI generation
    # ─────────────────────────────────────────────────────────
    {
      systemd.services.hart-liquid-ui = {
        description = "HART OS LiquidUI — AI-Generated Adaptive Interface";
        documentation = [ "https://github.com/hertz-ai/HARTOS" ];
        after = [ "hart.target" "hart-model-bus.service" ];
        wants = [ "hart-model-bus.service" ];
        wantedBy = [ "hart.target" ];

        environment = {
          HEVOLVE_DATA_DIR = cfg.dataDir;
          HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
          LIQUID_UI_PORT = toString ui.port;
          LIQUID_UI_RENDERER = ui.renderer;
          LIQUID_UI_THEME = ui.theme;
          LIQUID_UI_VOICE = if ui.voiceEnabled then "1" else "0";
          LIQUID_UI_HAPTIC = if ui.hapticEnabled then "1" else "0";
          LIQUID_UI_CONTEXT_MS = toString ui.contextRefreshMs;
          LIQUID_UI_A2UI = if ui.enableA2UI then "1" else "0";
          MODEL_BUS_HTTP_PORT = toString (config.hart.modelBus.ports.http or 6790);
          HARTOS_BACKEND_PORT = toString cfg.ports.backend;
          HART_THEME_DIR = "/run/current-system/sw/share/hart/conky-themes";
          HART_LIQUID_UI_PORT = toString ui.port;
          NUNBA_STATIC_DIR = lib.mkIf ui.embedNunba
            "${pkgs.callPackage ../packages/nunba.nix { inherit hartSrc; }}/lib/nunba/static";
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
            if curl -sf "http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}/v1/status" >/dev/null 2>&1; then
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
          WatchdogSec = 30;

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
          ProtectHome = true;
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
    # Voice I/O (when enabled)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf ui.voiceEnabled {

      # Audio tools for voice pipeline
      environment.systemPackages = with pkgs; [
        sox          # Audio manipulation (record, play, convert)
        alsa-utils   # arecord, aplay
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
            STT_AVAILABLE=$(curl -sf "$MODEL_BUS/v1/models" 2>/dev/null | \
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
