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

  # ── Keep the installed system's /etc/hart/src in step with what OTA applied ──
  # (task #20). hart-install freezes the repo at /etc/hart/src so the machine can
  # `nixos-rebuild` offline forever; OTA switches generations from pinned refs.
  # Without this refresh those two truths DIVERGE after the first applied update,
  # and a user's ordinary `nixos-rebuild switch` silently REVERTS the machine to
  # install-time HART. One writer for the refresh, called from BOTH apply sites
  # (the check service's autoApply branch and the `hart-ota apply` CLI verb).
  #
  # NEVER-FAIL by design: a sync miss must not fail an already-successful switch
  # — but every exit path LOGS (the no-silent-gulping rule). Image systems have
  # no /etc/hart/src and no-op; an unresolvable flake ref (offline apply of a
  # ref whose source was GC'd) keeps the previous copy and says so.
  otaSyncSrc = pkgs.writeShellApplication {
    name = "hart-ota-sync-src";
    runtimeInputs = [ pkgs.nix pkgs.jq ];
    text = ''
      FLAKE="''${1:?usage: hart-ota-sync-src <flake-ref-just-applied>}"
      if [ ! -e /etc/hart/src ]; then
        echo "[HART OTA] no /etc/hart/src (image system) — source sync skipped"
        exit 0
      fi
      SRC_PATH="$(nix flake metadata "$FLAKE" --json 2>/dev/null | jq -r '.path // empty')" || SRC_PATH=""
      if [ -z "$SRC_PATH" ] || [ ! -e "$SRC_PATH" ]; then
        echo "[HART OTA] cannot resolve source of $FLAKE — /etc/hart/src kept at previous rev"
        exit 0
      fi
      # Our refs carry ?dir=nixos, so metadata's .path is the nixos/ SUBDIR;
      # the installed copy is the REPO ROOT (the flake references ../compositor).
      ROOT="$SRC_PATH"
      if [ "$(basename "$SRC_PATH")" = "nixos" ] && [ -e "$(dirname "$SRC_PATH")/nixos/flake.nix" ]; then
        ROOT="$(dirname "$SRC_PATH")"
      fi
      rm -rf /etc/hart/src.new /etc/hart/src.old
      if ! cp -a "$ROOT" /etc/hart/src.new || ! chmod -R u+w /etc/hart/src.new; then
        echo "[HART OTA] source copy failed — /etc/hart/src kept at previous rev"
        rm -rf /etc/hart/src.new
        exit 0
      fi
      if mv /etc/hart/src /etc/hart/src.old && mv /etc/hart/src.new /etc/hart/src; then
        rm -rf /etc/hart/src.old
        echo "[HART OTA] /etc/hart/src synced to $FLAKE"
      else
        echo "[HART OTA] source swap failed — check /etc/hart/src{,.old,.new} by hand"
        exit 0
      fi
    '';
  };
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
      description = ''
        DORMANT — retained for compatibility, but NO LONGER schedules any
        poll.  The node's trigger model is: POLL central ONLY (a) on
        boot (the hart-ota-check OnBootSec timer) and (b) when the user
        runs `hart-ota check`; there is NO periodic interval poll.  A
        CENTRAL push (hart-ota-push, over the existing fleet/gossip
        fabric) covers everything in between.  This option does not
        wire OnUnitActiveSec anymore — setting it has no effect on the
        timer; it is kept only so existing configs that set it still
        evaluate.
      '';
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
      default = "github:hertz-ai/HARTOS";
      description = ''
        Nix flake reference for building/switching the system. When
        centralEndpoint returns an approved flake_ref for this channel,
        that value supersedes this one as the switch target (central can
        pin an exact commit, e.g. github:hertz-ai/HARTOS/<sha>). This
        remains the build target's repo and the offline fallback.
      '';
    };

    centralEndpoint = lib.mkOption {
      type = lib.types.str;
      default = "http://etime.hertzai.com:6777/api/ota/latest";
      description = ''
        CENTRAL authority endpoint the node polls for the approved
        {flake_ref, commit} of its channel — NOT github directly. The
        queen-bee central account decides which revision each channel is
        cleared to run; the node never auto-pulls an arbitrary upstream
        commit. The check timer GETs `''${centralEndpoint}?channel=<channel>`
        and expects JSON {flake_ref, commit, channel}. If central is
        unreachable the node falls back to polling flakeRef (so an
        air-gapped/edge node still updates), but central is the primary
        source of truth. Set to "" to disable central polling and use
        flakeRef only.
      '';
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
    # OTA Check Timer — BOOT poll ONLY (no periodic interval)
    # ─────────────────────────────────────────────────────────
    # Trigger model: the node polls CENTRAL on boot AND when the user runs
    # `hart-ota check`; it NEVER polls on a periodic interval.  Everything
    # in between a boot poll and a user-initiated check is covered by a
    # CENTRAL push (hart-ota-push) over the existing fleet/gossip fabric.
    # So this timer fires once at boot (OnBootSec) and that is the only
    # schedule — OnUnitActiveSec is intentionally absent (checkInterval is
    # dormant and no longer wires a recurring poll).  Persistent=true makes a
    # boot poll that was missed (node off at boot time) still run on next
    # start, without turning into a recurring timer.
    systemd.timers.hart-ota-check = {
      description = "HART OS OTA Boot Update Check (boot-only; no interval poll)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "5min";
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
        HART_OTA_CENTRAL_ENDPOINT = ota.centralEndpoint;
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
            # ── Resolve the approved {flake_ref, commit} for this channel ──
            # PRIMARY source = CENTRAL authority (the queen-bee account decides
            # which revision each channel is cleared to run). We poll CENTRAL,
            # not github directly. SWITCH_FLAKE defaults to the configured
            # flakeRef and is superseded by central's approved flake_ref so
            # central can pin an exact commit (github:hertz-ai/HARTOS/<sha>).
            SWITCH_FLAKE="${ota.flakeRef}"
            REMOTE_REV="check_failed"
            CENTRAL="${ota.centralEndpoint}"

            if [[ -n "$CENTRAL" ]]; then
              echo "[HART OTA] Polling CENTRAL: $CENTRAL?channel=${ota.channel}"
              CENTRAL_JSON=$(${pkgs.curl}/bin/curl -sf --max-time 15 \
                "$CENTRAL?channel=${ota.channel}" 2>/dev/null) || CENTRAL_JSON=""
              if [[ -n "$CENTRAL_JSON" ]]; then
                C_COMMIT=$(echo "$CENTRAL_JSON" | ${pkgs.jq}/bin/jq -r '.commit // ""')
                C_FLAKE=$(echo "$CENTRAL_JSON" | ${pkgs.jq}/bin/jq -r '.flake_ref // ""')
                if [[ -n "$C_COMMIT" && "$C_COMMIT" != "null" ]]; then
                  REMOTE_REV="$C_COMMIT"
                  [[ -n "$C_FLAKE" && "$C_FLAKE" != "null" ]] && SWITCH_FLAKE="$C_FLAKE"
                  echo "[HART OTA] CENTRAL approved rev=$REMOTE_REV flake=$SWITCH_FLAKE"
                fi
              fi
            fi

            # ── Fallback: poll flakeRef directly only if central gave nothing ──
            # (keeps an air-gapped / central-unreachable node updatable; central
            #  stays the primary source of truth when present).
            if [[ "$REMOTE_REV" == "check_failed" ]]; then
              echo "[HART OTA] CENTRAL unavailable, falling back to flake: ${ota.flakeRef}"
              REMOTE_REV=$(${pkgs.nix}/bin/nix flake metadata "${ota.flakeRef}" --json 2>/dev/null \
                | ${pkgs.jq}/bin/jq -r '.revision // "unknown"') || REMOTE_REV="check_failed"
            fi

            LOCAL_REV=$(${pkgs.nix}/bin/nix flake metadata /etc/nixos --json 2>/dev/null \
              | ${pkgs.jq}/bin/jq -r '.revision // "unknown"') || LOCAL_REV="unknown"

            echo "[HART OTA] Local: $LOCAL_REV"
            echo "[HART OTA] Approved: $REMOTE_REV"

            if [[ "$REMOTE_REV" != "check_failed" && "$REMOTE_REV" != "$LOCAL_REV" && "$REMOTE_REV" != "unknown" ]]; then
              echo "[HART OTA] New version available: $REMOTE_REV"

              # Persist update metadata INCLUDING the central-approved switch flake
              # so the (separate-tick) 'completed' branch switches to exactly the
              # revision central cleared, not the channel HEAD.
              ${pkgs.jq}/bin/jq -n \
                --arg rev "$REMOTE_REV" \
                --arg flake "$SWITCH_FLAKE" \
                --arg channel "${ota.channel}" \
                --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                '{revision: $rev, switch_flake: $flake, channel: $channel, discovered_at: $ts, status: "available"}' \
                > "$OTA_DIR/pending_update.json"

              # Start the 7-stage pipeline via orchestrator (SIGN/CANARY gates
              # still run locally — central only chooses WHICH commit, it never
              # skips the local sign-verify + canary safety gates).
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

            # Switch to exactly the flake CENTRAL approved at pipeline start
            # (persisted in pending_update.json). Fall back to the configured
            # flakeRef if the metadata is missing/older.
            SWITCH_FLAKE=$(${pkgs.jq}/bin/jq -r '.switch_flake // empty' \
              "$OTA_DIR/pending_update.json" 2>/dev/null || true)
            [[ -z "$SWITCH_FLAKE" ]] && SWITCH_FLAKE="${ota.flakeRef}"

            if [[ "${if ota.autoApply then "1" else "0"}" == "1" ]]; then
              echo "[HART OTA] Auto-apply enabled, switching to $SWITCH_FLAKE ..."
              # Same switch/rollback semantics as before; the if-form (vs the old
              # `|| { }`) exists ONLY so the source sync runs on SUCCESS alone —
              # a rolled-back switch must not advance /etc/hart/src (task #20).
              if sudo nixos-rebuild switch --flake "$SWITCH_FLAKE#hart-${cfg.variant}" 2>&1; then
                sudo ${otaSyncSrc}/bin/hart-ota-sync-src "$SWITCH_FLAKE" 2>&1                   || echo "[HART OTA] source sync failed unexpectedly — /etc/hart/src may be stale"
              else
                echo "[HART OTA] Switch failed, rolling back..."
                sudo nixos-rebuild switch --rollback 2>&1
              fi
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
          # The installed-source refresh (task #20) writes /etc/hart/src after a
          # successful switch; ProtectSystem=strict would otherwise leave /etc
          # read-only. "-" = ignore when absent (image systems have no /etc/hart).
          "-/etc/hart"
        ];
        PrivateTmp = true;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-ota-check";
      };
    };

    # ─────────────────────────────────────────────────────────
    # OTA Push Receiver — CENTRAL push → SAME apply (no new transport)
    # ─────────────────────────────────────────────────────────
    # The other half of the trigger model: between a boot poll and a user
    # `hart-ota check`, CENTRAL can PUSH an approved build at any time.  The
    # push rides the EXISTING fleet/gossip fabric — a signed `firmware_update`
    # FleetCommand on the MessageBus 'fleet.command' topic (core.peer_link +
    # WAMP/PeerLink/Crossbar legs).  Two legs receive it, both converging on the
    # EXACT same staged apply (pipeline → autoApply switch → auto-rollback) the
    # boot poll uses — central only chooses WHICH commit; the local SIGN/CANARY
    # gates still run (a push NEVER force-applies past canary), master key never
    # touched:
    #
    #   • REALTIME leg — lives IN hart-backend, the long-lived process that holds
    #     the WAMP session.  core.peer_link.local_subscribers (bootstrapped at
    #     backend start) subscribes to 'fleet.command' and routes OTA-class
    #     commands to ota_push_listener.handle_push, which verifies the
    #     central/regional signature and kicks hart-ota-check.  No separate
    #     subscriber process (a second process can't receive the Crossbar leg —
    #     wamp_session is process-local), so the realtime subscribe is NOT
    #     re-implemented here.
    #
    #   • DURABLE leg — THIS oneshot.  A push sent while the node was OFF is
    #     persisted as a pending FleetCommand row (offline-first fallback).  On
    #     boot this drains those via ota_push_listener.drain_pending (REUSING
    #     FleetCommandService.get_pending_commands, which re-verifies each
    #     issuer) and kicks hart-ota-check for any OTA push.  Mirrors
    #     embedded_main's boot drain — one node-side fleet-receive shape, reused.
    #
    # Runs as root (no User=) like hart-self-build-watch so it can start the
    # privileged hart-ota-check unit directly — no new sudo/polkit rule, and the
    # actual privileged switch still happens inside hart-ota-check's own
    # hardened, audited context.
    systemd.services.hart-ota-push = {
      description = "HART OS OTA Push Receiver — durable drain (central push → staged apply)";
      after = [ "network-online.target" "hart-backend.service" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        HEVOLVE_DATA_DIR = cfg.dataDir;
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        HART_OTA_CHANNEL = ota.channel;
        # The unit the push kicks — the SAME apply path the boot poll uses.
        HART_OTA_CHECK_UNIT = "hart-ota-check.service";
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "oneshot";
        WorkingDirectory = hartApp;
        # Reuse the node-side listener — drain offline-queued central OTA pushes
        # once and kick hart-ota-check for each.  No bespoke transport/pipeline
        # in the Nix string; the realtime subscribe lives in hart-backend.
        ExecStart = "${hartApp.python}/bin/python -m integrations.agent_engine.ota_push_listener --drain-only";
        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-ota-push";
      };
    };

    # Run the durable drain shortly after boot (and let the boot poll's timer
    # cover the no-push path).  A oneshot on a boot timer — NOT a recurring poll
    # (the trigger model forbids interval polling); realtime pushes are handled
    # live by hart-backend, this only sweeps what was queued while offline.
    systemd.timers.hart-ota-push = {
      description = "HART OS OTA durable push-drain (boot sweep)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "3min";
        RandomizedDelaySec = "2min";
        Persistent = true;
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
      # The installed-source refresh verb (task #20) — on PATH so operators can
      # run it by hand and the ota-central nixosTest can drive it directly.
      otaSyncSrc
      (pkgs.writeShellScriptBin "hart-ota" ''
        #!/usr/bin/env bash
        # HART OS Over-The-Air Update CLI
        BACKEND="http://localhost:${toString cfg.ports.backend}"

        case "''${1:-help}" in
          status)
            echo "=== HART OS Update Status ==="
            echo "Channel: ${ota.channel}"
            echo "Auto-apply: ${if ota.autoApply then "enabled" else "disabled"}"
            echo "Central: ${if ota.centralEndpoint != "" then ota.centralEndpoint else "(disabled — flakeRef only)"}"
            echo "Triggers: boot poll + 'hart-ota check' + central push (NO interval poll)"
            PUSH=$(systemctl is-active hart-ota-push.service 2>/dev/null || echo "unknown")
            echo "Push receiver: $PUSH"
            echo ""
            # Pipeline status from orchestrator
            curl -sf "$BACKEND/api/upgrades/status" 2>/dev/null | ${pkgs.jq}/bin/jq . || \
              echo "Backend not reachable"
            echo ""
            echo "NixOS generation:"
            nixos-version 2>/dev/null || echo "unknown"
            ;;
          check)
            # User-initiated poll — one of the two poll triggers (the other is
            # the boot timer).  Polls CENTRAL for this channel's approved build
            # and stages it through the same pipeline; there is no interval poll.
            echo "Checking CENTRAL for updates now (channel: ${ota.channel})..."
            systemctl start hart-ota-check.service
            journalctl -u hart-ota-check -n 20 --no-pager
            ;;
          apply)
            echo "Applying staged update..."
            # NOTE (pre-existing, observed 2026-07-29, deliberately unchanged
            # here): this verb applies ota.flakeRef while the check service
            # applies the persisted switch_flake from pending_update.json — a
            # central-approved pin can differ from the configured ref.
            if sudo nixos-rebuild switch --flake "${ota.flakeRef}#hart-${cfg.variant}"; then
              # Success-gated source sync (task #20) — same helper the
              # autoApply branch calls; see its definition for the semantics.
              sudo ${otaSyncSrc}/bin/hart-ota-sync-src "${ota.flakeRef}"                 || echo "[HART OTA] source sync failed unexpectedly — /etc/hart/src may be stale"
            fi
            ;;
          rollback)
            echo "Rolling back to previous generation..."
            sudo nixos-rebuild switch --rollback
            ;;
          self-build|build)
            echo "Rebuilding HART OS from current configuration..."
            sudo hart-self-build switch
            ;;
          dry-run)
            echo "Testing build without applying..."
            sudo hart-self-build dry-run
            ;;
          diff)
            echo "Showing what would change..."
            sudo hart-self-build diff
            ;;
          history)
            echo "=== Update History ==="
            ls -lt /nix/var/nix/profiles/system-*-link 2>/dev/null | head -10
            echo ""
            echo "=== Self-Build History ==="
            tail -5 /var/lib/hart/ota/history/builds.jsonl 2>/dev/null | \
              ${pkgs.jq}/bin/jq -r '"\(.timestamp) | \(.action) | \(.status)"' 2>/dev/null || \
              echo "(no self-builds yet)"
            ;;
          help|--help|-h)
            echo "hart-ota — HART OS Update Manager"
            echo ""
            echo "Commands:"
            echo "  hart-ota status       Show update status + current generation"
            echo "  hart-ota check        Poll CENTRAL for updates now (user trigger)"
            echo "  hart-ota apply        Apply staged update (nixos-rebuild switch)"
            echo "  hart-ota rollback     Revert to previous generation"
            echo "  hart-ota self-build   Rebuild OS from current config (runtime.nix)"
            echo "  hart-ota dry-run      Test build without applying"
            echo "  hart-ota diff         Show what would change"
            echo "  hart-ota history      Show update + build history"
            echo ""
            echo "Updates arrive on exactly two triggers (no periodic poll):"
            echo "  1. POLL central  — on boot, and on 'hart-ota check'"
            echo "  2. PUSH central  — at any time, via hart-ota-push over the"
            echo "                     existing fleet/gossip fabric (signed)"
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
