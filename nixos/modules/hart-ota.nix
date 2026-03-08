{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Over-The-Air Update Service
# ═══════════════════════════════════════════════════════════════
#
# Wraps the existing 7-stage upgrade pipeline (upgrade_orchestrator.py)
# as a systemd service with NixOS-native atomic switching.
#
# Pipeline: BUILD → TEST → AUDIT → BENCHMARK → SIGN → CANARY → DEPLOY
#
# NixOS advantage: every update is a new system generation, so
# rollback is always one `nixos-rebuild switch --rollback` away.
# The canary stage leverages this — if health degrades, the OS
# atomically reverts to the previous generation.
#
# Two modes:
#   - Pull: timer-based check against upstream (default)
#   - Push: gossip-received upgrade from peer (via upgrade_orchestrator)

let
  cfg = config.hart;
  ota = config.hart.ota;
  hartApp = config.hart.package;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.ota = {

    enable = lib.mkEnableOption "HART OS over-the-air updates";

    channel = lib.mkOption {
      type = lib.types.enum [ "stable" "testing" "nightly" ];
      default = "stable";
      description = "Update channel (stable, testing, nightly)";
    };

    checkInterval = lib.mkOption {
      type = lib.types.str;
      default = "1h";
      description = "How often to check for updates (systemd OnUnitActiveSec format)";
    };

    autoApply = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Automatically apply updates after canary passes.
        If false, updates are downloaded and staged but require manual approval.
      '';
    };

    canaryDuration = lib.mkOption {
      type = lib.types.int;
      default = 1800;
      description = "Canary monitoring duration in seconds (default: 30 minutes)";
    };

    canaryPercent = lib.mkOption {
      type = lib.types.int;
      default = 10;
      description = "Percentage of services to canary before full rollout (1-100)";
    };

    maxRollbackGenerations = lib.mkOption {
      type = lib.types.int;
      default = 5;
      description = "Number of NixOS generations to keep for rollback";
    };

    flakeRef = lib.mkOption {
      type = lib.types.str;
      default = "github:hevolve-ai/hart";
      description = "Nix flake reference for pulling updates";
    };

    preUpdateHook = lib.mkOption {
      type = lib.types.lines;
      default = "";
      description = "Shell commands to run before applying an update";
    };

    postUpdateHook = lib.mkOption {
      type = lib.types.lines;
      default = "";
      description = "Shell commands to run after a successful update";
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && ota.enable) {

    # ─────────────────────────────────────────────────────────
    # Runtime directories
    # ─────────────────────────────────────────────────────────
    systemd.tmpfiles.rules = [
      "d /var/lib/hart/ota 0750 hart hart -"
      "d /var/lib/hart/ota/staging 0750 hart hart -"
      "d /var/lib/hart/ota/history 0750 hart hart -"
    ];

    # ─────────────────────────────────────────────────────────
    # OTA Check Timer — periodic pull for updates
    # ─────────────────────────────────────────────────────────
    systemd.timers.hart-ota-check = {
      description = "HART OS OTA Update Check Timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "5min";
        OnUnitActiveSec = ota.checkInterval;
        RandomizedDelaySec = "5min";
        Persistent = true;
      };
    };

    # ─────────────────────────────────────────────────────────
    # OTA Check Service — check for new version, stage if found
    # ─────────────────────────────────────────────────────────
    systemd.services.hart-ota-check = {
      description = "HART OS OTA Update Check";
      after = [ "network-online.target" "hart-backend.service" ];
      wants = [ "network-online.target" ];

      environment = {
        HEVOLVE_DATA_DIR = cfg.dataDir;
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        HART_OTA_CHANNEL = ota.channel;
        HART_OTA_FLAKE_REF = ota.flakeRef;
        HART_OTA_AUTO_APPLY = if ota.autoApply then "1" else "0";
        HEVOLVE_CANARY_DURATION_SECONDS = toString ota.canaryDuration;
        HEVOLVE_CANARY_PCT = "0.${if ota.canaryPercent < 10 then "0${toString ota.canaryPercent}" else toString ota.canaryPercent}";
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "oneshot";
        User = "hart";
        Group = "hart";

        ExecStart = pkgs.writeShellScript "hart-ota-check" ''
          set -euo pipefail

          OTA_DIR="/var/lib/hart/ota"
          LOG="/var/log/hart/ota-check.log"

          echo "[HART OTA] Checking for updates (channel: ${ota.channel})"

          # ── Query current version ──
          CURRENT=$(nixos-version 2>/dev/null || echo "unknown")
          echo "[HART OTA] Current: $CURRENT"

          # ── Check upstream via Python orchestrator ──
          RESULT=$(${hartApp.python}/bin/python -c "
          import sys, json, os
          sys.path.insert(0, '${hartApp}')
          os.environ.setdefault('HEVOLVE_DB_PATH', '${cfg.dataDir}/hevolve_database.db')

          from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
          orch = UpgradeOrchestrator()
          status = orch.get_status()
          print(json.dumps(status))
          " 2>/dev/null) || RESULT='{"stage":"idle"}'

          STAGE=$(echo "$RESULT" | ${pkgs.jq}/bin/jq -r '.stage // "idle"')
          echo "[HART OTA] Pipeline stage: $STAGE"

          if [[ "$STAGE" == "idle" ]]; then
            # ── Check flake for new version ──
            echo "[HART OTA] Checking flake: ${ota.flakeRef}"
            REMOTE_REV=$(${pkgs.nix}/bin/nix flake metadata "${ota.flakeRef}" --json 2>/dev/null \
              | ${pkgs.jq}/bin/jq -r '.revision // "unknown"') || REMOTE_REV="check_failed"

            LOCAL_REV=$(${pkgs.nix}/bin/nix flake metadata /etc/nixos --json 2>/dev/null \
              | ${pkgs.jq}/bin/jq -r '.revision // "unknown"') || LOCAL_REV="unknown"

            echo "[HART OTA] Local: $LOCAL_REV"
            echo "[HART OTA] Remote: $REMOTE_REV"

            if [[ "$REMOTE_REV" != "check_failed" && "$REMOTE_REV" != "$LOCAL_REV" && "$REMOTE_REV" != "unknown" ]]; then
              echo "[HART OTA] New version available: $REMOTE_REV"

              # Write update metadata
              ${pkgs.jq}/bin/jq -n \
                --arg rev "$REMOTE_REV" \
                --arg channel "${ota.channel}" \
                --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                '{revision: $rev, channel: $channel, discovered_at: $ts, status: "available"}' \
                > "$OTA_DIR/pending_update.json"

              # Start the 7-stage pipeline via orchestrator
              ${hartApp.python}/bin/python -c "
              import sys, os
              sys.path.insert(0, '${hartApp}')
              os.environ.setdefault('HEVOLVE_DB_PATH', '${cfg.dataDir}/hevolve_database.db')

              from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
              orch = UpgradeOrchestrator()
              result = orch.start_upgrade('$REMOTE_REV', '$REMOTE_REV')
              print(f'[HART OTA] Pipeline started: {result}')
              " || echo "[HART OTA] Pipeline start failed"
            else
              echo "[HART OTA] System is up to date"
            fi
          elif [[ "$STAGE" == "completed" ]]; then
            echo "[HART OTA] Update completed, applying NixOS switch..."
            ${lib.optionalString (ota.preUpdateHook != "") ''
              echo "[HART OTA] Running pre-update hook..."
              ${ota.preUpdateHook}
            ''}

            if [[ "${if ota.autoApply then "1" else "0"}" == "1" ]]; then
              echo "[HART OTA] Auto-apply enabled, switching..."
              sudo nixos-rebuild switch --flake "${ota.flakeRef}#hart-${cfg.variant}" 2>&1 || {
                echo "[HART OTA] Switch failed, rolling back..."
                sudo nixos-rebuild switch --rollback 2>&1
              }
              ${lib.optionalString (ota.postUpdateHook != "") ''
                echo "[HART OTA] Running post-update hook..."
                ${ota.postUpdateHook}
              ''}
            else
              echo "[HART OTA] Update staged. Run 'hart-ota apply' to switch."
            fi
          else
            echo "[HART OTA] Pipeline in progress ($STAGE), advancing..."
            ${hartApp.python}/bin/python -c "
            import sys, os
            sys.path.insert(0, '${hartApp}')
            os.environ.setdefault('HEVOLVE_DB_PATH', '${cfg.dataDir}/hevolve_database.db')

            from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
            orch = UpgradeOrchestrator()
            result = orch.advance_pipeline()
            print(f'[HART OTA] Advanced: {result}')
            " || echo "[HART OTA] Advance failed"
          fi
        '';

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [
          cfg.dataDir
          cfg.logDir
          "/var/lib/hart/ota"
        ];
        PrivateTmp = true;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-ota-check";
      };
    };

    # ─────────────────────────────────────────────────────────
    # OTA Canary Monitor — health check during canary stage
    # ─────────────────────────────────────────────────────────
    systemd.services.hart-ota-canary = {
      description = "HART OS OTA Canary Health Monitor";
      after = [ "hart-backend.service" ];

      environment = {
        HEVOLVE_DATA_DIR = cfg.dataDir;
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        HEVOLVE_CANARY_DURATION_SECONDS = toString ota.canaryDuration;
      };

      serviceConfig = {
        Type = "oneshot";
        User = "hart";
        Group = "hart";

        ExecStart = pkgs.writeShellScript "hart-ota-canary" ''
          set -euo pipefail

          # Check if canary stage is active
          RESULT=$(${hartApp.python}/bin/python -c "
          import sys, json, os
          sys.path.insert(0, '${hartApp}')
          os.environ.setdefault('HEVOLVE_DB_PATH', '${cfg.dataDir}/hevolve_database.db')

          from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
          orch = UpgradeOrchestrator()
          health = orch.check_canary_health_status()
          print(json.dumps(health))
          " 2>/dev/null) || exit 0

          IS_CANARY=$(echo "$RESULT" | ${pkgs.jq}/bin/jq -r '.is_canary // false')
          HEALTHY=$(echo "$RESULT" | ${pkgs.jq}/bin/jq -r '.healthy // true')

          if [[ "$IS_CANARY" != "true" ]]; then
            exit 0
          fi

          if [[ "$HEALTHY" != "true" ]]; then
            echo "[HART OTA] Canary UNHEALTHY — triggering rollback"

            ${hartApp.python}/bin/python -c "
            import sys, os
            sys.path.insert(0, '${hartApp}')
            os.environ.setdefault('HEVOLVE_DB_PATH', '${cfg.dataDir}/hevolve_database.db')

            from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
            orch = UpgradeOrchestrator()
            orch.rollback('canary_health_failed')
            " || true

            # NixOS-level rollback
            sudo nixos-rebuild switch --rollback 2>&1 || true
            echo "[HART OTA] Rolled back to previous generation"
          else
            echo "[HART OTA] Canary healthy"
          fi
        '';

        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.dataDir cfg.logDir ];
        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-ota-canary";
      };
    };

    # Canary timer — check every 30s during canary window
    systemd.timers.hart-ota-canary = {
      description = "HART OS OTA Canary Health Timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "30s";
      };
    };

    # ─────────────────────────────────────────────────────────
    # Generation garbage collection — keep N rollback generations
    # ─────────────────────────────────────────────────────────
    systemd.services.hart-ota-gc = {
      description = "HART OS OTA Generation Garbage Collection";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = pkgs.writeShellScript "hart-ota-gc" ''
          set -euo pipefail
          echo "[HART OTA] Pruning old generations (keeping ${toString ota.maxRollbackGenerations})"
          ${pkgs.nix}/bin/nix-env --delete-generations \
            +${toString ota.maxRollbackGenerations} \
            --profile /nix/var/nix/profiles/system 2>/dev/null || true
          ${pkgs.nix}/bin/nix-collect-garbage --delete-older-than 30d 2>/dev/null || true
          echo "[HART OTA] Garbage collection complete"
        '';
        StandardOutput = "journal";
        SyslogIdentifier = "hart-ota-gc";
      };
    };

    systemd.timers.hart-ota-gc = {
      description = "HART OS OTA GC Timer (weekly)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "weekly";
        Persistent = true;
        RandomizedDelaySec = "1h";
      };
    };

    # ─────────────────────────────────────────────────────────
    # CLI tool
    # ─────────────────────────────────────────────────────────
    environment.systemPackages = [
      (pkgs.writeShellScriptBin "hart-ota" ''
        #!/usr/bin/env bash
        # HART OS Over-The-Air Update CLI
        BACKEND="http://localhost:${toString cfg.ports.backend}"

        case "''${1:-help}" in
          status)
            echo "=== HART OS Update Status ==="
            echo "Channel: ${ota.channel}"
            echo "Auto-apply: ${if ota.autoApply then "enabled" else "disabled"}"
            echo ""
            # Pipeline status from orchestrator
            curl -sf "$BACKEND/api/upgrades/status" 2>/dev/null | ${pkgs.jq}/bin/jq . || \
              echo "Backend not reachable"
            echo ""
            echo "NixOS generation:"
            nixos-version 2>/dev/null || echo "unknown"
            ;;
          check)
            echo "Checking for updates..."
            systemctl start hart-ota-check.service
            journalctl -u hart-ota-check -n 20 --no-pager
            ;;
          apply)
            echo "Applying staged update..."
            sudo nixos-rebuild switch --flake "${ota.flakeRef}#hart-${cfg.variant}"
            ;;
          rollback)
            echo "Rolling back to previous generation..."
            sudo nixos-rebuild switch --rollback
            ;;
          history)
            echo "=== Update History ==="
            ls -lt /nix/var/nix/profiles/system-*-link 2>/dev/null | head -10
            ;;
          help|--help|-h)
            echo "hart-ota — HART OS Update Manager"
            echo ""
            echo "Commands:"
            echo "  hart-ota status     Show update status + current generation"
            echo "  hart-ota check      Check for updates now"
            echo "  hart-ota apply      Apply staged update (nixos-rebuild switch)"
            echo "  hart-ota rollback   Revert to previous generation"
            echo "  hart-ota history    Show update history (NixOS generations)"
            ;;
          *)
            echo "Unknown command: $1 (try: hart-ota help)"
            exit 1
            ;;
        esac
      '')
    ];
  };
}
