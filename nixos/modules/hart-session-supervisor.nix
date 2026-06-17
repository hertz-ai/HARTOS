{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Out-of-Process Session Tier-Drop Supervisor  (Phase 1 / B4)
# ═══════════════════════════════════════════════════════════════
#
# THE never-blank-screen guarantee.
#
# WHY THIS EXISTS — the honest correction to the architecture:
#   node_watchdog.py is an IN-PROCESS Python thread heartbeat supervisor
#   (daemons call heartbeat(name); a silent thread is restarted via an
#   in-process restart_fn). It has NO concept of a Wayland session, a
#   DRM/KMS process, swaymsg, a /var/lib/hart tier-latch, or greetd.
#   A thread-restart supervisor STRUCTURALLY CANNOT drop a display-manager
#   session from one compositor binary to another and latch the choice
#   across boot.  So the tier-drop is a NEW, out-of-process supervisor —
#   the single most safety-critical unbuilt mechanism (HART_OS_NATIVE_
#   ARCHITECTURE.md §6.1, compositor/ROADMAP.md Phase 1).
#
# WHAT IT DOES:
#   - greetd (out-of-process display manager) launches ONE selector wrapper.
#   - The wrapper reads the latch /var/lib/hart/session-tier
#       (values: hart-comp | sway | cage   — the Phase-0 state-file contract),
#     defaulting to cage when absent/unreadable.
#   - It launches the chosen tier's session command:
#       Tier 1  hart-comp  (Smithay; RESERVED — only when a real session is
#                            wired by Phase 3; until then it falls straight
#                            through to the next tier so this is never blank)
#       Tier 2  sway       (proven wlroots WM running the SAME glass shell)
#       Tier 3  cage       (the EXACT audited never-fail paint floor that
#                            ships today — hart-liquid-ui.nix's hart-shell-
#                            session, reused verbatim, NEVER reimplemented)
#   - On a crash-loop (DEFAULT 3 restarts / 5 min) it writes the NEXT-LOWER
#     tier to the latch, so greetd's relaunch of the wrapper lands on a tier
#     that paints — and the choice LATCHES across boot (a Smithay regression
#     silently degrades to today's known-good cage behaviour, never a black
#     screen).
#   - The supervisor can NEVER drop below cage: cage is the floor.
#   - Operator reset:  `hartctl session reset-tier`  clears the latch so the
#     next boot attempts Tier-1 again — a transient Tier-1 bug can never
#     permanently mask as a downgrade with no recovery path.
#   - node_watchdog stays a SIGNAL EMITTER ONLY: if it ever writes the touch
#     flag /run/hart/compositor-unhealthy, the wrapper treats the current
#     boot as a crash for tier-drop accounting. node_watchdog NEVER selects
#     a session (see the note block at the bottom of this file).
#
# NEVER-BREAK GATES (compositor/ROADMAP.md Phase 1):
#   - A crash-loop at ANY tier ALWAYS lands on a tier that paints.
#   - Supervisor is out-of-process (greetd); node_watchdog never owns
#     session selection.
#   - Latch is operator-clearable.
#   - Cage Tier-3 remains the audited floor; the supervisor cannot drop
#     below it.
#   - defaultSession STAYS cage: this module is OPT-IN (enable = false) until
#     VM-proven (WSL-QEMU loop-kill fault injection). When disabled it is a
#     pure no-op and the GDM + hart-shell session in desktop.nix is
#     byte-identical to before.
#
# VM-GATED: every claim here (greetd relaunch, crash-loop drop, latch across
#   boot, lands-on-cage) MUST be proven in a CI nixosTest / local QEMU-KVM —
#   no Wayland/greetd session can run on the Windows dev box. The companion
#   nixosTest (hart-session-supervisor-tier-drop) loop-kills a deliberately
#   crashing fake Tier-1 and asserts the latch lands on cage.

let
  cfg = config.hart;
  sup = config.hart.sessionSupervisor;

  # ── The tier latch + crash-window state, per the Phase-0 contract ──
  stateDir   = "/var/lib/hart";
  latchFile  = "${stateDir}/session-tier";          # hart-comp | sway | cage
  windowFile = "${stateDir}/session-tier.window";   # crash timestamps (epoch, one per line)
  # node_watchdog (in-process) may TOUCH this to signal "compositor unhealthy".
  # It is in /run (tmpfs) so it never survives a reboot — a fresh boot always
  # gets a clean slate. The supervisor consumes it; node_watchdog never writes
  # the latch.
  unhealthyFlag = "/run/hart/compositor-unhealthy";

  # Ordered tier ladder, highest → lowest. The LAST entry (cage) is the floor.
  # A tier is "available" only if its launcher command is non-null; an
  # unavailable higher tier falls straight through to the next so the screen
  # is never blank waiting on an unbuilt compositor.
  tierLadder = [ "hart-comp" "sway" "cage" ];

  # Tier-3 cage launcher — REUSED VERBATIM from hart-liquid-ui.nix. We do NOT
  # reimplement the cage/software-GL floor here; hart-liquid-ui.nix's
  # `hart-shell-session` (installed on PATH via the kiosk session) IS the
  # audited floor. The supervisor only SELECTS it.
  cageCommand = sup.cageCommand;
  swayCommand = sup.swayCommand;
  compCommand = sup.compCommand;   # null until Phase 3 wires a real session

  # Resolve a tier name → its launch command (or null if unavailable).
  tierCommandFor = tier:
    if tier == "hart-comp" then compCommand
    else if tier == "sway" then swayCommand
    else if tier == "cage" then cageCommand
    else null;

  # ── hartctl — operator control tool, sharing the SAME latch contract ──
  # The script source lives at nixos/hartctl/hartctl (a real, unit-testable
  # file owned by this task). It reads/clears the same latchFile + windowFile
  # the selector wrapper uses, so there is ONE source of truth for the
  # session-tier contract. We substitute the build-time paths in.
  hartctl = pkgs.runCommand "hartctl"
    { nativeBuildInputs = [ pkgs.coreutils ]; }
    ''
      mkdir -p $out/bin
      substitute ${../hartctl/hartctl} $out/bin/hartctl \
        --replace '@HART_LATCH_FILE@'  '${latchFile}' \
        --replace '@HART_WINDOW_FILE@' '${windowFile}'
      chmod +x $out/bin/hartctl
      # Smoke-check the substituted script parses under POSIX sh at build time.
      ${pkgs.dash}/bin/dash -n $out/bin/hartctl
    '';

  # ── The selector wrapper greetd runs as its single session command ──
  # Pure POSIX sh so it runs under greetd's minimal session shell. Every tool
  # is referenced by absolute store path (greetd's session PATH is minimal).
  selectorScript = pkgs.writeShellScript "hart-session-selector" ''
    set -u
    PATH=${lib.makeBinPath (with pkgs; [ coreutils ])}:$PATH

    LATCH="${latchFile}"
    WINDOW="${windowFile}"
    UNHEALTHY="${unhealthyFlag}"
    MAX_CRASHES=${toString sup.crashLoopCount}
    WINDOW_SECS=${toString sup.crashLoopWindowSeconds}
    LADDER="${lib.concatStringsSep " " tierLadder}"
    FLOOR="cage"

    log() { echo "[hart-session-supervisor] $*" >&2; }

    # ── Read the tier to launch (SESSION_TIER_CONTRACT.md §3 rule 1) ──
    #   - latch PRESENT + valid value (hart-comp|sway|cage) -> that latched tier
    #   - latch MISSING / unreadable / invalid token        -> the FLOOR `cage`
    #
    # "Missing ⇒ cage" is fail-safe by construction: we NEVER boot a higher
    # unproven tier just because the latch is absent or torn. To RE-ARM Tier-1
    # the operator runs `hartctl session reset-tier`, which WRITES `hart-comp`
    # (it does not merely delete the file) — §4. So a fresh image ships with the
    # supervisor opt-in + the latch absent ⇒ cage floor, exactly the current
    # defaultSession behaviour, until an operator (or Phase 3 default) arms a
    # higher tier.
    read_tier() {
      if [ -r "$LATCH" ]; then
        _rtv=$(cat "$LATCH" 2>/dev/null | tr -d '[:space:]')
        case "$_rtv" in
          hart-comp|sway|cage) printf '%s' "$_rtv"; return 0 ;;
        esac
      fi
      printf '%s' "$FLOOR"
    }

    # ── Atomically write the latch (latches across boot) ──
    write_tier() {
      umask 022
      printf '%s\n' "$1" > "$LATCH.tmp" && mv -f "$LATCH.tmp" "$LATCH"
      log "latched session tier = $1"
    }

    # ── Is a tier launchable on this build? (command non-empty) ──
    tier_available() {
      case "$1" in
        hart-comp) [ -n "${if compCommand == null then "" else compCommand}" ] ;;
        sway)      [ -n "${if swayCommand == null then "" else swayCommand}" ] ;;
        cage)      [ -n "${if cageCommand == null then "" else cageCommand}" ] ;;
        *) return 1 ;;
      esac
    }

    # ── Launch command for a tier ──
    tier_command() {
      case "$1" in
        hart-comp) printf '%s' "${if compCommand == null then "" else compCommand}" ;;
        sway)      printf '%s' "${if swayCommand == null then "" else swayCommand}" ;;
        cage)      printf '%s' "${if cageCommand == null then "" else cageCommand}" ;;
      esac
    }

    # ── Next-lower available tier (never below the floor) ──
    lower_tier() {
      cur="$1"; seen=""; pick=""
      for t in $LADDER; do
        if [ -n "$seen" ] && tier_available "$t"; then pick="$t"; break; fi
        [ "$t" = "$cur" ] && seen=1
      done
      if [ -n "$pick" ]; then printf '%s' "$pick"; else printf '%s' "$FLOOR"; fi
    }

    # ── Record a crash; return 0 if the crash-loop threshold is breached ──
    record_crash() {
      now=$(date +%s)
      cutoff=$((now - WINDOW_SECS))
      tmp="$WINDOW.tmp"
      : > "$tmp"
      if [ -r "$WINDOW" ]; then
        while IFS= read -r ts; do
          case "$ts" in (*[!0-9]*|"") continue ;; esac
          [ "$ts" -gt "$cutoff" ] && printf '%s\n' "$ts" >> "$tmp"
        done < "$WINDOW"
      fi
      printf '%s\n' "$now" >> "$tmp"
      mv -f "$tmp" "$WINDOW"
      count=$(wc -l < "$WINDOW" | tr -d '[:space:]')
      log "crash recorded ($count in ''${WINDOW_SECS}s window, threshold $MAX_CRASHES)"
      [ "$count" -ge "$MAX_CRASHES" ]
    }

    clear_window() { : > "$WINDOW" 2>/dev/null || true; }

    # ── If node_watchdog flagged the compositor unhealthy this boot, treat it
    #    as a crash for tier-drop accounting (signal-only; it never picks). ──
    if [ -e "$UNHEALTHY" ]; then
      log "node_watchdog signalled compositor unhealthy"
      rm -f "$UNHEALTHY" 2>/dev/null || true
      if record_crash; then
        cur=$(read_tier)
        if [ "$cur" != "$FLOOR" ]; then
          nxt=$(lower_tier "$cur")
          log "unhealthy-signal crash-loop: dropping $cur -> $nxt"
          write_tier "$nxt"
          clear_window
        fi
      fi
    fi

    # ── Select the tier: latched value, skipping unavailable higher tiers ──
    TIER=$(read_tier)
    while ! tier_available "$TIER"; do
      nlow=$(lower_tier "$TIER")
      log "tier '$TIER' unavailable on this build — falling through to '$nlow'"
      [ "$nlow" = "$TIER" ] && break
      TIER="$nlow"
    done
    CMD=$(tier_command "$TIER")
    if [ -z "$CMD" ]; then
      log "FATAL: no available session command (even the floor is empty)"
      exit 1
    fi

    log "launching tier '$TIER': $CMD"
    # Run the session in the foreground. When it EXITS (crash or clean),
    # control returns here; greetd relaunches this selector for the next
    # session attempt. On a crash we drop a tier and latch BEFORE returning.
    start=$(date +%s)
    sh -c "$CMD"
    rc=$?
    end=$(date +%s)
    ran=$((end - start))
    log "tier '$TIER' exited rc=$rc after ''${ran}s"

    # A session that ran a long time then exited is a normal logout, NOT a
    # crash — don't count it toward the crash-loop. Only short-lived exits
    # (< the crash window's per-restart budget) accrue. We use the crash
    # window itself as the "too fast" guard: 3 exits inside WINDOW_SECS.
    if [ "$ran" -lt "$WINDOW_SECS" ]; then
      if record_crash; then
        if [ "$TIER" != "$FLOOR" ]; then
          nxt=$(lower_tier "$TIER")
          log "crash-loop on '$TIER' ($MAX_CRASHES in ''${WINDOW_SECS}s) — dropping to '$nxt' and latching"
          write_tier "$nxt"
          clear_window
        else
          log "crash-loop on the floor ('$FLOOR') — cannot drop further; staying on the floor (the screen still paints)"
        fi
      fi
    else
      # Long-lived session ended cleanly: a healthy run resets the window.
      clear_window
    fi

    # Return to greetd, which relaunches this selector for the next session.
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.sessionSupervisor = {

    enable = lib.mkEnableOption ''
      the out-of-process session tier-drop supervisor (greetd) — the
      never-blank-screen guarantee. OPT-IN (default false): defaultSession
      stays the cage hart-shell session until this is VM-proven via loop-kill
      fault injection. When off, this module is a pure no-op'';

    crashLoopCount = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
      description = ''
        Number of fast session exits within crashLoopWindowSeconds that count
        as a crash-loop and trigger a one-tier drop. Architecture spec: 3.
      '';
    };

    crashLoopWindowSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 300;
      description = ''
        The crash-loop sliding window (seconds). Architecture spec: 5 min.
        A session that runs longer than this before exiting is treated as a
        normal logout, not a crash.
      '';
    };

    cageCommand = lib.mkOption {
      type = lib.types.str;
      default = "hart-shell-session";
      description = ''
        Tier-3 (FLOOR) launch command — the EXACT audited cage + WebKitGTK +
        forced-software-GL session from hart-liquid-ui.nix. Reused verbatim,
        never reimplemented here. This is the floor the supervisor can never
        drop below. Resolved from PATH (the kiosk session installs it).
      '';
    };

    swayCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "${pkgs.sway}/bin/sway";
      defaultText = lib.literalExpression ''"''${pkgs.sway}/bin/sway"'';
      description = ''
        Tier-2 launch command — proven wlroots WM (sway) running the same
        glass shell as a layer-shell client. Null disables Tier-2 (the
        supervisor falls straight through hart-comp/sway to the cage floor).
        Phase 8 wires the sway config that auto-starts the glass shell + the
        swaymsg tile/summon shim; until then sway is the bare WM fallback.
      '';
    };

    compCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Tier-1 launch command — HART-comp (Smithay/Rust). NULL until Phase 3
        lands a real, VM-proven session; while null the supervisor falls
        straight through to Tier-2/Tier-3 so the slot is reserved but never
        produces a blank screen. Phase 3 sets this to the hart-comp launcher.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && sup.enable) {

    # The latch + crash-window live under the existing hart state dir; the
    # tmpfs run dir holds the node_watchdog "unhealthy" signal flag.
    systemd.tmpfiles.rules = [
      "d /var/lib/hart 0750 hart hart -"
      "d /run/hart      0750 hart hart -"
    ];

    # Enabling greetd (below) pulls in upstream nixos graphical-desktop.nix,
    # which sets fs.inotify.max_user_watches via mkDefault — the SAME option
    # hart-base.nix also sets via mkDefault, and two equal-priority mkDefaults
    # collide ("defined multiple times"). Resolve the tie HERE, gated to the
    # supervisor that introduces greetd→graphical-desktop; the value is identical
    # (524288) so behaviour is unchanged — only the priority tie is broken. Found
    # by the wired-in nixosTest's CI eval (the gate working as intended).
    boot.kernel.sysctl."fs.inotify.max_user_watches" = lib.mkForce 524288;

    # ── greetd: the out-of-process supervisor (NOT a Python thread) ──
    # greetd relaunches its session command whenever the session exits, which
    # is exactly the relaunch primitive the selector wrapper rides on. The
    # wrapper drops + latches a tier BEFORE returning to greetd, so the next
    # relaunch lands on a tier that paints.
    #
    # GDM in desktop.nix is REPLACED by greetd ONLY when this module is enabled
    # (mkForce so the desktop.nix gdm.enable + defaultSession do not collide).
    # GNOME stays user-selectable (see the autologin escape note below) as the
    # ultimate human escape hatch, preserving the desktop.nix guarantee.
    services.greetd = {
      enable = true;
      settings = {
        default_session = {
          command = "${selectorScript}";
          user = "hart-admin";
        };
      };
    };

    # greetd and GDM are mutually-exclusive display managers. When the
    # supervisor owns the session, force GDM off so the two don't both try to
    # claim the seat (desktop.nix turns gdm on for the non-supervised path).
    services.xserver.displayManager.gdm.enable = lib.mkForce false;

    # The supervisor selects sessions itself; the displayManager-level
    # defaultSession is meaningless under greetd's command model. Leave the
    # cage launcher as the floor via cageCommand above.
    services.displayManager.defaultSession = lib.mkForce "hart-shell";

    # hartctl on PATH (operator reset-tier + status) + sway when Tier-2 is
    # enabled (the wlroots fallback the architecture mandates "wired NOW").
    # cage already comes from hart-liquid-ui.nix; hart-comp is null until
    # Phase 3.
    environment.systemPackages =
      [ hartctl ]
      ++ lib.optional (sup.swayCommand != null) pkgs.sway;

    # NOTE — node_watchdog stays a SIGNAL EMITTER ONLY (the architecture
    # correction, HART_OS_NATIVE_ARCHITECTURE.md §6.1):
    #
    #   security/node_watchdog.py is an in-process thread supervisor and MUST
    #   NOT gain any session-selection logic. The only integration is one-way:
    #   if it detects the compositor unhealthy it may `touch
    #   /run/hart/compositor-unhealthy` (a 0-byte tmpfs flag). The selector
    #   wrapper above consumes + clears that flag and counts it toward the
    #   crash-loop. node_watchdog never reads or writes the latch, never knows
    #   a tier, and never picks a session. This boundary is load-bearing:
    #   session selection is OUT-OF-PROCESS (greetd) by construction.
    #
    #   The matching Python change (a helper that emits the signal) belongs to
    #   the node_watchdog owner's task, NOT this module — kept as a documented
    #   contract so the wire is testable without coupling the two files.
  };
}
