{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Compute Mesh — Privacy-Bounded Cross-Device Intelligence
# ═══════════════════════════════════════════════════════════════
#
# Your devices are a single brain. Phone, laptop, desktop, server
# — they all share compute when they belong to the same user.
#
# Privacy boundary: user_id (Ed25519 keypair). Only YOUR devices
# can join YOUR mesh. Different users NEVER share compute here.
# (Cross-user sharing is handled by the hive's distributed task
# coordinator — a completely separate system.)
#
# Transport: WireGuard tunnel (encrypted, authenticated, NAT-piercing)
#
# Discovery:
#   LAN   → existing UDP beacon (port 6780) + device fingerprint
#   WAN   → STUN/TURN for NAT traversal, then WireGuard
#   Internet → WireGuard over public IP or relay
#
# How it works:
#
#   ┌─────────────────┐        WireGuard        ┌──────────────────┐
#   │     Phone        │ ◄═══════════════════► │     Desktop       │
#   │  (1GB VRAM)      │      encrypted         │  (RTX 4090)      │
#   │  asks: "run 7B"  │      tunnel            │  runs: 7B model  │
#   │  gets: response  │                         │  returns: result │
#   └─────────────────┘                         └──────────────────┘
#           │                                            │
#           │         Same user_id = same mesh           │
#           │                                            │
#   ┌───────┴─────────┐                         ┌───────┴──────────┐
#   │   Edge Node      │                         │   Home Server    │
#   │  (Raspberry Pi)  │ ◄════════════════════► │  (48GB RAM)      │
#   │  contributes CPU │      WireGuard          │  runs: 70B model │
#   └─────────────────┘                         └──────────────────┘
#
# All mesh traffic is end-to-end encrypted. The OS treats remote
# compute as transparently as local compute — the Model Bus routes
# to mesh peers when local resources are insufficient.

let
  cfg = config.hart;
  mesh = config.hart.computeMesh;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.computeMesh = {

    enable = lib.mkEnableOption "HART OS Compute Mesh (same-user cross-device compute sharing)";

    ports = {
      mesh = lib.mkOption {
        type = lib.types.port;
        default = 6795;
        description = "WireGuard mesh tunnel port (UDP)";
      };
      taskRelay = lib.mkOption {
        type = lib.types.port;
        default = 6796;
        description = "Task offload HTTP endpoint port";
      };
    };

    maxOffloadPercent = lib.mkOption {
      type = lib.types.int;
      default = 50;
      description = "Maximum percentage of local compute to donate to mesh peers (0-100)";
    };

    allowWAN = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Allow mesh connections over internet (not just LAN)";
    };

    stunServer = lib.mkOption {
      type = lib.types.str;
      default = "stun:stun.l.google.com:19302";
      description = "STUN server for NAT traversal (WAN mesh)";
    };

    meshInterface = lib.mkOption {
      type = lib.types.str;
      default = "hart-mesh0";
      description = "WireGuard interface name for the compute mesh";
    };

    meshSubnet = lib.mkOption {
      type = lib.types.str;
      default = "10.99.0.0/16";
      description = "IP subnet for mesh peers";
    };

    autoAcceptSameUser = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Auto-accept mesh pairing from devices with same user_id";
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && mesh.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # WireGuard kernel module + tools
    # ─────────────────────────────────────────────────────────
    {
      boot.kernelModules = [ "wireguard" ];

      environment.systemPackages = with pkgs; [
        wireguard-tools     # wg, wg-quick
        stun                # NAT traversal client
      ];

      # Firewall: allow WireGuard mesh + task relay
      networking.firewall = {
        allowedUDPPorts = [ mesh.ports.mesh ];
        allowedTCPPorts = [ mesh.ports.taskRelay ];
      };

      # Runtime directories
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/mesh 0700 hart hart -"              # Mesh state
        "d /var/lib/hart/mesh/peers 0700 hart hart -"        # Known peers
        "d /var/lib/hart/mesh/keys 0700 hart hart -"         # WireGuard keys
        "d /run/hart/mesh 0750 hart hart -"                  # Runtime state
      ];
    }

    # ─────────────────────────────────────────────────────────
    # Mesh Key Generator (first-boot)
    # ─────────────────────────────────────────────────────────
    {
      systemd.services.hart-mesh-keygen = {
        description = "HART OS Compute Mesh Key Generation";
        after = [ "hart-first-boot.service" ];
        wants = [ "hart-first-boot.service" ];
        wantedBy = [ "hart.target" ];

        unitConfig.ConditionPathExists = "!/var/lib/hart/mesh/keys/private.key";

        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          User = "root";  # Need root for WireGuard key gen

          ExecStart = pkgs.writeShellScript "hart-mesh-keygen" ''
            set -euo pipefail

            KEY_DIR="/var/lib/hart/mesh/keys"
            mkdir -p "$KEY_DIR"

            echo "[HART OS Mesh] Generating WireGuard keypair..."

            # Generate WireGuard keypair
            ${pkgs.wireguard-tools}/bin/wg genkey | tee "$KEY_DIR/private.key" | \
              ${pkgs.wireguard-tools}/bin/wg pubkey > "$KEY_DIR/public.key"

            # Derive mesh device ID from node identity
            if [[ -f "${cfg.dataDir}/node_public.key" ]]; then
              # Use first 4 bytes of node public key as device index
              DEVICE_IDX=$(xxd -p "${cfg.dataDir}/node_public.key" | head -c 4)
              DEVICE_NUM=$((16#$DEVICE_IDX % 65534 + 1))
              echo "$DEVICE_NUM" > "$KEY_DIR/device_index"
              echo "[HART OS Mesh] Device index: $DEVICE_NUM"

              # Compute mesh IP: 10.99.{high}.{low}
              HIGH=$((DEVICE_NUM / 256))
              LOW=$((DEVICE_NUM % 256))
              echo "10.99.$HIGH.$LOW/16" > "$KEY_DIR/mesh_ip"
              echo "[HART OS Mesh] Mesh IP: 10.99.$HIGH.$LOW"
            else
              echo "[HART OS Mesh] WARNING: No node identity — using random device index"
              DEVICE_NUM=$((RANDOM % 65534 + 1))
              echo "$DEVICE_NUM" > "$KEY_DIR/device_index"
              HIGH=$((DEVICE_NUM / 256))
              LOW=$((DEVICE_NUM % 256))
              echo "10.99.$HIGH.$LOW/16" > "$KEY_DIR/mesh_ip"
            fi

            # Lock down permissions
            chmod 600 "$KEY_DIR/private.key"
            chmod 644 "$KEY_DIR/public.key"
            chown -R hart:hart "$KEY_DIR"
            chmod 600 "$KEY_DIR/private.key"  # re-apply after chown

            echo "[HART OS Mesh] Keys generated"
          '';
        };
      };
    }

    # ─────────────────────────────────────────────────────────
    # Compute Mesh Daemon — discovery, pairing, task relay
    # ─────────────────────────────────────────────────────────
    {
      systemd.services.hart-compute-mesh = {
        description = "HART OS Compute Mesh Daemon";
        documentation = [ "https://github.com/hertz-ai/HARTOS" ];
        after = [
          "hart.target"
          "hart-mesh-keygen.service"
          "hart-discovery.service"
          "network-online.target"
        ];
        wants = [
          "hart-mesh-keygen.service"
          "hart-discovery.service"
          "network-online.target"
        ];
        wantedBy = [ "hart.target" ];

        environment = {
          HEVOLVE_DATA_DIR = cfg.dataDir;
          HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
          MESH_TASK_RELAY_PORT = toString mesh.ports.taskRelay;
          MESH_WG_PORT = toString mesh.ports.mesh;
          MESH_MAX_OFFLOAD = toString mesh.maxOffloadPercent;
          MESH_ALLOW_WAN = if mesh.allowWAN then "1" else "0";
          MESH_STUN_SERVER = mesh.stunServer;
          MESH_INTERFACE = mesh.meshInterface;
          MESH_SUBNET = mesh.meshSubnet;
          MESH_AUTO_ACCEPT = if mesh.autoAcceptSameUser then "1" else "0";
          PYTHONDONTWRITEBYTECODE = "1";
          PYTHONUNBUFFERED = "1";
        };

        serviceConfig = {
          Type = "notify";
          User = "hart";
          Group = "hart";
          SupplementaryGroups = [ "systemd-network" ];

          # Need NET_ADMIN for WireGuard interface management
          AmbientCapabilities = [ "CAP_NET_ADMIN" "CAP_NET_BIND_SERVICE" ];

          ExecStart = pkgs.writeShellScript "hart-compute-mesh" ''
            set -euo pipefail

            KEY_DIR="/var/lib/hart/mesh/keys"
            PEER_DIR="/var/lib/hart/mesh/peers"

            echo "[HART OS Mesh] Starting compute mesh daemon"
            echo "[HART OS Mesh] Task relay: port ${toString mesh.ports.taskRelay}"
            echo "[HART OS Mesh] WireGuard: port ${toString mesh.ports.mesh}"
            echo "[HART OS Mesh] Max offload: ${toString mesh.maxOffloadPercent}%"
            echo "[HART OS Mesh] WAN: ${if mesh.allowWAN then "enabled" else "LAN only"}"

            # Read mesh identity
            if [[ ! -f "$KEY_DIR/mesh_ip" ]]; then
              echo "[HART OS Mesh] ERROR: No mesh IP — run hart-mesh-keygen first"
              exit 1
            fi

            MESH_IP=$(cat "$KEY_DIR/mesh_ip")
            PUB_KEY=$(cat "$KEY_DIR/public.key")
            echo "[HART OS Mesh] Identity: $MESH_IP (pubkey: ''${PUB_KEY:0:8}...)"

            # Count known peers
            PEER_COUNT=$(ls "$PEER_DIR"/*.json 2>/dev/null | wc -l || echo 0)
            echo "[HART OS Mesh] Known peers: $PEER_COUNT"

            # ── Start Python mesh daemon ──
            exec ${cfg.package.python}/bin/python -c "
            import sys, os
            sys.path.insert(0, '${cfg.package}')
            os.environ['HEVOLVE_DATA_DIR'] = '${cfg.dataDir}'

            from integrations.agent_engine.compute_mesh_service import ComputeMeshService

            mesh = ComputeMeshService(
                task_relay_port=${toString mesh.ports.taskRelay},
                wg_port=${toString mesh.ports.mesh},
                max_offload_percent=${toString mesh.maxOffloadPercent},
                allow_wan=${if mesh.allowWAN then "True" else "False"},
                stun_server='${mesh.stunServer}',
                mesh_interface='${mesh.meshInterface}',
                mesh_subnet='${mesh.meshSubnet}',
                auto_accept=${if mesh.autoAcceptSameUser then "True" else "False"},
            )

            import systemd.daemon
            systemd.daemon.notify('READY=1')

            mesh.serve_forever()
            "
          '';

          Restart = "on-failure";
          RestartSec = 10;
          WatchdogSec = 120;

          # Resource limits
          Slice = "hart-agents.slice";
          MemoryMax = "512M";
          CPUWeight = 50;

          # Security hardening
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [
            cfg.dataDir
            cfg.logDir
            "/run/hart/mesh"
            "/var/lib/hart/mesh"
          ];
          PrivateTmp = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK" ];
          SystemCallFilter = [ "@system-service" "@network-io" ];

          StandardOutput = "journal";
          StandardError = "journal";
          SyslogIdentifier = "hart-compute-mesh";
        };
      };
    }

    # ─────────────────────────────────────────────────────────
    # Mesh CLI tools
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-mesh" ''
          #!/usr/bin/env bash
          # HART OS Compute Mesh CLI
          RELAY="http://localhost:${toString mesh.ports.taskRelay}"

          case "''${1:-help}" in
            status)
              curl -sf "$RELAY/mesh/status" | jq .
              ;;
            peers)
              curl -sf "$RELAY/mesh/peers" | jq .
              ;;
            pair)
              echo "Pairing request..."
              if [[ -z "''${2:-}" ]]; then
                echo "Usage: hart-mesh pair <peer_address>"
                exit 1
              fi
              curl -sf -X POST "$RELAY/mesh/pair" \
                -d "{\"peer_address\": \"$2\"}" \
                -H "Content-Type: application/json" | jq .
              ;;
            offload)
              shift
              if [[ -z "''${1:-}" ]]; then
                echo "Usage: hart-mesh offload <prompt>"
                exit 1
              fi
              curl -sf -X POST "$RELAY/mesh/infer" \
                -d "{\"prompt\": \"$*\"}" \
                -H "Content-Type: application/json" | jq .
              ;;
            help|--help|-h)
              echo "hart-mesh — HART OS Compute Mesh CLI"
              echo ""
              echo "Commands:"
              echo "  hart-mesh status          Show mesh status + compute inventory"
              echo "  hart-mesh peers           List paired devices"
              echo "  hart-mesh pair <addr>     Pair with another device"
              echo "  hart-mesh offload <text>  Offload inference to mesh peer"
              ;;
            *)
              echo "Unknown command: $1 (try: hart-mesh help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
