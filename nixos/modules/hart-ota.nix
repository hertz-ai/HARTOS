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
  # install-time HART. One writer for the refresh, called from the ONE apply
  # site (the root hart-ota-apply engine, success-gated after its switch).
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

  # ── Privileged apply: systemd-native boundary, NO sudo (task #22) ──
  # VERIFIED BROKEN 2026-07-29: hart-ota-check and hart-ota-canary run as
  # User=hart with NoNewPrivileges=true, and NNP makes the kernel ignore
  # sudo's setuid bit — `sudo nixos-rebuild` inside those units failed
  # unconditionally, so autoApply could never switch and the canary's
  # NixOS-level rollback was a silent no-op (its `|| true` hid it).  The
  # fix keeps the unprivileged pipeline exactly as hardened as before and
  # moves ONLY the generation change behind a root path-unit boundary:
  #
  #   unprivileged unit ──writes──▶ /var/lib/hart/ota/apply-request.json
  #   hart-ota-apply.path (root) ──PathExists──▶ hart-ota-apply.service (root)
  #     └─ consumes the request, nixos-rebuild switch/rollback, success-gated
  #        source sync, result → /var/lib/hart/ota/last_apply.json
  #
  # Trust model UNCHANGED: hart-written OTA state (pending_update.json)
  # already drove the intended sudo switch — the pipeline's SIGN/CANARY
  # gates remain the authorization; the request only names WHICH staged
  # flake to apply.  Follow-up hardening (tracked, not done here): the
  # apply unit independently re-verifying the central signature over the
  # pinned commit before switching.
  otaRequestApply = pkgs.writeShellApplication {
    name = "hart-ota-request-apply";
    runtimeInputs = [ pkgs.jq pkgs.coreutils ];
    text = ''
      # The ONE writer for apply requests (check autoApply, canary rollback,
      # CLI verbs).  usage: hart-ota-request-apply switch|rollback [flake-ref]
      KIND="''${1:?usage: hart-ota-request-apply switch|rollback [flake-ref]}"
      FLAKE="''${2:-}"
      OTA_DIR="/var/lib/hart/ota"
      case "$KIND" in
        switch)
          if [ -z "$FLAKE" ]; then
            # One resolution home: the central-approved pin in
            # pending_update.json supersedes the configured channel ref
            # (closes the 2026-07-29 CLI-applies-flakeRef inconsistency).
            FLAKE="$(jq -r '.switch_flake // empty' "$OTA_DIR/pending_update.json" 2>/dev/null || true)"
            if [ -z "$FLAKE" ]; then
              FLAKE="${ota.flakeRef}"
            fi
          fi
          ;;
        rollback) ;;
        *) echo "[HART OTA] unknown request kind: $KIND" >&2; exit 2 ;;
      esac
      TMP="$OTA_DIR/.apply-request.json.tmp"
      jq -n --arg kind "$KIND" --arg flake "$FLAKE" \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{kind: $kind, flake: $flake, requested_at: $ts}' > "$TMP"
      mv -f "$TMP" "$OTA_DIR/apply-request.json"
      echo "[HART OTA] $KIND requested (flake: ''${FLAKE:-n/a}) — the root hart-ota-apply unit takes it from here; follow with: journalctl -u hart-ota-apply -f"
    '';
  };

  otaApplyRun = pkgs.writeShellApplication {
    name = "hart-ota-apply-run";
    # nixos-rebuild comes from the UNIT's path (not runtimeInputs) on purpose:
    # runtimeInputs would pin the store path inside the script, making the
    # binary unshadowable — the ota-central nixosTest observes the switch argv
    # through a unit-path recording stub, exactly as it did for the old shape.
    runtimeInputs = [ pkgs.jq pkgs.coreutils ];
    text = ''
      # Root engine behind hart-ota-apply.path — the ONLY privileged step in
      # the OTA story.  Everything else (poll, pipeline, canary watch) stays
      # User=hart + NoNewPrivileges.
      OTA_DIR="/var/lib/hart/ota"
      REQ="$OTA_DIR/apply-request.json"
      if [ ! -e "$REQ" ]; then
        echo "[HART OTA apply] no request file — nothing to do"
        exit 0
      fi
      KIND="$(jq -r '.kind // empty' "$REQ" 2>/dev/null || true)"
      FLAKE="$(jq -r '.flake // empty' "$REQ" 2>/dev/null || true)"
      REQUESTED_AT="$(jq -r '.requested_at // empty' "$REQ" 2>/dev/null || true)"
      # Consume BEFORE acting: a malformed or finished request must never
      # retrigger the path unit into a loop (PathExists re-fires as long as
      # the file exists after the service exits).
      rm -f "$REQ"

      STATUS="invalid_request"
      case "$KIND" in
        switch)
          if [ -z "$FLAKE" ]; then
            echo "[HART OTA apply] switch request without a flake ref — ignored"
          else
            ${lib.optionalString (ota.preUpdateHook != "") ''
              echo "[HART OTA apply] running pre-update hook..."
              ${ota.preUpdateHook}
            ''}
            echo "[HART OTA apply] switching to $FLAKE#hart-${cfg.variant} ..."
            # if-form on purpose: the source sync runs on SUCCESS alone — a
            # rolled-back switch must not advance /etc/hart/src (task #20).
            if nixos-rebuild switch --flake "$FLAKE#hart-${cfg.variant}"; then
              STATUS="applied"
              ${otaSyncSrc}/bin/hart-ota-sync-src "$FLAKE" \
                || echo "[HART OTA apply] source sync failed unexpectedly — /etc/hart/src may be stale"
            else
              echo "[HART OTA apply] switch FAILED — rolling back..."
              STATUS="rolled_back"
              nixos-rebuild switch --rollback \
                || { STATUS="rollback_failed"; echo "[HART OTA apply] ROLLBACK FAILED — manual intervention needed"; }
            fi
            ${lib.optionalString (ota.postUpdateHook != "") ''
              echo "[HART OTA apply] running post-update hook..."
              ${ota.postUpdateHook}
            ''}
          fi
          ;;
        rollback)
          echo "[HART OTA apply] rollback requested — switching to previous generation..."
          if nixos-rebuild switch --rollback; then
            STATUS="rolled_back"
          else
            STATUS="rollback_failed"
            echo "[HART OTA apply] ROLLBACK FAILED — manual intervention needed"
          fi
          ;;
        *)
          echo "[HART OTA apply] unknown request kind: ''${KIND:-<empty>} — ignored"
          ;;
      esac

      # Result surface for `hart-ota status` + the pipeline (hart-owned dir,
      # root-written file must stay readable by the hart-side readers).
      TMP="$OTA_DIR/.last_apply.json.tmp"
      jq -n --arg kind "$KIND" --arg flake "$FLAKE" --arg status "$STATUS" \
        --arg requested_at "$REQUESTED_AT" \
        --arg finished_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{kind: $kind, flake: $flake, status: $status,
          requested_at: $requested_at, finished_at: $finished_at}' > "$TMP"
      chmod 0644 "$TMP"
      mv -f "$TMP" "$OTA_DIR/last_apply.json"
      echo "[HART OTA apply] done: $STATUS"
    '';
  };

  # ── The ONE orchestrator driver (writeText + exec — the no-heredoc rule) ──
  # DIAGNOSED from the first surviving shard run (2026-07-30, job 90691374922):
  # every inline `python -c "` block in this module hit the documented Nix
  # indentation-collapse class — the body's lines keep their leading spaces
  # inside the shell string, so python dies `IndentationError: unexpected
  # indent` on line 2.  The damage was MASKED at three of five sites by
  # `2>/dev/null ||` fallbacks (a silent-gulp violation): the stage query
  # always "returned" idle, advance never advanced, and the canary health
  # check silently exited 0 — the canary net never checked health, ever.
  # One column-0 script (the same writeText+exec pattern ota-central's mock
  # uses for exactly this reason), one verb per pipeline action.
  otaOrchestratorDrive = pkgs.writeText "hart-ota-orchestrator-drive.py" ''
    import json
    import os
    import sys

    sys.path.insert(0, "${hartApp}")
    os.environ.setdefault("HEVOLVE_DB_PATH", "${cfg.dataDir}/hevolve_database.db")

    from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator

    verb = sys.argv[1] if len(sys.argv) > 1 else "status"
    orch = UpgradeOrchestrator()
    if verb == "status":
        print(json.dumps(orch.get_status()))
    elif verb == "stage":
        print("[HART OTA] Pipeline started: %s" % (orch.start_upgrade(sys.argv[2], sys.argv[2]),))
    elif verb == "advance":
        print("[HART OTA] Advanced: %s" % (orch.advance_pipeline(),))
    elif verb == "canary-health":
        print(json.dumps(orch.check_canary_health_status()))
    elif verb == "canary-rollback":
        orch.rollback("canary_health_failed")
    else:
        raise SystemExit("unknown verb: %s" % verb)
  '';
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
          # Fallback still floors to idle (an orchestrator error must not kill
          # the boot poll) but LOGS the failure — no more silent 2>/dev/null
          # gulp, which hid the IndentationError that kept this query dead.
          RESULT=$(${hartApp.python}/bin/python ${otaOrchestratorDrive} status) \
            || { echo "[HART OTA] orchestrator status query FAILED — treating as idle"; RESULT='{"stage":"idle"}'; }

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
              ${hartApp.python}/bin/python ${otaOrchestratorDrive} stage "$REMOTE_REV" \
                || echo "[HART OTA] Pipeline start failed"
            elif [[ "$REMOTE_REV" == "check_failed" || "$REMOTE_REV" == "unknown" ]]; then
              # DO NOT CALL A FAILED CHECK "up to date" (real HW 2026-08-16).
              # Central was unreachable AND the flake fallback could not resolve
              # a revision, so this node has NO IDEA whether it is current -- it
              # printed "System is up to date" anyway, which is the same
              # silent-success lie the canary health check told before
              # (see the VERIFIED BROKEN note at the top of this module). A node
              # that cannot check must SAY so, at a severity an operator sees,
              # or an un-updatable fleet looks like a healthy one forever.
              echo "[HART OTA] UPDATE CHECK FAILED - update state UNKNOWN" >&2
              echo "[HART OTA]   central: ${ota.centralEndpoint} (unreachable?)" >&2
              echo "[HART OTA]   flake:   ${ota.flakeRef} (revision unresolved)" >&2
              echo "[HART OTA] This node is NOT known to be current; it is unchecked." >&2
              exit 1
            elif [[ "$LOCAL_REV" == "unknown" ]]; then
              # Remote resolved but LOCAL did not: /etc/nixos is not a flake we
              # can read a revision from (e.g. a dd'd image whose source tree is
              # not a git checkout). Comparing against "unknown" silently means
              # "equal" -> "up to date" forever. Say what is actually true.
              echo "[HART OTA] LOCAL revision unknown (/etc/nixos is not a readable flake)" >&2
              echo "[HART OTA] Cannot compare against approved $REMOTE_REV; node is unchecked." >&2
              exit 1
            else
              echo "[HART OTA] System is up to date (local $LOCAL_REV == approved $REMOTE_REV)"
            fi
          elif [[ "$STAGE" == "completed" ]]; then
            echo "[HART OTA] Update completed."
            if [[ "${if ota.autoApply then "1" else "0"}" == "1" ]]; then
              # This unit is User=hart + NoNewPrivileges — it CANNOT switch
              # generations itself (NNP makes the kernel ignore sudo's setuid
              # bit; the old `sudo nixos-rebuild` here failed on every run,
              # task #22).  The request writer resolves the central-approved
              # switch_flake from pending_update.json and the root
              # hart-ota-apply unit performs the switch + rollback + source
              # sync + pre/post hooks.
              echo "[HART OTA] Auto-apply enabled — requesting privileged switch"
              ${otaRequestApply}/bin/hart-ota-request-apply switch \
                || echo "[HART OTA] apply request failed — update stays staged"
            else
              echo "[HART OTA] Update staged. Run 'hart-ota apply' to switch."
            fi
          else
            echo "[HART OTA] Pipeline in progress ($STAGE), advancing..."
            ${hartApp.python}/bin/python ${otaOrchestratorDrive} advance \
              || echo "[HART OTA] Advance failed"
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
    # OTA Privileged Apply — root path-unit boundary (task #22)
    # ─────────────────────────────────────────────────────────
    # The ONLY privileged step in the OTA story.  Unprivileged units and the
    # CLI write /var/lib/hart/ota/apply-request.json via hart-ota-request-apply
    # (the one writer); this pair consumes it.  See the otaApplyRun comment in
    # the let-block for the full trust-model rationale.
    systemd.paths.hart-ota-apply = {
      description = "HART OS OTA apply-request watcher";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathExists = "/var/lib/hart/ota/apply-request.json";
        Unit = "hart-ota-apply.service";
      };
    };

    systemd.services.hart-ota-apply = {
      description = "HART OS OTA Privileged Apply (generation switch/rollback)";
      # Root on purpose (no User=): nixos-rebuild switch changes the system
      # generation.  The engine consumes the request file FIRST so the path
      # unit cannot retrigger into a loop.
      path = [ pkgs.nixos-rebuild ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${otaApplyRun}/bin/hart-ota-apply-run";
        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-ota-apply";
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
    # hart-ota-check unit directly — no new sudo/polkit rule.  hart-ota-check
    # itself is UNPRIVILEGED (User=hart + NoNewPrivileges); the privileged
    # switch happens in the root hart-ota-apply unit it requests (task #22).
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

          # Check if canary stage is active.  The old inline python here died
          # on the Nix indentation collapse and the `2>/dev/null || exit 0`
          # SILENTLY swallowed it — the canary health monitor never actually
          # checked health.  Failure still exits 0 (the monitor must never
          # crash-loop the timer) but now LOGS.
          RESULT=$(${hartApp.python}/bin/python ${otaOrchestratorDrive} canary-health) \
            || { echo "[HART OTA] canary health query FAILED — skipping this tick"; exit 0; }

          IS_CANARY=$(echo "$RESULT" | ${pkgs.jq}/bin/jq -r '.is_canary // false')
          HEALTHY=$(echo "$RESULT" | ${pkgs.jq}/bin/jq -r '.healthy // true')

          if [[ "$IS_CANARY" != "true" ]]; then
            exit 0
          fi

          if [[ "$HEALTHY" != "true" ]]; then
            echo "[HART OTA] Canary UNHEALTHY — triggering rollback"

            ${hartApp.python}/bin/python ${otaOrchestratorDrive} canary-rollback \
              || echo "[HART OTA] orchestrator-level rollback failed (NixOS-level rollback still requested below)"

            # NixOS-level rollback via the root apply unit — this unit is
            # User=hart + NoNewPrivileges, so the old `sudo nixos-rebuild
            # switch --rollback || true` here was a SILENT NO-OP (NNP blocks
            # sudo's setuid; the || true hid the failure): the canary safety
            # net never actually reverted the generation (task #22).
            ${otaRequestApply}/bin/hart-ota-request-apply rollback \
              || echo "[HART OTA] rollback request failed — generation NOT reverted"
          else
            echo "[HART OTA] Canary healthy"
          fi
        '';

        NoNewPrivileges = true;
        ProtectSystem = "strict";
        # /var/lib/hart/ota: the rollback request file lives there.
        ReadWritePaths = [ cfg.dataDir cfg.logDir "/var/lib/hart/ota" ];
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
        # Was 30s. On a USB2 flash root that is 250+ wakeups an hour for a health
        # probe whose result changes on the scale of minutes, and it competes for
        # I/O with the compositor's page flips (the 11:00 freeze mechanism).
        OnUnitActiveSec = "5min";
        AccuracySec = "30s";
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
            if [ -e /var/lib/hart/ota/last_apply.json ]; then
              echo ""
              echo "Last privileged apply:"
              ${pkgs.jq}/bin/jq . /var/lib/hart/ota/last_apply.json 2>/dev/null || true
            fi
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
            # ONE apply path (task #22): the request writer resolves the
            # central-approved switch_flake from pending_update.json exactly
            # like autoApply does (closing the 2026-07-29 flakeRef-vs-
            # switch_flake inconsistency); the root hart-ota-apply unit does
            # the switch + rollback-on-failure + success-gated source sync.
            # sudo here is fine — an interactive shell has no NoNewPrivileges.
            sudo ${otaRequestApply}/bin/hart-ota-request-apply switch
            ;;
          rollback)
            echo "Rolling back to previous generation..."
            sudo ${otaRequestApply}/bin/hart-ota-request-apply rollback
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
