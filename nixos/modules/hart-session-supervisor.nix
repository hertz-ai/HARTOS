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
#       (values: hart-comp | sway | cage   — the Phase-0 state-file contract).
#     When the latch is ABSENT/unreadable (a fresh boot), it seeds the START
#     tier (the `startTier` option, default `hart-comp` = the head of the
#     ladder) so the boot tries the BEST tier first and only DEGRADES on a real
#     failure. (Set startTier=cage to restore the old fail-safe-to-floor start.)
#   - It launches the chosen tier's session command — best tier first, falling
#     back on failure (crash OR shell-paint timeout):
#       Tier 1  hart-comp  (Smithay/Rust, --backend drm; the START tier — runs
#                            the SAME GTK4 layer-shell glass host as sway; an
#                            unavailable/crashing/hung Tier-1 drops to Tier-2)
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
#     below it (the watchdog/crash-loop never latches below `cage`).
#   - The module is OPT-IN (enable = false default). When DISABLED it is a pure
#     no-op and the GDM + hart-shell session in desktop.nix is byte-identical to
#     before. When ENABLED the supervisor OWNS session selection (greetd replaces
#     GDM) and drives the ladder — starting at `startTier` (default Tier-1) and
#     degrading to the cage floor on failure. The displayManager-level
#     defaultSession is meaningless under greetd's command model (greetd runs the
#     selector, not a named session), so the desktop's cage-pin no longer gates
#     the boot tier — the supervisor does.
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

  # ── Shell-paint readiness marker (the HUNG-tier guard) ──
  # The crash-loop accounting below only catches a tier whose COMPOSITOR PROCESS
  # EXITS. It is blind to the worse real-hardware failure the "boots to only a
  # mouse pointer" regression exposed: the compositor is UP (sway's cursor shows)
  # but the glass-shell layer-shell host never PAINTS and never exits — so the
  # session hangs forever and `sh -c "$CMD"` blocks, never reaching the drop
  # logic. The screen is stuck with no shell and no fallback.
  #
  # The fix is the shell-paint watchdog (selector loop below): the shell host
  # signals "I painted my first frame" by touching this tmpfs marker; if the
  # marker does NOT appear within shellPaintTimeoutSeconds while the compositor is
  # still alive, the tier is HUNG and is dropped exactly like a crash (same
  # record_crash → lower_tier → write_tier path, no parallel mechanism). It is in
  # /run (tmpfs) so every boot starts with a clean slate, and the selector clears
  # it before each launch so a previous tier's marker can never mask a hang.
  #
  # Contract for the shell host (cage hart-glass-shell + the GTK4 layer-shell
  # host): touch this marker once the WebView presents its first frame. Absent
  # that touch (e.g. an older shell build that does not yet write it), the
  # watchdog still fires on timeout and escalates DOWN — which is the safe
  # direction (a slow-but-fine tier degrades to a faster-painting one; it never
  # strands the user on a blank higher tier). The cage FLOOR is exempt: it is the
  # audited paint floor and there is nothing below it to drop to.
  #
  # The marker lives in a dedicated GROUP-WRITABLE dir (/run/hart/session, 0770
  # hart hart) — NOT directly under /run/hart (0750, the `hart` group cannot
  # write there). The shell host + selector run as hart-admin, which IS in the
  # `hart` group, so both can create/clear the marker here. We do NOT widen the
  # shared /run/hart mode (other consumers rely on 0750) — only this subdir.
  sessionRunDir = "/run/hart/session";
  readyFlag = "${sessionRunDir}/shell-ready";

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
    READY="${readyFlag}"
    MAX_CRASHES=${toString sup.crashLoopCount}
    WINDOW_SECS=${toString sup.crashLoopWindowSeconds}
    PAINT_TIMEOUT=${toString sup.shellPaintTimeoutSeconds}
    LADDER="${lib.concatStringsSep " " tierLadder}"
    FLOOR="cage"
    # The HIGHEST tier a FRESH (un-latched) boot starts at — so the ladder tries
    # the BEST tier first and only degrades on a real crash/hang. A drop still
    # latches the LOWER tier across boot; this is purely the un-latched default.
    START="${sup.startTier}"

    log() { echo "[hart-session-supervisor] $*" >&2; }

    # ── Read the tier to launch (SESSION_TIER_CONTRACT.md §3 rule 1) ──
    #   - latch PRESENT + valid value (hart-comp|sway|cage) -> that latched tier
    #   - latch MISSING / unreadable / invalid token        -> the START tier
    #     (startTier option; default `hart-comp` = the head of the ladder)
    #
    # A FRESH boot starts at the BEST tier ($START, default Tier-1 hart-comp) and
    # the ladder only DEGRADES on a real failure — a crash or a paint-timeout —
    # each of which writes the LOWER tier to the latch (which then persists across
    # boot). The supervisor owns the never-blank guarantee, so starting high is
    # safe: an unavailable/crashing/hung higher tier falls straight through to
    # sway then the cage floor (and can never drop below cage). Set startTier=cage
    # to restore the old "missing ⇒ floor, never attempt a higher tier unless an
    # operator arms it" behaviour. `hartctl session reset-tier` still re-arms the
    # top rung (writes hart-comp) so a transient Tier-1 bug never permanently
    # masks as a downgrade.
    read_tier() {
      if [ -r "$LATCH" ]; then
        _rtv=$(cat "$LATCH" 2>/dev/null | tr -d '[:space:]')
        case "$_rtv" in
          hart-comp|sway|cage) printf '%s' "$_rtv"; return 0 ;;
        esac
      fi
      printf '%s' "$START"
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
    # Clear any stale paint marker from a previous tier/boot BEFORE launch so it
    # can never mask this tier's hang (it lives in /run tmpfs but a same-boot
    # re-launch could leave a marker behind). The shell host re-touches it on its
    # first painted frame.
    rm -f "$READY" 2>/dev/null || true

    # Tell the launched shell host WHERE to write its first-paint marker, so the
    # host and this watchdog share ONE path (no hardcoded divergence). The host
    # honours HART_SHELL_READY_FLAG and falls back to the same /run/hart default.
    export HART_SHELL_READY_FLAG="$READY"

    # Run the session in the BACKGROUND so the paint-watchdog can observe a HUNG
    # tier (compositor up, shell never paints, process never exits). When it
    # EXITS (crash or clean), `wait` below returns its rc; greetd then relaunches
    # this selector for the next session attempt. On a crash OR a paint-timeout we
    # drop a tier and latch BEFORE returning.
    start=$(date +%s)
    sh -c "$CMD" &
    sesspid=$!

    # ── Shell-paint watchdog ──────────────────────────────────────────────────
    # Poll up to PAINT_TIMEOUT for one of three outcomes:
    #   (a) the session process exits early  -> fall through to exit-accounting
    #       (the EXISTING crash path handles it — unchanged);
    #   (b) the shell touches $READY         -> it painted; stop watching and just
    #       `wait` for the session (a healthy long-lived run);
    #   (c) the timeout elapses with the process STILL ALIVE and NO $READY marker
    #       -> the tier is HUNG. On a non-floor tier, kill it and treat it as a
    #       crash so the SAME record_crash -> lower_tier -> write_tier drop runs.
    # PAINT_TIMEOUT=0 disables the watchdog (pure crash-only behaviour).
    painted=0
    if [ "$PAINT_TIMEOUT" -gt 0 ]; then
      waited=0
      while [ "$waited" -lt "$PAINT_TIMEOUT" ]; do
        if ! kill -0 "$sesspid" 2>/dev/null; then
          break                       # (a) session already exited — crash path
        fi
        if [ -e "$READY" ]; then
          painted=1                    # (b) shell painted its first frame
          break
        fi
        sleep 1
        waited=$((waited + 1))
      done

      if [ "$painted" -eq 0 ] && kill -0 "$sesspid" 2>/dev/null; then
        # (c) HUNG: compositor process alive but no first-paint within the budget.
        if [ "$TIER" != "$FLOOR" ]; then
          log "tier '$TIER' is HUNG (compositor up, no first paint in ''${PAINT_TIMEOUT}s) — killing + treating as a crash"
          kill -TERM "$sesspid" 2>/dev/null || true
          sleep 2
          kill -KILL "$sesspid" 2>/dev/null || true
          wait "$sesspid" 2>/dev/null || true
          # Reuse the EXACT crash-drop path (no parallel mechanism): record this
          # hang as a crash and, on threshold, drop one tier + latch. A hung tier
          # is at least as bad as a crashed one, so it counts toward the same
          # window — a tier that hangs MAX_CRASHES times escalates DOWN.
          if record_crash; then
            nxt=$(lower_tier "$TIER")
            log "paint-watchdog crash-loop on '$TIER' ($MAX_CRASHES hangs/crashes in ''${WINDOW_SECS}s) — dropping to '$nxt' and latching"
            write_tier "$nxt"
            clear_window
          fi
          # Return to greetd; it relaunches the selector, which now reads the
          # (possibly) lowered latch.
          exit 0
        else
          # The floor itself hasn't signalled paint yet — never drop below it.
          # Let it keep running (cage is the audited paint floor); just stop
          # watching and wait normally. The screen still paints by the floor's
          # own contract; a missing marker here only means an older floor build.
          log "floor ('$FLOOR') has not signalled paint within ''${PAINT_TIMEOUT}s — staying on the floor (cannot drop below it)"
        fi
      fi
    fi

    # Healthy/painted OR floor: wait for the session to exit normally, then run
    # the SAME exit-accounting as before (crash-on-early-exit / clean logout).
    wait "$sesspid"
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

    startTier = lib.mkOption {
      type = lib.types.enum [ "hart-comp" "sway" "cage" ];
      default = "hart-comp";
      description = ''
        The HIGHEST tier a FRESH boot starts at — the top rung the ladder
        attempts before the watchdog/crash-loop drops it. When the latch is
        ABSENT / unreadable / invalid (a clean image, a wiped state dir, a torn
        write), the selector seeds it to this tier instead of the cage floor, so
        the boot tries the BEST tier first and only DEGRADES on a real failure
        (a crash or a paint-timeout). A drop still LATCHES across boot, and
        `hartctl session reset-tier` re-arms the top rung; this option only sets
        where an UN-latched boot begins.

        Default `hart-comp` (Tier-1, the head of the ladder): the supervisor owns
        the never-blank guarantee, so starting high is safe — an unavailable or
        crashing/hung higher tier falls straight through to sway then the cage
        floor (the supervisor can never drop below cage). Set to `cage` for the
        old fail-safe-to-floor behaviour (never attempt a higher tier unless an
        operator arms it). `sway` starts at Tier-2. The chosen tier must still be
        AVAILABLE (its launch command non-null) or the selector skips it down the
        ladder exactly as it does for a latched-but-unavailable tier.
      '';
    };

    shellPaintTimeoutSeconds = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 20;
      description = ''
        Shell-paint watchdog budget (seconds). After a tier's compositor is
        launched, the glass-shell host must signal its first painted frame (touch
        /run/hart/session/shell-ready) within this many seconds. If it does NOT — while
        the compositor process is still alive — the tier is treated as HUNG and
        dropped one rung exactly like a crash (same crash-loop accounting). This
        catches the "compositor up but shell never paints / never exits" failure
        the bare crash-on-exit detection is blind to (the real-hardware
        pointer-only boot). Set to 0 to disable the watchdog (crash-only
        behaviour). The cage FLOOR is never dropped by this watchdog.
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
        Default is the bare WM; when hart.swayTier1.enable is set, that module
        upgrades this (via mkDefault) to its `hart-sway-session` launcher, which
        auto-starts the glass shell + puts the swaymsg tile/summon shim on PATH
        (the Phase-8 Tier-2 parity rung). An explicit operator value still wins.
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
      # Group-writable so the shell host (hart-admin, in the `hart` group) can
      # write the first-paint marker the paint-watchdog consumes. Scoped subdir —
      # the shared /run/hart stays 0750.
      "d /run/hart/session 0770 hart hart -"
    ];

    # Enabling greetd (below) pulls in upstream nixos graphical-desktop.nix,
    # which sets fs.inotify.max_user_watches via mkDefault — the SAME option
    # hart-base.nix also sets via mkDefault, and two equal-priority mkDefaults
    # collide ("defined multiple times"). Resolve the tie HERE, gated to the
    # supervisor that introduces greetd→graphical-desktop; the value is identical
    # (524288) so behaviour is unchanged — only the priority tie is broken. Found
    # by the wired-in nixosTest's CI eval (the gate working as intended).
    #
    # PRIORITY 90 (mkOverride 90), NOT mkForce(50): on the real desktop closure
    # hart-kernel.nix ALSO sets this option, with mkForce(50) = 1048576. Two
    # mkForces would themselves collide. Using mkOverride 90 (weaker than
    # mkForce(50) but stronger than the two mkDefaults at 1000) lets hart-kernel's
    # mkForce win cleanly WHEN it is enabled (desktop: 1048576 applies), while
    # still breaking the mkDefault tie on a supervisor node that has NO hart-kernel
    # (the nixosTest nodes). One value, no collision either way.
    boot.kernel.sysctl."fs.inotify.max_user_watches" = lib.mkOverride 90 524288;

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
