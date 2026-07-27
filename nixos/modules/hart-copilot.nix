# HART OS — Claude Code as the resident co-pilot, in the node's own terminal.
#
# The steward's intent (2026-07-26): Claude Code should live INSIDE HART OS as the
# co-pilot that debugs and bootstraps the OS from within, working the seeded goals
# as its own, through the guardrails HART OS already has.
#
# TRUST BOUNDARY (why this is safe to run unattended) — the steward's framing:
# "trust is a boundary which makes it human-like... where important, it doesn't
# change the outcome". So the co-pilot has FULL autonomy inside the work and ZERO
# authority at the boundaries:
#   * inside the work  : read, edit, run tests, claim its shard of the 71 seeded
#                        goals, drive them through the ONE pipeline (POST /chat +
#                        dispatch_goal) so the constitution applies to an
#                        AI-initiated fix exactly as to a human one;
#   * at the boundary  : it COMMITS TO A BRANCH, never main. Merge, OTA publish and
#                        master-key signing stay human/democratic. Worst case is a
#                        branch nobody merges.
# Master-key signing is AI-EXCLUDED by construction (security/master_key.py); this
# module neither needs nor touches it.
#
# NO API KEY IS BAKED INTO THE IMAGE. The steward authenticates interactively on
# the node (`claude` -> /login, OAuth), and the credential lands in the user's home.
# NOTE: on the live ISO the home is tmpfs, so that login does NOT survive a reboot —
# persistence needs the INSTALLED (writable-root) image (the raw-desktop work).
#
# Packaging: claude-code is not in the pinned 24.11 nixpkgs, but it IS in 25.05
# (v1.0.85, mainProgram "claude"), and this flake ALREADY threads that input through
# for Rust (hartRustNixpkgs) — reuse it rather than adding a third nixpkgs. Its
# license is unfree; the flake already sets allowUnfree.
#
# DEFAULT OFF: a normal build is byte-identical and carries none of this closure.
{ config, lib, pkgs, hartRustNixpkgs ? null, ... }:

let
  cfg = config.hart.copilot;

  # 25.05 pkgs — same instantiation pattern as hart-comp.nix's rust_1_88, so there
  # is ONE way this repo reaches the newer nixpkgs. Falls back to `pkgs` off-flake
  # (where the input is absent) so plain module eval never crashes.
  newPkgs =
    if hartRustNixpkgs != null
    then import hartRustNixpkgs {
      inherit (pkgs.stdenv.hostPlatform) system;
      config = pkgs.config;
    }
    else pkgs;

  # Guarded: if a future nixpkgs drops/renames the attr, eval must not explode.
  claudePkg = newPkgs.claude-code or null;

  # `hart-copilot` — the one command that opens the co-pilot on a WRITABLE checkout
  # with the trust boundary stated up front. The nix store is read-only, so the
  # co-pilot cannot edit the running system's source in place; it works a real git
  # clone in the user's home and its output ships back the normal way (branch ->
  # human merge -> OTA), never by mutating /nix/store.
  copilotLauncher = pkgs.writeShellScriptBin "hart-copilot" ''
    set -uo pipefail
    REPO="''${HART_COPILOT_REPO:-$HOME/HARTOS}"
    ORIGIN="''${HART_COPILOT_ORIGIN:-https://github.com/hertz-ai/HARTOS.git}"

    echo "=== HART OS co-pilot (Claude Code) ==="
    echo "  work tree : $REPO"
    echo "  boundary  : full autonomy INSIDE the work; commits go to a BRANCH."
    echo "              merge / OTA publish / master-key signing stay HUMAN."
    echo

    if [ ! -d "$REPO/.git" ]; then
      echo "[hart-copilot] cloning HARTOS into $REPO (first run)…"
      ${pkgs.git}/bin/git clone --depth 50 "$ORIGIN" "$REPO" || {
        echo "[hart-copilot] clone failed — check network, or set HART_COPILOT_ORIGIN" >&2
        exit 1
      }
    else
      echo "[hart-copilot] updating $REPO…"
      ${pkgs.git}/bin/git -C "$REPO" fetch --depth 50 origin || \
        echo "[hart-copilot] fetch failed (working offline with what is on disk)" >&2
    fi

    # Never leave the co-pilot sitting ON main: a fresh working branch makes the
    # "propose, don't dispose" boundary structural rather than a convention.
    BRANCH="''${HART_COPILOT_BRANCH:-copilot/$(${pkgs.coreutils}/bin/date +%Y%m%d-%H%M%S)}"
    ${pkgs.git}/bin/git -C "$REPO" checkout -b "$BRANCH" 2>/dev/null || \
      ${pkgs.git}/bin/git -C "$REPO" checkout "$BRANCH" 2>/dev/null || true
    echo "[hart-copilot] branch: $(${pkgs.git}/bin/git -C "$REPO" rev-parse --abbrev-ref HEAD)"
    echo

    cd "$REPO" || exit 1
    exec ${lib.getExe claudePkg} "$@"
  '';
