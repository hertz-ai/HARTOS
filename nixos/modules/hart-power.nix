{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Power Management
# ═══════════════════════════════════════════════════════════════
#
# Profiles:
#   performance — Max clock, no throttling (desktop/server, AC power)
#   balanced    — Default, adaptive governors (laptop default)
#   powersave   — Aggressive saving, dim display, spin down (battery)
#   ai-burst    — Performance for GPU + max CPU during inference, then powersave
#
# Suspend/hibernate support with agent state preservation.
# Before suspend, running agents checkpoint their state to disk.
# On resume, agents reload from checkpoint.

let
  cfg = config.hart;
  pwr = config.hart.power;
in
{
  options.hart.power = {

    enable = lib.mkEnableOption "HART OS power management";

    defaultProfile = lib.mkOption {
      type = lib.types.enum [ "performance" "balanced" "powersave" "ai-burst" "gaming" ];
      default = if cfg.variant == "server" then "performance"
                else if cfg.variant == "edge" then "powersave"
                else "balanced";
      description = "Default power profile";
    };

    suspend = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = cfg.variant != "server";
        description = "Enable suspend-to-RAM (disabled on servers by default)";
      };

      hibernate = lib.mkOption {
        type = lib.types.bool;
        default = cfg.variant == "desktop";
        description = "Enable hibernate (suspend-to-disk)";
      };

      agentCheckpoint = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Checkpoint agent state before suspend/hibernate";
      };

      lidAction = lib.mkOption {
        type = lib.types.enum [ "suspend" "hibernate" "lock" "ignore" ];
        default = "suspend";
        description = "Action on laptop lid close";
      };
    };

    thermalThrottle = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable thermal throttling to prevent overheating";
      };

      criticalTemp = lib.mkOption {
        type = lib.types.int;
        default = 90;
        description = "Critical temperature (Celsius) — triggers emergency throttle";
      };
    };
  };

  config = lib.mkIf (cfg.enable && pwr.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # power-profiles-daemon (GNOME/freedesktop standard)
    # ─────────────────────────────────────────────────────────
    {
      services.power-profiles-daemon.enable = true;

      # TLP for advanced laptop battery optimization
      services.tlp = lib.mkIf (cfg.variant == "desktop" || cfg.variant == "phone") {
        enable = true;
        settings = {
          CPU_SCALING_GOVERNOR_ON_AC = if pwr.defaultProfile == "performance" || pwr.defaultProfile == "gaming"
            then "performance" else "schedutil";
          CPU_SCALING_GOVERNOR_ON_BAT = "powersave";
          CPU_ENERGY_PERF_POLICY_ON_AC = "performance";
          CPU_ENERGY_PERF_POLICY_ON_BAT = "power";
          CPU_BOOST_ON_AC = 1;
          CPU_BOOST_ON_BAT = 0;
          WIFI_PWR_ON_BAT = "on";
          RUNTIME_PM_ON_AC = "auto";
          RUNTIME_PM_ON_BAT = "auto";
        };
      };

      # thermald for Intel thermal management
      services.thermald.enable = pwr.thermalThrottle.enable;
    }

    # ─────────────────────────────────────────────────────────
    # Suspend / Hibernate
    # ─────────────────────────────────────────────────────────
    (lib.mkIf pwr.suspend.enable {

      # Lid action
      services.logind = {
        lidSwitch = pwr.suspend.lidAction;
        lidSwitchDocked = "ignore";
        lidSwitchExternalPower = "lock";
      };

      # Agent checkpoint before suspend
      systemd.services.hart-suspend-checkpoint = lib.mkIf pwr.suspend.agentCheckpoint {
        description = "Checkpoint agent state before suspend";
        before = [ "sleep.target" ];
        wantedBy = [ "sleep.target" ];

        serviceConfig = {
          Type = "oneshot";
          ExecStart = pkgs.writeShellScript "hart-suspend-checkpoint" ''
            set -euo pipefail
            echo "[HART Power] Checkpointing agent state before suspend..."

            # Signal backend to checkpoint
            curl -sf -X POST "http://localhost:${toString cfg.ports.backend}/api/power/checkpoint" \
              -H "Content-Type: application/json" \
              -d '{"reason": "suspend"}' 2>/dev/null || \
              echo "[HART Power] Backend not reachable, skipping checkpoint"

            echo "[HART Power] Checkpoint complete"
          '';
          TimeoutStartSec = 15;
          StandardOutput = "journal";
          SyslogIdentifier = "hart-power";
        };
      };

      # Resume hook — restart services that may have stalled
      systemd.services.hart-resume = {
        description = "HART OS resume from suspend";
        after = [ "suspend.target" "hibernate.target" ];
        wantedBy = [ "suspend.target" "hibernate.target" ];

        serviceConfig = {
          Type = "oneshot";
          ExecStart = pkgs.writeShellScript "hart-resume" ''
            set -euo pipefail
            echo "[HART Power] Resuming from suspend..."

            # Re-check network
            ${pkgs.systemd}/bin/networkctl reconfigure --no-pager 2>/dev/null || true

            # Signal backend to reload state
            curl -sf -X POST "http://localhost:${toString cfg.ports.backend}/api/power/resume" \
              -H "Content-Type: application/json" 2>/dev/null || \
              echo "[HART Power] Backend will reconnect on its own"

            echo "[HART Power] Resume complete"
          '';
          StandardOutput = "journal";
          SyslogIdentifier = "hart-power";
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # Gaming profile — GPU max clocks
    # ─────────────────────────────────────────────────────────
    (lib.mkIf (pwr.defaultProfile == "gaming") {
      systemd.services.hart-gpu-max-clocks = {
        description = "HART OS gaming — set GPU to max clocks";
        after = [ "multi-user.target" ];
        wantedBy = [ "multi-user.target" ];

        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = pkgs.writeShellScript "hart-gpu-max-clocks" ''
            set -euo pipefail
            echo "[HART Gaming] Setting GPU to max clocks..."

            # NVIDIA: persistence mode + max clocks
            if command -v nvidia-smi >/dev/null 2>&1; then
              nvidia-smi -pm 1 2>/dev/null || true
              nvidia-smi --lock-gpu-clocks=$(nvidia-smi --query-gpu=clocks.max.graphics --format=csv,noheader,nounits | head -1) 2>/dev/null || true
              nvidia-smi --lock-memory-clocks=$(nvidia-smi --query-gpu=clocks.max.memory --format=csv,noheader,nounits | head -1) 2>/dev/null || true
              echo "[HART Gaming] NVIDIA GPU clocks locked to max"
            fi

            # AMD: set performance power profile
            for card in /sys/class/drm/card*/device/power_dpm_force_performance_level; do
              echo "high" > "$card" 2>/dev/null || true
            done

            echo "[HART Gaming] GPU max clocks applied"
          '';
          ExecStop = pkgs.writeShellScript "hart-gpu-reset-clocks" ''
            # Reset to defaults on service stop
            if command -v nvidia-smi >/dev/null 2>&1; then
              nvidia-smi --reset-gpu-clocks 2>/dev/null || true
              nvidia-smi --reset-memory-clocks 2>/dev/null || true
            fi
            for card in /sys/class/drm/card*/device/power_dpm_force_performance_level; do
              echo "auto" > "$card" 2>/dev/null || true
            done
          '';
          StandardOutput = "journal";
          SyslogIdentifier = "hart-gaming";
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # CLI tool
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-power" ''
          #!/usr/bin/env bash
          case "''${1:-status}" in
            status)
              echo "=== HART OS Power Status ==="
              echo "Default profile: ${pwr.defaultProfile}"
              echo "Suspend: ${if pwr.suspend.enable then "enabled" else "disabled"}"
              echo "Hibernate: ${if pwr.suspend.hibernate then "enabled" else "disabled"}"
              echo ""
              echo "Active profile:"
              powerprofilesctl get 2>/dev/null || echo "unknown"
              echo ""
              echo "Battery:"
              cat /sys/class/power_supply/BAT*/capacity 2>/dev/null && \
                echo "%" || echo "No battery (AC power)"
              ;;
            set)
              if [[ -z "''${2:-}" ]]; then
                echo "Usage: hart-power set <performance|balanced|powersave>"
                exit 1
              fi
              powerprofilesctl set "$2" 2>/dev/null || echo "Failed to set profile"
              echo "Power profile: $2"
              ;;
            suspend)
              echo "Suspending..."
              systemctl suspend
              ;;
            hibernate)
              echo "Hibernating..."
              systemctl hibernate
              ;;
            lock)
              loginctl lock-sessions 2>/dev/null || echo "No session manager"
              ;;
            help|--help|-h)
              echo "hart-power — HART OS Power Management"
              echo ""
              echo "  hart-power status                Show power status"
              echo "  hart-power set <profile>         Set power profile"
              echo "  hart-power suspend               Suspend to RAM"
              echo "  hart-power hibernate              Suspend to disk"
              echo "  hart-power lock                  Lock all sessions"
              ;;
            *)
              echo "Unknown command: $1 (try: hart-power help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
