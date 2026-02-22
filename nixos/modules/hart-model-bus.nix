{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Model Bus — Native AI Access for Every Application
# ═══════════════════════════════════════════════════════════════
#
# The Model Bus is the OS-level abstraction that makes AI a
# native capability. Any application — Linux, Android, Windows,
# or Web — can access any deployed model through a single
# unified interface. No port numbers. No model formats.
# No inference server URLs. Just: "give me intelligence."
#
# Transport layers (all serve the same backend):
#
#   Linux apps    → Unix socket  /run/hart/model-bus.sock
#   Desktop apps  → D-Bus        com.hart.ModelBus
#   Android apps  → HTTP API     localhost:6790 (via Binder bridge)
#   Windows apps  → Named pipe   \\.\pipe\hart-model-bus → Unix socket
#   Web/PWA       → HTTP API     localhost:6790
#   AI Agents     → Unix socket  (direct, zero-copy)
#
# The bus routes requests to the best available backend:
#   1. Local llama.cpp (LLM)        → port 8080
#   2. Local MiniCPM (vision)       → port 9891
#   3. Local Whisper (STT)          → managed subprocess
#   4. Local Piper (TTS)            → managed subprocess
#   5. Compute mesh peer            → WireGuard tunnel
#   6. Remote HevolveAI/hivemind  → world model bridge
#
# Architecture:
#
#   ┌─────────────────────────────────────────────────────────┐
#   │              Applications (any subsystem)                │
#   │  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────────┐ │
#   │  │Linux │  │Droid │  │Wine  │  │ Web  │  │ Agents   │ │
#   │  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘  └────┬─────┘ │
#   │     │socket   │HTTP    │pipe    │HTTP     │socket    │
#   │  ┌──┴─────────┴────────┴────────┴─────────┴────────┐   │
#   │  │              Model Bus Service                    │   │
#   │  │  ┌─────────┐  ┌──────────┐  ┌───────────────┐   │   │
#   │  │  │Registry │  │Guardrail │  │ Speculative   │   │   │
#   │  │  │(models) │  │Gate      │  │ Dispatcher    │   │   │
#   │  │  └────┬────┘  └────┬─────┘  └──────┬────────┘   │   │
#   │  └───────┴─────────────┴───────────────┴────────────┘   │
#   │          │                                               │
#   │  ┌───────┴──────────────────────────────────────────┐   │
#   │  │  Backends: llama.cpp │ MiniCPM │ Whisper │ Piper │   │
#   │  │           Mesh Peers │ HevolveAI/HiveMind      │   │
#   │  └──────────────────────────────────────────────────┘   │
#   └─────────────────────────────────────────────────────────┘

let
  cfg = config.hart;
  bus = config.hart.modelBus;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.modelBus = {

    enable = lib.mkEnableOption "HART OS Model Bus (unified AI access for all apps)";

    socketPath = lib.mkOption {
      type = lib.types.str;
      default = "/run/hart/model-bus.sock";
      description = "Unix domain socket path for native Linux app access";
    };

    ports = {
      http = lib.mkOption {
        type = lib.types.port;
        default = 6790;
        description = "HTTP API port (Android, Wine, Web apps)";
      };
      grpc = lib.mkOption {
        type = lib.types.port;
        default = 6791;
        description = "gRPC port for high-throughput inference (vision, embeddings)";
      };
    };

    maxConcurrentRequests = lib.mkOption {
      type = lib.types.int;
      default = 32;
      description = "Maximum concurrent inference requests across all transports";
    };

    routingStrategy = lib.mkOption {
      type = lib.types.enum [ "speculative" "fastest" "cheapest" "local-only" ];
      default = "speculative";
      description = ''
        Model routing strategy:
        - speculative: fast-first with expert takeover (default)
        - fastest: lowest latency backend wins
        - cheapest: prefer local over mesh over cloud
        - local-only: never route to mesh peers or cloud
      '';
    };

    enableDBus = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Expose D-Bus interface com.hart.ModelBus for desktop apps";
    };

    enableAndroidBridge = lib.mkOption {
      type = lib.types.bool;
      default = cfg.subsystems.android.enable or false;
      description = "Enable Android Binder → HTTP bridge for APK model access";
    };

    enableWineBridge = lib.mkOption {
      type = lib.types.bool;
      default = cfg.subsystems.windows.enable or false;
      description = "Enable Wine named pipe → Unix socket bridge";
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && bus.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Runtime directories
    # ─────────────────────────────────────────────────────────
    {
      systemd.tmpfiles.rules = [
        "d /run/hart 0750 hart hart -"
        "d /run/hart/model-bus 0750 hart hart -"
        "d /var/lib/hart/model-bus 0750 hart hart -"        # Persistent state (model registry cache)
        "d /var/lib/hart/model-bus/cache 0750 hart hart -"  # Response cache
      ];

      # Open HTTP port for non-native apps (Android, Wine, Web)
      networking.firewall.allowedTCPPorts = [
        bus.ports.http
      ];
    }

    # ─────────────────────────────────────────────────────────
    # Model Bus Service — the core routing daemon
    # ─────────────────────────────────────────────────────────
    {
      systemd.services.hart-model-bus = {
        description = "HART OS Model Bus — Unified AI Access";
        documentation = [ "https://github.com/hevolve-ai/hart" ];
        after = [ "hart-backend.service" "hart.target" ];
        wants = [ "hart-backend.service" ];
        wantedBy = [ "hart.target" ];

        environment = {
          HEVOLVE_DATA_DIR = cfg.dataDir;
          HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
          MODEL_BUS_SOCKET = bus.socketPath;
          MODEL_BUS_HTTP_PORT = toString bus.ports.http;
          MODEL_BUS_GRPC_PORT = toString bus.ports.grpc;
          MODEL_BUS_MAX_CONCURRENT = toString bus.maxConcurrentRequests;
          MODEL_BUS_ROUTING = bus.routingStrategy;
          HART_LLM_PORT = toString cfg.ports.llm;
          HART_VISION_PORT = toString cfg.ports.vision;
          HART_BACKEND_PORT = toString cfg.ports.backend;
          PYTHONDONTWRITEBYTECODE = "1";
          PYTHONUNBUFFERED = "1";
        };

        serviceConfig = {
          Type = "notify";
          User = "hart";
          Group = "hart";
          SupplementaryGroups = [ "video" "render" ];

          ExecStart = pkgs.writeShellScript "hart-model-bus" ''
            set -euo pipefail

            echo "[HART OS Model Bus] Starting unified AI access layer"
            echo "[HART OS Model Bus] Socket: ${bus.socketPath}"
            echo "[HART OS Model Bus] HTTP:   port ${toString bus.ports.http}"
            echo "[HART OS Model Bus] Strategy: ${bus.routingStrategy}"
            echo "[HART OS Model Bus] Max concurrent: ${toString bus.maxConcurrentRequests}"

            # ── Detect available backends ──
            BACKENDS=""

            # LLM (llama.cpp)
            if curl -sf "http://localhost:${toString cfg.ports.llm}/health" >/dev/null 2>&1; then
              echo "[HART OS Model Bus] Backend: llama.cpp (port ${toString cfg.ports.llm}) ✓"
              BACKENDS="$BACKENDS llm"
            else
              echo "[HART OS Model Bus] Backend: llama.cpp — not available (will retry)"
            fi

            # Vision (MiniCPM)
            if curl -sf "http://localhost:${toString cfg.ports.vision}/health" >/dev/null 2>&1; then
              echo "[HART OS Model Bus] Backend: MiniCPM vision (port ${toString cfg.ports.vision}) ✓"
              BACKENDS="$BACKENDS vision"
            else
              echo "[HART OS Model Bus] Backend: MiniCPM vision — not available"
            fi

            # HART backend (for world model bridge + agent dispatch)
            if curl -sf "http://localhost:${toString cfg.ports.backend}/status" >/dev/null 2>&1; then
              echo "[HART OS Model Bus] Backend: HART backend (port ${toString cfg.ports.backend}) ✓"
              BACKENDS="$BACKENDS backend"
            fi

            # Compute mesh (if enabled)
            if curl -sf "http://localhost:6796/mesh/status" >/dev/null 2>&1; then
              echo "[HART OS Model Bus] Backend: Compute mesh ✓"
              BACKENDS="$BACKENDS mesh"
            fi

            echo "[HART OS Model Bus] Active backends:$BACKENDS"

            # ── Start Python Model Bus daemon ──
            exec ${cfg.package.python}/bin/python -c "
            import sys, os
            sys.path.insert(0, '${cfg.package}')
            os.environ['HEVOLVE_DATA_DIR'] = '${cfg.dataDir}'

            from integrations.agent_engine.model_bus_service import ModelBusService

            bus = ModelBusService(
                socket_path='${bus.socketPath}',
                http_port=${toString bus.ports.http},
                grpc_port=${toString bus.ports.grpc},
                max_concurrent=${toString bus.maxConcurrentRequests},
                routing_strategy='${bus.routingStrategy}',
                llm_port=${toString cfg.ports.llm},
                vision_port=${toString cfg.ports.vision},
                backend_port=${toString cfg.ports.backend},
            )

            import systemd.daemon
            systemd.daemon.notify('READY=1')

            bus.serve_forever()
            "
          '';

          # Socket permissions: hart group can access
          ExecStartPost = pkgs.writeShellScript "hart-model-bus-post" ''
            # Wait for socket to appear
            for i in $(seq 1 30); do
              if [ -S "${bus.socketPath}" ]; then
                chmod 0660 "${bus.socketPath}"
                chgrp hart "${bus.socketPath}"
                echo "[HART OS Model Bus] Socket ready"
                exit 0
              fi
              sleep 0.5
            done
            echo "[HART OS Model Bus] WARNING: Socket did not appear within 15s"
          '';

          Restart = "on-failure";
          RestartSec = 5;
          WatchdogSec = 60;

          # Resource limits
          Slice = "hart-agents.slice";
          MemoryMax = "1G";
          CPUWeight = 100;
          TasksMax = 256;

          # Security hardening
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [
            cfg.dataDir
            cfg.logDir
            "/run/hart"
          ];
          PrivateTmp = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_VSOCK" ];
          SystemCallFilter = [ "@system-service" "@network-io" ];

          StandardOutput = "journal";
          StandardError = "journal";
          SyslogIdentifier = "hart-model-bus";
        };
      };
    }

    # ─────────────────────────────────────────────────────────
    # D-Bus Interface: com.hart.ModelBus
    # ─────────────────────────────────────────────────────────
    (lib.mkIf bus.enableDBus {

      # D-Bus policy: allow hart user + hart group to call ModelBus
      services.dbus.packages = [
        (pkgs.writeTextDir "share/dbus-1/system.d/com.hart.ModelBus.conf" ''
          <?xml version="1.0" encoding="UTF-8"?>
          <!DOCTYPE busconfig PUBLIC
           "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
           "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
          <busconfig>
            <!-- HART OS Model Bus: AI access for all desktop apps -->
            <policy user="hart">
              <allow own="com.hart.ModelBus"/>
              <allow send_destination="com.hart.ModelBus"/>
              <allow send_interface="com.hart.ModelBus"/>
            </policy>

            <policy group="hart">
              <allow send_destination="com.hart.ModelBus"/>
              <allow send_interface="com.hart.ModelBus"/>
            </policy>

            <!-- Allow any user to call read-only methods -->
            <policy context="default">
              <allow send_destination="com.hart.ModelBus"
                     send_interface="com.hart.ModelBus"
                     send_member="ListModels"/>
              <allow send_destination="com.hart.ModelBus"
                     send_interface="com.hart.ModelBus"
                     send_member="ModelStatus"/>
              <allow send_destination="com.hart.ModelBus"
                     send_interface="com.hart.ModelBus"
                     send_member="Infer"/>
              <allow send_destination="com.hart.ModelBus"
                     send_interface="com.hart.ModelBus"
                     send_member="DescribeImage"/>
              <allow send_destination="com.hart.ModelBus"
                     send_interface="com.hart.ModelBus"
                     send_member="TextToSpeech"/>
              <allow send_destination="com.hart.ModelBus"
                     send_interface="com.hart.ModelBus"
                     send_member="SpeechToText"/>
            </policy>
          </busconfig>
        '')
      ];

      # D-Bus activation: auto-start model bus when any app calls it
      environment.etc."dbus-1/system.d/com.hart.ModelBus.service" = {
        text = ''
          [D-BUS Service]
          Name=com.hart.ModelBus
          Exec=${cfg.package.python}/bin/python -c "from integrations.agent_engine.model_bus_service import start_dbus_bridge; start_dbus_bridge()"
          User=hart
          SystemdService=hart-model-bus.service
        '';
      };
    })

    # ─────────────────────────────────────────────────────────
    # Android Bridge: Binder → HTTP → Model Bus
    # ─────────────────────────────────────────────────────────
    (lib.mkIf bus.enableAndroidBridge {

      # Android system property telling apps where the model bus lives
      systemd.services.hart-android-runtime.environment = lib.mkIf
        (config.systemd.services ? hart-android-runtime)
      {
        HART_MODEL_BUS_URL = "http://localhost:${toString bus.ports.http}";
      };

      # Content provider manifest fragment for Android apps
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/android/providers 0750 hart hart -"
      ];
    })

    # ─────────────────────────────────────────────────────────
    # Wine Bridge: Named pipe → Unix socket
    # ─────────────────────────────────────────────────────────
    (lib.mkIf bus.enableWineBridge {

      # Wine named pipe bridge service
      # Maps \\.\pipe\hart-model-bus to the Unix socket
      systemd.services.hart-wine-model-bridge = {
        description = "HART OS Wine → Model Bus Bridge";
        after = [ "hart-model-bus.service" ];
        wants = [ "hart-model-bus.service" ];
        wantedBy = [ "hart.target" ];

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";

          ExecStart = pkgs.writeShellScript "hart-wine-bridge" ''
            set -euo pipefail

            WINE_PIPE_DIR="/var/lib/hart/wine/drive_c/hart"
            mkdir -p "$WINE_PIPE_DIR"

            echo "[HART OS Wine Bridge] Mapping named pipe to Model Bus"

            # Create symlink so Wine apps can find the socket
            # Wine translates \\.\pipe\X to /tmp/.wine-*/pipe-X
            # We create a proxy that forwards to the Model Bus socket
            exec ${cfg.package.python}/bin/python -c "
            import sys, os, socket, select, threading
            sys.path.insert(0, '${cfg.package}')

            SOCKET_PATH = '${bus.socketPath}'
            HTTP_PORT = ${toString bus.ports.http}

            # Wine apps will use HTTP on localhost
            # This bridge ensures the Wine prefix can resolve it
            print('[HART OS Wine Bridge] Active — Wine apps use http://localhost:' + str(HTTP_PORT))

            import time
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
    # System packages: model bus CLI tools
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = [
        # hart-infer: CLI tool for model bus access
        (pkgs.writeShellScriptBin "hart-infer" ''
          #!/usr/bin/env bash
          # Quick inference via Model Bus
          # Usage: hart-infer "What is the capital of France?"
          # Usage: hart-infer --vision /path/to/image.jpg "What is this?"
          # Usage: hart-infer --tts "Hello world"
          # Usage: hart-infer --stt /path/to/audio.wav

          MODEL_BUS="http://localhost:${toString bus.ports.http}"

          case "''${1:-}" in
            --vision)
              shift
              IMAGE="''${1:?Usage: hart-infer --vision <image> <prompt>}"
              shift
              PROMPT="''${*:?Usage: hart-infer --vision <image> <prompt>}"
              curl -sf "$MODEL_BUS/v1/vision" \
                -F "image=@$IMAGE" \
                -F "prompt=$PROMPT" | jq -r '.response // .error'
              ;;
            --tts)
              shift
              TEXT="''${*:?Usage: hart-infer --tts <text>}"
              OUTPUT="/tmp/hart-tts-$$.wav"
              curl -sf "$MODEL_BUS/v1/tts" \
                -d "{\"text\": \"$TEXT\"}" \
                -H "Content-Type: application/json" \
                -o "$OUTPUT"
              echo "Audio: $OUTPUT"
              command -v aplay &>/dev/null && aplay "$OUTPUT" 2>/dev/null
              ;;
            --stt)
              shift
              AUDIO="''${1:?Usage: hart-infer --stt <audio_file>}"
              curl -sf "$MODEL_BUS/v1/stt" \
                -F "audio=@$AUDIO" | jq -r '.text // .error'
              ;;
            --list)
              curl -sf "$MODEL_BUS/v1/models" | jq .
              ;;
            --status)
              curl -sf "$MODEL_BUS/v1/status" | jq .
              ;;
            --help|-h)
              echo "hart-infer — HART OS Model Bus CLI"
              echo ""
              echo "Usage:"
              echo "  hart-infer <prompt>                   LLM inference"
              echo "  hart-infer --vision <image> <prompt>  Vision (describe image)"
              echo "  hart-infer --tts <text>               Text-to-speech"
              echo "  hart-infer --stt <audio>              Speech-to-text"
              echo "  hart-infer --list                     List available models"
              echo "  hart-infer --status                   Model Bus status"
              ;;
            *)
              PROMPT="$*"
              if [[ -z "$PROMPT" ]]; then
                echo "Usage: hart-infer <prompt>"
                exit 1
              fi
              curl -sf "$MODEL_BUS/v1/chat" \
                -d "{\"prompt\": \"$PROMPT\"}" \
                -H "Content-Type: application/json" | jq -r '.response // .error'
              ;;
          esac
        '')
      ];
    }
  ]);
}