in
{
  options.hart.copilot = {
    enable = lib.mkEnableOption ''
      Claude Code as the resident HART OS co-pilot, available in the node's own
      terminal as `claude` (and `hart-copilot`, which opens it on a writable
      checkout, on a fresh branch, with the trust boundary printed).

      Full autonomy inside the work; commits land on a BRANCH. Merge, OTA publish
      and master-key signing remain human/democratic. No API key is baked into the
      image — authenticate interactively with `claude` on first use.

      DEFAULT OFF: a normal build carries none of this closure
    '';

    daemon.enable = lib.mkEnableOption ''
      run the co-pilot as a RESIDENT daemon (hart-copilot-daemon.service) instead of
      only an interactive command.

      It does not add a second work loop: `coding_daemon` already ticks, gates and
      dispatches. This keeps a Claude Code session resident and hands it bounded
      work, so the executor is a real coding agent rather than one /chat turn.

      Boundary, enforced in order every tick: stop-file -> hive circuit breaker ->
      should_yield_to_user() -> rate limit -> assigned task or idle. Work happens on
      a copilot/* branch in a writable clone; merge, OTA and master-key signing stay
      human. Resource-capped so it cannot starve the node it is fixing.

      Requires an authenticated Claude (run `claude` once and /login). DEFAULT OFF:
      an unattended agent is opt-in, never something a user gets by surprise
    '';
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    # Hard failure beats a silently co-pilot-less image: if the attr vanished from
    # the newer nixpkgs, say so at eval time instead of shipping a broken promise.
    {
      assertions = [{
        assertion = claudePkg != null;
        message = ''
          hart.copilot.enable = true, but `claude-code` is not present in the
          newer nixpkgs input (hartRustNixpkgs). Either that input is missing
          (off-flake eval) or the attribute was renamed upstream.
        '';
      }];
    }
    {
      # lib.optionals on claudePkg: with the attr absent, `lib.getExe null` inside
      # copilotLauncher would crash EVAL before the assertion above could print its
      # explanation. Guarding here keeps the failure mode a readable assertion.
      environment.systemPackages = (lib.optionals (claudePkg != null) [
        claudePkg          # `claude` — the co-pilot itself, in the terminal
        copilotLauncher    # `hart-copilot` — opens it bounded + on a branch
      ]) ++ [
        pkgs.git           # it commits its own work (to a branch)
        pkgs.gh            # branch push / PR — the human still merges
      ];

      # Point the co-pilot at THIS node's own backend, so "debug the OS from within"
      # means driving the live local runtime (the ONE /chat pipeline + dispatch),
      # not a remote guess.
      environment.sessionVariables = {
        HART_COPILOT_BACKEND = "http://127.0.0.1:6777";
      };
    }

    # ── Resident daemon (opt-in, hart.copilot.daemon.enable) ──────────────────
    # NOT a second work loop: coding_daemon already ticks/gates/dispatches. This
    # keeps a Claude Code session resident and hands it bounded work, so the
    # executor is a real coding agent rather than one /chat turn.
    #
    # WorkingDirectory is the hart-app store path so the daemon's gate imports
    # (security.hive_guardrails, integrations.agent_engine.dispatch) resolve against
    # the SAME code the node is running. Claude itself runs in the WRITABLE clone
    # (HART_COPILOT_REPO), never in the read-only store.
    (lib.mkIf (cfg.daemon.enable && claudePkg != null) {
      systemd.services.hart-copilot-daemon = {
        description = "HART OS - resident co-pilot (bounded Claude Code worker)";
        # The gates it consults live behind the backend; start after it, and do not
        # sit on the boot-critical path.
        after = [ "hart-backend.service" "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];
        path = [ claudePkg pkgs.git pkgs.gh pkgs.coreutils pkgs.openssh ];
        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";
          WorkingDirectory = config.hart.package;
          Environment = [
            "HART_OS_MODE=1"
            "HART_COPILOT_BACKEND=http://127.0.0.1:6777"
            "HART_COPILOT_REPO=/var/lib/hart/copilot/HARTOS"
            "HART_COPILOT_STOP=/run/hart/copilot-stop"
          ];
          ExecStart = "${config.hart.package.python}/bin/python scripts/hart_copilot_daemon.py";
          # A crash must not take the node's co-pilot down permanently, but it must
          # not hot-loop either: back off hard between restarts.
          Restart = "on-failure";
          RestartSec = 60;
          # It shares an 8GB node with the OS it is fixing. Hard caps + the lowest
          # scheduling priority mean a wedged agent degrades itself, never the
          # desktop. (The daemon also yields to the user in software every tick.)
          MemoryMax = "2G";
          CPUWeight = 5;
          Nice = 19;
          IOWeight = 10;
          # It edits a git clone under its own state dir and talks to localhost.
          # It has no business anywhere else on the system.
          StateDirectory = "hart/copilot";
          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          ReadWritePaths = [ "/var/lib/hart/copilot" "/run/hart" ];
        };
      };
    })
  ]);
}
