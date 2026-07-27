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

  # These two must equal REPO and FLAKE_ATTR in scripts/hart_copilot_daemon.py.
  # That file states the case for constants over knobs, so they are written twice
  # rather than plumbed through an env var the daemon does not read.
  # tests/unit/test_copilot_daemon_boundary.py asserts the two stay equal.
  copilotRepo = "/var/lib/hart/copilot/HARTOS";
  copilotFlakeAttr = "hart-desktop";

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

  # `hart-copilot-tabs` — the node equivalent of the Windows 4-tab autostart
  # (schtasks -> claude_autostart_boot.ps1 -> one Terminal window, four elevated
  # tabs, one per repo). Same idea, one command, on the machine being debugged.
  #
  # Two deliberate differences from the Windows original, both forced by reality:
  #
  #   * NO PINNED SESSION IDS. Those ids (99c39a1e..., 3a9b530c...) exist only in
  #     the Windows box's ~/.claude and mean nothing here, so each tab uses
  #     --continue: resume the latest session FOR THAT REPO ON THIS MACHINE. First
  #     run starts fresh, every run after continues where the node left off, which
  #     is the behaviour that actually compounds.
  #   * ONE kitty session file instead of one launcher per tab. The Windows script
  #     needs a separate `wt` invocation per tab because `wt`'s `;` parsing
  #     silently dropped the last tab. kitty takes a declarative session file, so
  #     the whole window is described once and no tab can go missing.
  #
  # Repos are cloned on demand and a repo that will not clone is SKIPPED with a
  # message rather than killing the window, so a private repo the node has no
  # credential for costs you one tab, not the session.
  copilotTabs = pkgs.writeShellScriptBin "hart-copilot-tabs" ''
    set -uo pipefail
    WS="''${HART_WORKSPACE:-$HOME/hart}"
    mkdir -p "$WS" || { echo "cannot create $WS" >&2; exit 1; }

    # The repos these sessions care about, mirroring the Windows tabs. HARTOS is
    # the OS itself; hevolveai is the closed intelligence layer; hevolve is the web
    # product; Nunba is the desktop companion + landing page.
    REPOS="HARTOS hevolveai hevolve Nunba-HART-Companion"

    SESSION="$(mktemp -t hart-copilot-tabs.XXXXXX)"
    trap 'rm -f "$SESSION"' EXIT
    TABS=0
    for r in $REPOS; do
      d="$WS/$r"
      if [ ! -d "$d/.git" ]; then
        echo "[hart-copilot-tabs] cloning $r …"
        # PRIVATE repos (hevolveai is closed-source) cannot be cloned by anonymous
        # https. Try `gh repo clone` FIRST: gh is already installed here and, once
        # the node has run `gh auth login`, it carries the credential and clones
        # private repos exactly like public ones. Plain git is the fallback for a
        # node with no gh session, which still works for the public repos.
        if ${pkgs.gh}/bin/gh repo clone "hertz-ai/$r" "$d" -- --depth 50 2>/dev/null \
           || ${pkgs.git}/bin/git clone --depth 50 "https://github.com/hertz-ai/$r.git" "$d" 2>/dev/null; then
          :
        else
          echo "[hart-copilot-tabs] skip $r (no access; for a private repo run: gh auth login)" >&2
          continue
        fi
      fi
      # --continue, never a pinned id: see the note above.
      {
        echo "new_tab $r"
        echo "cd $d"
        echo "launch ${lib.getExe claudePkg} --continue"
      } >> "$SESSION"
      TABS=$((TABS + 1))
    done

    if [ "$TABS" -eq 0 ]; then
      echo "[hart-copilot-tabs] no repo could be prepared; nothing to open" >&2
      exit 1
    fi
    echo "[hart-copilot-tabs] opening $TABS tabs in $WS"
    exec ${pkgs.kitty}/bin/kitty --session "$SESSION"
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
        copilotTabs        # `hart-copilot-tabs` — the 4-tab workspace, on the node
      ]) ++ [
        pkgs.git           # it commits its own work (to a branch)
        pkgs.gh            # branch push / PR — the human still merges
        # `claude` -> /login is an OAuth flow: it hands the URL to the desktop's
        # URL opener. Without xdg-open on PATH that call fails and claude falls
        # back to PRINTING the URL, which on a TV-style desktop means retyping a
        # long signed URL by hand. Firefox is already preinstalled and already the
        # x-scheme-handler/https default (desktop.nix), so the only missing piece
        # was the opener itself: one package turns login into one click.
        pkgs.xdg-utils
      ];

      # Point the co-pilot at THIS node's own backend, so "debug the OS from within"
      # means driving the live local runtime (the ONE /chat pipeline + dispatch),
      # not a remote guess.
      environment.sessionVariables = {
        HART_COPILOT_BACKEND = "http://127.0.0.1:6777";
        # Belt and braces for the OAuth login. xdg-open above is the general path,
        # but tools differ in what they try first, and $BROWSER is the one every
        # one of them honours. Firefox is preinstalled and is already the
        # x-scheme-handler/https default, so this names the same browser the rest
        # of the desktop opens rather than introducing a second answer.
        BROWSER = "firefox";
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
        # systemd is here for `systemctl start --wait hart-copilot-verify`, which is
        # the daemon's only route to activating a config. There is deliberately no
        # sudo: NoNewPrivileges below would block it anyway.
        # systemd is here for `systemctl start --wait hart-copilot-verify`, the
        # daemon's only route to activating a config. There is deliberately no sudo:
        # NoNewPrivileges below would block it anyway.
        #
        # The python is the node's own interpreter, with the node's own dependencies.
        # Without it the agent had no python on PATH at all, while its prompt told it
        # to "verify with the repo's own tests before you commit" and not to claim a
        # fix it had not run. It could not run anything. Same shape as the sudo bug:
        # an instruction the environment could not carry out.
        path = [
          claudePkg pkgs.git pkgs.gh pkgs.coreutils pkgs.openssh pkgs.systemd
          config.hart.package.python
        ];
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

      # ── Verification, privilege-separated ────────────────────────────────
      # The daemon's docstring says the agent verifies OS changes with
      # `nixos-rebuild test`. It could not: the unit runs as `hart` with
      # NoNewPrivileges, `sudo` is not on its path, and no sudoers rule grants
      # it anything, so the call returned "not a NixOS host?" ON a NixOS host.
      #
      # The fix is not to loosen the daemon. It is to put activation in a root
      # unit that takes NO arguments from the daemon. `test` is written into
      # ExecStart, so an agent that ignores every instruction in its prompt
      # still cannot express `switch` or `boot`: there is no argument to pass.
      # What the machine comes up as stays a human decision because the
      # daemon has no way to say otherwise, rather than because it was asked.
      systemd.services.hart-copilot-verify = {
        description = "HART OS - co-pilot verification (nixos-rebuild test, never switch)";
        serviceConfig = {
          Type = "oneshot";
          # Absolute, and fixed. The flake ref is the daemon's own clone; the
          # verb is `test`, which activates on the running system and does not
          # touch the boot generation, so a power cycle undoes it.
          ExecStart = ''
            /run/current-system/sw/bin/nixos-rebuild test --flake ${copilotRepo}/nixos#${copilotFlakeAttr}
          '';
          TimeoutStartSec = "45min";
        };
        path = [ pkgs.nix pkgs.git pkgs.openssh pkgs.coreutils ];
      };

      # Let the daemon's user start that one unit and nothing else. Without
      # this the separation above is just a unit nobody can reach.
      security.polkit.extraConfig = ''
        polkit.addRule(function(action, subject) {
          if (action.id == "org.freedesktop.systemd1.manage-units" &&
              action.lookup("unit") == "hart-copilot-verify.service" &&
              subject.user == "hart") {
            return polkit.Result.YES;
          }
        });
      '';
    })
  ]);
}
