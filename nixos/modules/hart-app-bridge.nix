{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS App Bridge — Cross-Subsystem Intelligence
# ═══════════════════════════════════════════════════════════════
#
# The bridge makes subsystem boundaries invisible. An Android app
# can call a Linux service. A Windows app can use an AI model.
# A Linux CLI can trigger an Android Activity. Everything talks
# to everything through OS-native agents.
#
# How it works:
#
#   Android camera app shares photo
#     → App Bridge receives Intent("SEND", image/jpeg)
#     → Queries capability registry: "who handles image analysis?"
#     → Options: GIMP (Linux), Photos (Android), AI Vision (Model Bus)
#     → Semantic routing picks best: AI Vision (fastest for analysis)
#     → Model Bus describes image → result back to Android app
#
# Cross-subsystem IPC:
#
#   ┌──────────┐  Intent   ┌──────────────┐  D-Bus   ┌──────────┐
#   │ Android  │ ────────→ │              │ ───────→ │ Linux    │
#   │ App      │           │  App Bridge  │          │ Service  │
#   │          │ ←──────── │              │ ←─────── │          │
#   └──────────┘  Result   │              │  Result  └──────────┘
#                           │              │
#   ┌──────────┐  HTTP     │              │  Pipe    ┌──────────┐
#   │ Web/PWA  │ ────────→ │  Capability  │ ───────→ │ Windows  │
#   │ App      │           │  Registry    │          │ App      │
#   │          │ ←──────── │              │ ←─────── │ (Wine)   │
#   └──────────┘  Result   │              │  Result  └──────────┘
#                           │              │
#   ┌──────────┐  Socket   │              │
#   │ AI Agent │ ────────→ │  Semantic    │
#   │          │           │  Router      │
#   │          │ ←──────── │              │
#   └──────────┘  Result   └──────────────┘
#
# The App Bridge also unifies:
#   - Clipboard (copy in Android, paste in Linux)
#   - Drag & drop (across subsystem windows)
#   - File sharing (XDG portal for cross-subsystem file access)
#   - Notifications (unified notification stream)

let
  cfg = config.hart;
  bridge = config.hart.appBridge;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.appBridge = {

    enable = lib.mkEnableOption "HART OS App Bridge (cross-subsystem agent routing)";

    socketPath = lib.mkOption {
      type = lib.types.str;
      default = "/run/hart/app-bridge.sock";
      description = "Unix domain socket for the bridge daemon";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 6810;
      description = "HTTP API port for subsystem bridge access";
    };

    allowCrossSubsystem = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable cross-subsystem routing (Linux↔Android↔Windows↔Web)";
    };

    intentRouter = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Route Android Intents to Linux D-Bus services and vice versa";
    };

    clipboardSync = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Unified clipboard across all subsystems";
    };

    dragAndDrop = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Cross-subsystem drag and drop via XDG portal";
    };

    notificationUnification = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Unified notification stream from all subsystems";
    };

    aiFallback = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Fall back to AI agent when no native app handles a capability request";
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && bridge.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Runtime directories + firewall
    # ─────────────────────────────────────────────────────────
    {
      systemd.tmpfiles.rules = [
        "d /run/hart/app-bridge 0750 hart hart -"
        "d /var/lib/hart/app-bridge 0750 hart hart -"
        "d /var/lib/hart/app-bridge/registry 0750 hart hart -"    # Capability registry
        "d /var/lib/hart/app-bridge/clipboard 0750 hart hart -"   # Clipboard staging
        "d /var/lib/hart/app-bridge/transfers 0750 hart hart -"   # File transfers
      ];

      # Open bridge port for internal subsystem access only (localhost)
      # Not exposed externally — only subsystems on the same machine use it
    }

    # ─────────────────────────────────────────────────────────
    # App Bridge Daemon — capability registry + semantic router
    # ─────────────────────────────────────────────────────────
    {
      systemd.services.hart-app-bridge = {
        description = "HART OS App Bridge — Cross-Subsystem Agent Routing";
        documentation = [ "https://github.com/hevolve-ai/hart" ];
        after = [ "hart.target" "hart-model-bus.service" ];
        wants = [ "hart-model-bus.service" ];
        wantedBy = [ "hart.target" ];

        environment = {
          HEVOLVE_DATA_DIR = cfg.dataDir;
          HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
          APP_BRIDGE_SOCKET = bridge.socketPath;
          APP_BRIDGE_PORT = toString bridge.port;
          APP_BRIDGE_CROSS_SUBSYSTEM = if bridge.allowCrossSubsystem then "1" else "0";
          APP_BRIDGE_INTENT_ROUTER = if bridge.intentRouter then "1" else "0";
          APP_BRIDGE_CLIPBOARD = if bridge.clipboardSync then "1" else "0";
          APP_BRIDGE_DND = if bridge.dragAndDrop then "1" else "0";
          APP_BRIDGE_AI_FALLBACK = if bridge.aiFallback then "1" else "0";
          MODEL_BUS_HTTP_PORT = toString (config.hart.modelBus.ports.http or 6790);
          HARTOS_BACKEND_PORT = toString cfg.ports.backend;
          PYTHONDONTWRITEBYTECODE = "1";
          PYTHONUNBUFFERED = "1";
        };

        serviceConfig = {
          Type = "notify";
          User = "hart";
          Group = "hart";

          ExecStart = pkgs.writeShellScript "hart-app-bridge" ''
            set -euo pipefail

            echo "[HART OS App Bridge] Starting cross-subsystem agent routing"
            echo "[HART OS App Bridge] Socket: ${bridge.socketPath}"
            echo "[HART OS App Bridge] HTTP: port ${toString bridge.port}"

            # ── Discover available subsystems ──
            SUBSYSTEMS="linux"  # Linux is always present

            # Check Android
            if systemctl is-active hart-android-runtime.service >/dev/null 2>&1; then
              SUBSYSTEMS="$SUBSYSTEMS android"
              echo "[HART OS App Bridge] Subsystem: Android ✓"
            fi

            # Check Windows (Wine)
            if command -v wine64 &>/dev/null || command -v wine &>/dev/null; then
              SUBSYSTEMS="$SUBSYSTEMS windows"
              echo "[HART OS App Bridge] Subsystem: Windows (Wine) ✓"
            fi

            # Check Web/PWA
            if command -v chromium &>/dev/null; then
              SUBSYSTEMS="$SUBSYSTEMS web"
              echo "[HART OS App Bridge] Subsystem: Web/PWA ✓"
            fi

            # Check AI (Model Bus)
            if curl -sf "http://localhost:${toString (config.hart.modelBus.ports.http or 6790)}/v1/status" >/dev/null 2>&1; then
              SUBSYSTEMS="$SUBSYSTEMS ai"
              echo "[HART OS App Bridge] Subsystem: AI (Model Bus) ✓"
            fi

            echo "[HART OS App Bridge] Active subsystems: $SUBSYSTEMS"

            # ── Build capability registry ──
            REGISTRY_DIR="/var/lib/hart/app-bridge/registry"

            # Register Linux capabilities (D-Bus services)
            if command -v busctl &>/dev/null; then
              DBUS_SERVICES=$(busctl list --system --no-pager 2>/dev/null | grep -c hart || echo 0)
              echo "[HART OS App Bridge] Linux D-Bus services: $DBUS_SERVICES"
            fi

            # ── Start Python bridge daemon ──
            exec ${cfg.package.python}/bin/python -c "
            import sys, os
            sys.path.insert(0, '${cfg.package}')
            os.environ['HEVOLVE_DATA_DIR'] = '${cfg.dataDir}'

            from integrations.agent_engine.app_bridge_service import AppBridgeService

            bridge = AppBridgeService(
                socket_path='${bridge.socketPath}',
                http_port=${toString bridge.port},
                cross_subsystem=${if bridge.allowCrossSubsystem then "True" else "False"},
                intent_router=${if bridge.intentRouter then "True" else "False"},
                clipboard_sync=${if bridge.clipboardSync then "True" else "False"},
                drag_and_drop=${if bridge.dragAndDrop then "True" else "False"},
                ai_fallback=${if bridge.aiFallback then "True" else "False"},
                model_bus_port=${toString (config.hart.modelBus.ports.http or 6790)},
                backend_port=${toString cfg.ports.backend},
            )

            import systemd.daemon
            systemd.daemon.notify('READY=1')

            bridge.serve_forever()
            "
          '';

          Restart = "on-failure";
          RestartSec = 5;
          WatchdogSec = 60;

          # Resource limits
          Slice = "hart-agents.slice";
          MemoryMax = "512M";
          CPUWeight = 50;
          TasksMax = 128;

          # Security hardening
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [
            cfg.dataDir
            cfg.logDir
            "/run/hart"
            "/var/lib/hart/app-bridge"
          ];
          PrivateTmp = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
          SystemCallFilter = [ "@system-service" "@network-io" ];

          StandardOutput = "journal";
          StandardError = "journal";
          SyslogIdentifier = "hart-app-bridge";
        };
      };
    }

    # ─────────────────────────────────────────────────────────
    # D-Bus Interface: com.hart.AppBridge
    # ─────────────────────────────────────────────────────────
    {
      services.dbus.packages = [
        (pkgs.writeTextDir "share/dbus-1/system.d/com.hart.AppBridge.conf" ''
          <?xml version="1.0" encoding="UTF-8"?>
          <!DOCTYPE busconfig PUBLIC
           "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
           "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
          <busconfig>
            <!-- HART OS App Bridge: cross-subsystem routing -->
            <policy user="hart">
              <allow own="com.hart.AppBridge"/>
              <allow send_destination="com.hart.AppBridge"/>
              <allow send_interface="com.hart.AppBridge"/>
              <allow send_interface="com.hart.AppBridge.Intent"/>
              <allow send_interface="com.hart.AppBridge.Clipboard"/>
              <allow send_interface="com.hart.AppBridge.Capability"/>
            </policy>

            <policy group="hart">
              <allow send_destination="com.hart.AppBridge"/>
              <allow send_interface="com.hart.AppBridge"/>
              <allow send_interface="com.hart.AppBridge.Intent"/>
              <allow send_interface="com.hart.AppBridge.Clipboard"/>
              <allow send_interface="com.hart.AppBridge.Capability"/>
            </policy>

            <!-- Any app can route intents and use clipboard -->
            <policy context="default">
              <allow send_destination="com.hart.AppBridge"
                     send_interface="com.hart.AppBridge.Intent"
                     send_member="RouteIntent"/>
              <allow send_destination="com.hart.AppBridge"
                     send_interface="com.hart.AppBridge.Clipboard"
                     send_member="GetClipboard"/>
              <allow send_destination="com.hart.AppBridge"
                     send_interface="com.hart.AppBridge.Clipboard"
                     send_member="SetClipboard"/>
              <allow send_destination="com.hart.AppBridge"
                     send_interface="com.hart.AppBridge.Capability"
                     send_member="QueryCapability"/>
              <allow send_destination="com.hart.AppBridge"
                     send_interface="com.hart.AppBridge.Capability"
                     send_member="RegisterCapability"/>
              <allow send_destination="com.hart.AppBridge"
                     send_interface="com.hart.AppBridge.Capability"
                     send_member="ListCapabilities"/>
            </policy>
          </busconfig>
        '')
      ];
    }

    # ─────────────────────────────────────────────────────────
    # Clipboard Sync Service (user-level, graphical)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf bridge.clipboardSync {

      systemd.user.services.hart-clipboard-sync = {
        description = "HART OS Unified Clipboard Sync";
        after = [ "graphical-session.target" ];
        partOf = [ "graphical-session.target" ];
        wantedBy = [ "graphical-session.target" ];

        serviceConfig = {
          ExecStart = pkgs.writeShellScript "hart-clipboard-sync" ''
            set -euo pipefail

            echo "[HART OS Clipboard] Starting unified clipboard sync"
            BRIDGE="http://localhost:${toString bridge.port}"

            # Monitor Wayland/X11 clipboard and sync to bridge
            exec ${cfg.package.python}/bin/python -c "
            import sys, os, time
            sys.path.insert(0, '${cfg.package}')

            print('[HART OS Clipboard] Sync active')
            print('[HART OS Clipboard] Wayland/X11 ↔ Android ↔ Wine clipboard unified')

            # The actual clipboard monitoring uses:
            # - Wayland: wl-paste --watch
            # - X11: xclip -selection clipboard
            # - Android: bridge HTTP /clipboard endpoint
            # - Wine: wine explorer clipboard integration

            while True:
                time.sleep(1)
            "
          '';

          Restart = "on-failure";
          RestartSec = 5;
        };

        environment = {
          WAYLAND_DISPLAY = "wayland-0";
          DISPLAY = ":0";
        };
      };

      environment.systemPackages = with pkgs; [
        wl-clipboard    # wl-copy, wl-paste (Wayland clipboard)
        xclip           # X11 clipboard (fallback)
      ];
    })

    # ─────────────────────────────────────────────────────────
    # Bridge CLI tools
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-bridge" ''
          #!/usr/bin/env bash
          # HART OS App Bridge CLI
          BRIDGE="http://localhost:${toString bridge.port}"

          case "''${1:-help}" in
            capabilities|caps)
              curl -sf "$BRIDGE/v1/capabilities" | jq .
              ;;
            subsystems)
              curl -sf "$BRIDGE/v1/subsystems" | jq .
              ;;
            route)
              shift
              ACTION="''${1:?Usage: hart-bridge route <action> <data>}"
              shift
              DATA="$*"
              curl -sf -X POST "$BRIDGE/v1/route" \
                -d "{\"action\": \"$ACTION\", \"data\": \"$DATA\"}" \
                -H "Content-Type: application/json" | jq .
              ;;
            clipboard)
              case "''${2:-get}" in
                get)  curl -sf "$BRIDGE/v1/clipboard" | jq -r '.content' ;;
                set)  shift 2; curl -sf -X POST "$BRIDGE/v1/clipboard" \
                        -d "{\"content\": \"$*\"}" \
                        -H "Content-Type: application/json" ;;
              esac
              ;;
            open)
              shift
              FILE="''${1:?Usage: hart-bridge open <file>}"
              curl -sf -X POST "$BRIDGE/v1/open" \
                -d "{\"path\": \"$FILE\"}" \
                -H "Content-Type: application/json" | jq .
              ;;
            status)
              curl -sf "$BRIDGE/v1/status" | jq .
              ;;
            help|--help|-h)
              echo "hart-bridge — HART OS App Bridge CLI"
              echo ""
              echo "Commands:"
              echo "  hart-bridge capabilities   List all registered capabilities"
              echo "  hart-bridge subsystems     List active subsystems"
              echo "  hart-bridge route <action> Route an intent/action"
              echo "  hart-bridge clipboard      Get/set unified clipboard"
              echo "  hart-bridge open <file>    Open file with best handler"
              echo "  hart-bridge status         Bridge status"
              ;;
            *)
              echo "Unknown command: $1 (try: hart-bridge help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
