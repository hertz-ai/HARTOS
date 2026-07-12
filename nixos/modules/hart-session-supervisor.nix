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
  # Set ONLY when a tier is dropped by a paint-HANG (vs a crash-loop). Its
  # presence makes the persisted latch ELIGIBLE for ONE automatic fresh-boot
  # re-promotion — a cold-boot slow first paint is transient, not a permanent
  # break, so a one-off slow boot must not demote the machine forever (the only
  # other way back up is a manual `hartctl session reset-tier`). Persistent
  # (next to the latch) so it survives the reboot the re-promotion happens on.
  hangMarkFile = "${stateDir}/session-tier.hang";
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

  # ── Input-alive marker (the INPUT twin of the paint marker) ──
  # The paint watchdog above catches a tier that never PRESENTS a frame. It is
  # blind to the WORSE-on-real-hardware failure the "pointer frozen at 0,0,
  # nothing types" regression exposed (#134): the compositor is UP and PAINTS (the
  # onboarding screen renders, a caret BLINKS — frames ARE being scanned out) but
  # NO input is delivered, pointer AND keyboard dead at once. That is the
  # seat/libinput layer failing to feed events into the compositor (the libinput
  # event source not live in the calloop loop, missing seat pointer/keyboard
  # capabilities, or no devices opened by libseat), NOT a hung compositor (it
  # still presents the blink) and NOT a WebView focus issue (pointer MOTION is
  # focus-independent yet frozen). A painted-but-input-starved tier PASSES the
  # paint watchdog and then `wait`s forever, locking the user out behind a pretty,
  # dead screen.
  #
  # The input-alive marker is the symmetric signal: the tier's compositor touches
  # this tmpfs flag once its INPUT PIPELINE is proven LIVE — libinput's event
  # source is wired into the event loop AND it is actually delivering events into
  # the seat (the natural write site is the first InputEvent dispatched into
  # State::process_input_event in compositor/src/udev.rs step 11(a), which fires
  # at STARTUP as libseat opens the devices, NOT a user keypress). It is a
  # STARTUP-assertable capability signal, deliberately NOT "the user has
  # interacted", so an idle user can never be mistaken for a dead seat — that
  # distinction is the whole flap-safety of this dimension. Same dir, same 0770
  # group-writable rule, same export-the-path contract as shell-ready, so the
  # compositor and this watchdog share ONE path with no hardcoded divergence
  # (HART_INPUT_ALIVE_FLAG, exported next to HART_SHELL_READY_FLAG below).
  #
  # FAIL-SAFE / NEVER-FLAP: this check is OFF by default
  # (inputAliveTimeoutSeconds = 0). Absence of the marker is AMBIGUOUS — it can
  # mean "input genuinely dead" OR "this tier's compositor build does not write
  # the marker yet" — and dropping on a missing WRITER would flap EVERY healthy
  # tier down to the floor on every boot. So unlike the paint marker (whose
  # absence-after-timeout safely degrades to a faster-painting tier), the input
  # marker is only treated as AUTHORITATIVE when an operator opts in by setting
  # inputAliveTimeoutSeconds > 0 — which declares "the tiers I run write this
  # marker, so its absence is real input-death." Until the tier compositors
  # (udev.rs / the cage floor) write HART_INPUT_ALIVE_FLAG, it stays 0 and this is
  # a pure no-op (mirrors compCommand=null reserving Tier-1 until Phase 3). The
  # cage FLOOR is exempt (nothing below it to drop to), exactly like paint.
  inputAliveFlag = "${sessionRunDir}/input-alive";

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
    INPUT_ALIVE="${inputAliveFlag}"
    # ── Fresh-boot re-promotion state (transient-cold-boot self-heal) ──
    #   HANGMARK (persistent, next to the latch): set ONLY by a paint-HANG drop;
    #     a crash-loop drop clears it (a crash is sticky, never re-promoted).
    #   BOOT_SENTINEL (/run tmpfs): absent at power-on; gates the re-promotion to
    #     the FIRST selector run of a fresh boot so a logout/relaunch never re-tries.
    #   REPROMOTED_FLAG (/run tmpfs): set when this boot already spent its one retry;
    #     a re-hang then SETTLES (does not re-arm) so a confirmed-broken tier is
    #     never re-walked every boot (preserves the latch's never-re-walk guarantee).
    HANGMARK="${hangMarkFile}"
    BOOT_SENTINEL="${sessionRunDir}/boot-repromote-checked"
    REPROMOTED_FLAG="${sessionRunDir}/repromoted-this-boot"
    MAX_CRASHES=${toString sup.crashLoopCount}
    WINDOW_SECS=${toString sup.crashLoopWindowSeconds}
    PAINT_TIMEOUT=${toString sup.shellPaintTimeoutSeconds}
    INPUT_TIMEOUT=${toString sup.inputAliveTimeoutSeconds}
    # The seat input-device enumerator the input-alive watchdog consults before it
    # drops a painted-but-input-starved tier (the touch-only / device-less guard).
    # An operator-set, trusted command (same trust level as the tier launch commands
    # run via `sh -c "$CMD"`), so it is run word-split-unquoted as a command + args.
    INPUT_DEVICE_PROBE="${sup.inputDeviceProbeCommand}"
    LADDER="${lib.concatStringsSep " " tierLadder}"
    FLOOR="cage"
    # The HIGHEST tier a FRESH (un-latched) boot starts at — so the ladder tries
    # the BEST tier first and only degrades on a real crash/hang. A drop still
    # latches the LOWER tier across boot; this is purely the un-latched default.
    START="${sup.startTier}"
    # Grace (seconds) to let a HUNG compositor drop DRM master after SIGTERM before
    # we SIGKILL it (a hard kill mid-scanout can orphan card0's DRM master → the
    # next tier gets EBUSY). And the settle (seconds) we pause after a tier's
    # process is gone so the kernel reclaims that master before the next tier (which
    # greetd relaunches) tries drmSetMaster. Both small + bounded — they only add a
    # few seconds to a DROP, never to a healthy boot.
    TIER_TERM_GRACE=${toString sup.tierTermGraceSeconds}
    DRM_SETTLE=${toString sup.drmMasterSettleSeconds}

    # Emit each decision to stderr (greetd's tty, for a console operator) AND —
    # best-effort — to the journal under `hart-session-supervisor`, because the
    # greetd SESSION's stderr does NOT reach journald on its own (real-HW 2026-07-10:
    # a full HARTJRNL journal export had ZERO supervisor/compositor lines, so every
    # tier-drop diagnosis was a guess). systemd-cat reads stdin line-by-line here;
    # guarded on the binary + `|| true` so a missing/broken systemd-cat can never
    # turn a log call into a script-fatal error on the never-brick path.
    log() {
      echo "[hart-session-supervisor] $*" >&2
      [ -x "${pkgs.systemd}/bin/systemd-cat" ] \
        && printf '%s\n' "[hart-session-supervisor] $*" \
             | "${pkgs.systemd}/bin/systemd-cat" -t hart-session-supervisor 2>/dev/null \
        || true
    }

    # ── Let the kernel reclaim the DRM master a just-exited compositor held ──
    # After a tier's process is GONE (crashed, hung-killed, or logged out), the
    # kernel needs a brief moment to release that process's DRM master + GBM/scanout
    # state on card0. Without this the NEXT tier greetd relaunches can race the
    # teardown and fail drmSetMaster with EBUSY ("device busy") — the exact bare-HW
    # symptom across all tiers. A plain bounded sleep (no card0 poking) is the
    # safest portable settle; DRM_SETTLE=0 disables it (the nixosTest VMs, which
    # have no real DRM master to reclaim, set it to 0 to stay fast).
    drm_master_settle() {
      [ "$DRM_SETTLE" -gt 0 ] 2>/dev/null && sleep "$DRM_SETTLE" || true
    }

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

    # ── Ladder position of a tier (0 = top .. N = floor); -1 if not a member ──
    # Lets write_tier tell a DEGRADE (a downward drop toward the cage floor) apart
    # from a promote / initial latch, so ONLY a real fall-back arms the boot-log
    # capture below.
    ladder_index() {
      _li=0
      for _lt in $LADDER; do
        [ "$_lt" = "$1" ] && { printf '%s' "$_li"; return 0; }
        _li=$((_li + 1))
      done
      printf '%s' "-1"
    }

    # ── Atomically write the latch (latches across boot) ──
    write_tier() {
      umask 022
      _prev_tier=""
      [ -r "$LATCH" ] && _prev_tier="$(tr -d '[:space:]' < "$LATCH" 2>/dev/null || true)"
      # No latch yet ⇒ the supervisor was running the START (top) tier — read_tier's
      # default when the latch is absent — so a first drop below it is STILL a
      # degrade. Infer START as the prior so the very first fall-back (1->2) is
      # captured, not just a later 2->3 (e.g. when Tier-2 then stabilizes and Tier-1's
      # failure would otherwise never be captured). Does NOT write the latch here, so
      # the "latch absent = fail-safe" contract is untouched.
      [ -z "$_prev_tier" ] && _prev_tier="$START"
      printf '%s\n' "$1" > "$LATCH.tmp" && mv -f "$LATCH.tmp" "$LATCH"
      log "latched session tier = $1"
      # DEGRADE capture hook (the "why did it fall back to cage" evidence): when this
      # write LOWERS the tier (a 1->2 / 2->3 drop toward the floor), touch the boot-
      # log trigger so the canonical hart-boot-log capture writes a FRESH journalctl
      # bundle to the persistent HARTLOG partition AT THE MOMENT of the drop. The
      # live-USB journal is volatile (tmpfs), so this grabs the failed tier's output +
      # this supervisor's drop-REASON log before the next tier floods/rolls it, and
      # beats waiting up to one periodic interval. NO journalctl here — the reason is
      # already in this supervisor's journal, which the capture collects (DRY: one
      # capture path, hart-boot-log-capture, never a second parallel one). Best-effort
      # (|| true): arming the capture must never wedge the drop itself.
      if [ -n "$_prev_tier" ] && [ "$_prev_tier" != "$1" ]; then
        _pi=$(ladder_index "$_prev_tier"); _ni=$(ladder_index "$1")
        if [ "$_pi" -ge 0 ] && [ "$_ni" -gt "$_pi" ]; then
          printf 'from=%s to=%s ts=%s\n' "$_prev_tier" "$1" "$(date +%s)" \
            > "${sessionRunDir}/tier-degraded" 2>/dev/null || true
          log "tier degrade $_prev_tier -> $1: armed HARTLOG boot-log capture"
        fi
      fi
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
    # SAFETY: drop is ONLY ever DOWNWARD. If `cur` is not a member of the ladder
    # (a torn/garbage latch that slipped past read_tier's validation, or a future
    # caller passing an unvalidated value), we must NOT walk past a never-matched
    # `cur` and hand back the first available tier — that would be an UPWARD drop.
    # Return the FLOOR (cage) for any out-of-ladder `cur`: a drop can never raise
    # the tier, and the floor always paints. (Today's callers pass read_tier-
    # validated values so this guard is belt-and-suspenders, but the invariant
    # "lower_tier never returns above `cur`" must hold structurally, not by luck.)
    lower_tier() {
      cur="$1"; seen=""; pick=""
      case " $LADDER " in
        *" $cur "*) : ;;                       # cur IS a ladder member — proceed
        *) printf '%s' "$FLOOR"; return 0 ;;    # out-of-ladder cur -> floor, never up
      esac
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

    # ── Kill a HUNG non-floor tier and drop one rung (the SINGLE deterministic-
    #    hang drop path, shared by BOTH the paint-timeout and the input-alive-
    #    timeout watchdogs — DRY, no parallel mechanism) ───────────────────────
    # A HANG is DETERMINISTIC, unlike a fast-exit crash: the compositor came up but
    # never satisfied a liveness signal (no first paint, OR painted but no input)
    # and a retry fails the SAME way — so it is dropped after the FIRST timeout,
    # NOT after crashLoopCount (waiting crashLoopCount × the budget would mean a
    # minute-plus of black/dead screen on real HW before reaching the floor). The
    # caller GUARANTEES "$TIER" != "$FLOOR" and that the session process is still
    # alive; $1 is the human reason for the log. Does NOT return — exits to greetd
    # on the lowered, latched tier.
    drop_hung_tier() {
      _reason="$1"
      log "tier '$TIER' is HUNG ($_reason) — killing + dropping immediately"
      # SIGTERM FIRST and give the compositor a real grace window to drop DRM
      # master cleanly (drmDropMaster on its way out) before we SIGKILL. A straight
      # SIGKILL on a compositor that is mid-scanout can orphan the DRM master on
      # card0, so the NEXT tier greetd relaunches hits EBUSY ("device busy") and the
      # whole ladder stalls on a black screen. SIGTERM → wait → SIGKILL only if it
      # ignored the term is the standard graceful handoff.
      kill -TERM "$sesspid" 2>/dev/null || true
      term_waited=0
      while [ "$term_waited" -lt "$TIER_TERM_GRACE" ] && kill -0 "$sesspid" 2>/dev/null; do
        sleep 1
        term_waited=$((term_waited + 1))
      done
      kill -KILL "$sesspid" 2>/dev/null || true
      wait "$sesspid" 2>/dev/null || true
      # Let the kernel reclaim DRM master + tear down the GBM/scanout state the
      # killed compositor held, so the next tier can become master cleanly.
      drm_master_settle
      # We still record the hang in the crash window (DRY: the SAME record_crash →
      # lower_tier → write_tier accounting keeps the window/threshold telemetry
      # consistent) but the DROP is unconditional, not threshold-gated — only the
      # fast-EXIT crash path requires crashLoopCount.
      record_crash || true
      nxt=$(lower_tier "$TIER")
      log "watchdog: HUNG '$TIER' dropped to '$nxt' on the FIRST timeout (deterministic hang) — latching"
      write_tier "$nxt"
      # Self-documenting fall-to-cage: write_tier ALREADY armed the HARTLOG boot-log
      # capture on this downward drop (see write_tier + hart-boot-log.nix's path unit)
      # — the ONE capture path, covering EVERY drop (hang here, crash-loop, and the
      # reconciliation drop), PATH-independent, and run by a root service that
      # survives this session's teardown. No second inline capture here (no parallel
      # mechanism); the bundle records the exact from→to via /run/hart/session/
      # tier-degraded + this supervisor's journal.
      # A HANG MAY be a transient cold-boot slow start, not a permanent break — arm
      # ONE fresh-boot re-promotion. BUT if this boot already spent its re-promotion
      # retry on this tier (REPROMOTED_FLAG set), the hang is CONFIRMED: clear the
      # arm so the latch settles and is never re-walked on later boots (the latch's
      # never-re-walk guarantee holds for confirmed failures).
      if [ -e "$REPROMOTED_FLAG" ]; then
        rm -f "$HANGMARK" 2>/dev/null || true
      else
        : > "$HANGMARK" 2>/dev/null || true
      fi
      clear_window
      # Return to greetd; it relaunches the selector, which now reads the lowered
      # latch.
      exit 0
    }

    # ── Touch-only / device-less guard for the input-alive drop (FM3b / FM5) ──
    # The compositor emits the input-alive beacon (HART_INPUT_ALIVE_FLAG) on a
    # KEYBOARD or POINTER event only — NOT on touch (hart-comp does not yet route
    # wl_touch). So on a touchSCREEN-only box (or a box with no input device at all)
    # a missing beacon is EXPECTED, not input-death, and dropping the tier would flap
    # a healthy painting surface to the floor. This returns SUCCESS (a drop is
    # justified) UNLESS it can POSITIVELY confirm the seat exposes ONLY touch / no
    # beacon-eligible device. It is CONSERVATIVE toward the existing drop behaviour:
    # an empty or inconclusive probe still allows the drop (so a normal keyboard/mouse
    # box and every CI VM are unaffected) — it can ONLY ever SUPPRESS a wrong drop,
    # never cause one. Two sources, in order:
    #   1. $INPUT_DEVICE_PROBE (libinput list-devices): authoritative `Capabilities:`
    #      classification — `keyboard`/`pointer` => drop justified; only `touch` =>
    #      suppress.
    #   2. the always-present kernel evdev table (/proc/bus/input/devices): a `mouseN`
    #      handler is a pointer (mouse/touchpad), a `kbd`+`leds` handler is a real
    #      keyboard (the `leds` handler excludes power/lid pseudo-keyboards); a pure
    #      touchscreen registers neither (only `event`). No pointer/keyboard handler
    #      AND at least one device present => provably touch-only => suppress.
    seat_has_beacon_input_device() {
      _caps=""
      if [ -n "$INPUT_DEVICE_PROBE" ]; then
        # Unquoted on purpose: this is an operator-trusted "command + args" string.
        _caps=$($INPUT_DEVICE_PROBE 2>/dev/null | grep -i 'Capabilities:' 2>/dev/null) || _caps=""
      fi
      if [ -n "$_caps" ]; then
        # libinput classified the seat — trust it.
        if printf '%s\n' "$_caps" | grep -qiE 'keyboard|pointer'; then
          return 0    # a beacon-eligible device exists -> a missing beacon is real death
        fi
        if printf '%s\n' "$_caps" | grep -qi 'touch'; then
          return 1    # seat lists ONLY touch -> compositor cannot beacon it -> suppress
        fi
        return 0      # classified but neither -> inconclusive -> allow the drop
      fi
      # No libinput output: fall back to the kernel evdev table.
      if [ -r /proc/bus/input/devices ]; then
        if grep -qiE '^H: Handlers=.*(mouse[0-9]|kbd[^=]*leds|leds[^=]*kbd)' /proc/bus/input/devices; then
          return 0    # a pointer or a real (LED-bearing) keyboard exists -> drop justified
        fi
        if grep -qE '^B: EV=' /proc/bus/input/devices; then
          return 1    # devices exist but NONE are pointer/keyboard -> touch-only -> suppress
        fi
        return 0      # empty/unreadable table -> inconclusive -> allow the drop
      fi
      return 0        # no evdev table at all -> inconclusive -> allow the drop
    }

    # ── Fresh-boot re-promotion of a transiently HANG-dropped tier ────────────
    # A latch is lowered by either a fast-exit CRASH-LOOP (crashLoopCount fast
    # exits — a strong "genuinely broken" signal that stays STICKY) or a single
    # deterministic paint-HANG (one timeout). A COLD first boot (disk/GPU still
    # warming) can trip the paint-watchdog ONCE and latch a lower tier — and the
    # ONLY way back up today is a manual `hartctl session reset-tier`. So a one-off
    # slow boot permanently demotes the machine (worst case all the way to cage).
    # This self-heals that WITHOUT re-walking a confirmed-broken tier every boot:
    #   - only a HANG drop arms re-promotion (it wrote $HANGMARK); a crash drop
    #     cleared it and stays sticky;
    #   - on the FIRST selector run of a FRESH boot (gated by $BOOT_SENTINEL, a
    #     /run tmpfs flag absent at power-on) we re-arm a hang-latched tier to the
    #     START tier for ONE retry. If the warm retry paints, the transient is
    #     healed; if it HANGS again this boot the hang path sees $REPROMOTED_FLAG
    #     and does NOT re-arm $HANGMARK, so the tier settles and later boots never
    #     re-walk it.
    # Every step is best-effort + fail-safe: any unwritable flag just leaves the
    # persisted latch untouched (degrade, never brick).
    maybe_repromote() {
      [ -e "$BOOT_SENTINEL" ] && return 0          # only the first run of a boot
      : > "$BOOT_SENTINEL" 2>/dev/null || true     # mark this boot's check done
      [ -e "$HANGMARK" ] || return 0               # last drop was NOT a hang -> sticky
      _cur=$(read_tier)
      if [ "$_cur" = "$START" ]; then
        rm -f "$HANGMARK" 2>/dev/null || true      # already at the top rung -> nothing to raise
        return 0
      fi
      log "fresh-boot re-promotion: latch '$_cur' was a transient paint-HANG drop, re-arming to start tier '$START' for ONE retry"
      rm -f "$HANGMARK" 2>/dev/null || true
      : > "$REPROMOTED_FLAG" 2>/dev/null || true   # a re-hang this boot now settles (no re-arm)
      write_tier "$START"
    }

    # Run the once-per-boot re-promotion check BEFORE selecting a tier, so a
    # transient cold-boot paint-HANG that demoted the machine on a previous boot
    # gets ONE automatic retry at the start tier this boot. No-op unless the last
    # drop was a hang; crash-loop drops stay sticky.
    maybe_repromote

    # ── If node_watchdog flagged the compositor unhealthy this boot, treat it
    #    as a crash for tier-drop accounting (signal-only; it never picks). ──
    #
    # CRITICAL — this block ALWAYS `exit 0`s after handling, so a single selector
    # invocation records AT MOST ONE crash. Falling through here to launch a tier
    # would (a) record a SECOND crash for the same boot on that tier's crash/hang
    # path (firing the crash-loop threshold a cycle early and non-deterministically),
    # and (b) launch the OLD `read_tier` value even after we just dropped+latched a
    # LOWER one — relaunching the very tier the watchdog flagged unhealthy. Instead
    # we record the unhealthy signal as THIS boot's one crash, drop+latch if the
    # threshold is breached, and return to greetd, which relaunches the selector on
    # the (possibly lowered) latch — a clean, single-crash cycle.
    if [ -e "$UNHEALTHY" ]; then
      log "node_watchdog signalled compositor unhealthy"
      rm -f "$UNHEALTHY" 2>/dev/null || true
      if record_crash; then
        cur=$(read_tier)
        if [ "$cur" != "$FLOOR" ]; then
          nxt=$(lower_tier "$cur")
          log "unhealthy-signal crash-loop: dropping $cur -> $nxt (latching + relaunching on the new tier)"
          write_tier "$nxt"
          rm -f "$HANGMARK" 2>/dev/null || true   # crash-class drop is STICKY (never re-promoted)
          clear_window
        else
          log "unhealthy-signal crash-loop on the floor ('$FLOOR') — cannot drop further; relaunching the floor"
        fi
      fi
      # Return to greetd on the (possibly lowered) latch WITHOUT launching a tier in
      # this run — guarantees this invocation recorded exactly one crash.
      exit 0
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
    # Publish the ACTUALLY-RUNNING tier (the rung we are launching NOW) so
    # hart-display-health + any observer report the LIVE tier, NOT the drop-LATCH.
    # The latch is written ONLY on a downward drop, so a clean hart-comp start never
    # wrote it → hart-display-health defaulted to 'cage' and misreported a fully
    # working Tier-1 as cage for weeks (real-HW 2026-07-12: hart-comp ran + scanned
    # out + painted while display-health said tier=cage, sending every diagnosis down
    # the wrong path). One writer, /run tmpfs, rewritten on every (re)launch so it
    # always reflects the rung that is actually up. Best-effort; never fatal.
    printf '%s\n' "$TIER" > "${sessionRunDir}/current-tier" 2>/dev/null || true
    # Clear any stale paint + input markers from a previous tier/boot BEFORE launch
    # so they can never mask this tier's hang (they live in /run tmpfs but a same-
    # boot re-launch could leave a marker behind). The shell host re-touches the
    # paint marker on its first painted frame; the compositor re-touches the input
    # marker once its input pipeline is live.
    rm -f "$READY" 2>/dev/null || true
    rm -f "$INPUT_ALIVE" 2>/dev/null || true

    # Tell the launched shell host + compositor WHERE to write their liveness
    # markers, so the writers and this watchdog share ONE path each (no hardcoded
    # divergence). The host honours HART_SHELL_READY_FLAG (first paint) and the
    # compositor honours HART_INPUT_ALIVE_FLAG (input pipeline live); both fall back
    # to the same /run/hart defaults when unset.
    export HART_SHELL_READY_FLAG="$READY"
    export HART_INPUT_ALIVE_FLAG="$INPUT_ALIVE"

    # Run the session in the BACKGROUND so the paint-watchdog can observe a HUNG
    # tier (compositor up, shell never paints, process never exits). When it
    # EXITS (crash or clean), `wait` below returns its rc; greetd then relaunches
    # this selector for the next session attempt. On a crash OR a paint-timeout we
    # drop a tier and latch BEFORE returning.
    start=$(date +%s)
    # Route this tier's stdout+stderr — AND the shell host it launches, which inherits
    # these fds — into the journal under a per-tier identifier (`hart-tier-hart-comp`,
    # `hart-tier-sway`, `hart-tier-cage`), so a real-HW tier crash/hang is finally
    # diagnosable via `journalctl -t hart-tier-<tier>` (captured by the HARTJRNL
    # journal-export). This is the missing half of the self-logging chain: the
    # compositor + shell run inside the greetd session, whose stderr never reaches
    # journald otherwise. systemd-cat EXECs the command (it does not fork), so the pid
    # `sesspid` captures + the `kill`/`wait` the paint-watchdog and crash-accounting
    # below rely on are preserved unchanged. Guarded + fallback: if systemd-cat is
    # somehow unavailable the tier launches plain — the never-black floor must NEVER
    # depend on the logger being present.
    if [ -x "${pkgs.systemd}/bin/systemd-cat" ]; then
      "${pkgs.systemd}/bin/systemd-cat" -t "hart-tier-$TIER" sh -c "$CMD" &
    else
      sh -c "$CMD" &
    fi
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
          drop_hung_tier "compositor up, no first paint in ''${PAINT_TIMEOUT}s"
        else
          # The floor itself hasn't signalled paint yet — never drop below it.
          # Let it keep running (cage is the audited paint floor); just stop
          # watching and wait normally. The screen still paints by the floor's
          # own contract; a missing marker here only means an older floor build.
          log "floor ('$FLOOR') has not signalled paint within ''${PAINT_TIMEOUT}s — staying on the floor (cannot drop below it)"
        fi
      fi
    fi

    # ── Input-alive watchdog (the INPUT twin of the paint watchdog) ────────────
    # A tier can PASS the paint watchdog (it presented a frame, $READY exists) yet
    # be INPUT-STARVED: the compositor scans out but the seat/libinput layer never
    # delivers pointer/keyboard events (the real-HW "pointer frozen at 0,0, nothing
    # types" — #134). The paint check is blind to it; this catches it. It runs ONLY
    # when the tier actually PAINTED (painted=1 — input-aliveness is only meaningful
    # once a tier is presenting) AND the operator opted in (INPUT_TIMEOUT > 0 — see
    # inputAliveFlag's never-flap note: marker absence is authoritative ONLY when
    # the running tiers are known to write it) AND the process is still alive. We
    # poll up to INPUT_TIMEOUT for $INPUT_ALIVE; if the compositor never asserts its
    # input pipeline is live while still running, the tier is input-dead and is
    # dropped via the SAME deterministic-hang path (drop_hung_tier — DRY). The cage
    # FLOOR is exempt. INPUT_TIMEOUT=0 (default) makes this a pure no-op, byte-
    # identical to the paint-only behaviour, so a build whose tiers do not yet write
    # the marker can NEVER flap a healthy tier down.
    if [ "$painted" -eq 1 ] && [ "$INPUT_TIMEOUT" -gt 0 ] && kill -0 "$sesspid" 2>/dev/null; then
      input_alive=0
      iwaited=0
      while [ "$iwaited" -lt "$INPUT_TIMEOUT" ]; do
        if ! kill -0 "$sesspid" 2>/dev/null; then
          break                       # session exited — the crash path below handles it
        fi
        if [ -e "$INPUT_ALIVE" ]; then
          input_alive=1               # the compositor proved its input pipeline is live
          break
        fi
        sleep 1
        iwaited=$((iwaited + 1))
      done

      if [ "$input_alive" -eq 0 ] && kill -0 "$sesspid" 2>/dev/null; then
        if [ "$TIER" != "$FLOOR" ]; then
          # Only treat a missing beacon as input-death if the seat actually exposes a
          # keyboard/pointer (a device the compositor WOULD beacon on). On a touch-
          # only / device-less seat the beacon legitimately never fires (FM3b/FM5), so
          # suppress the drop and keep the painting tier — never flap it to the floor.
          if seat_has_beacon_input_device; then
            drop_hung_tier "painted but no input-alive signal in ''${INPUT_TIMEOUT}s (input-starved seat)"
          else
            log "tier '$TIER' painted but no input-alive signal in ''${INPUT_TIMEOUT}s — seat exposes no pointer/keyboard (touch-only / device-less); NOT dropping (the compositor does not beacon on touch yet, so a missing beacon here is not input-death)"
          fi
        else
          # The floor painted but never signalled input — never drop below it. cage
          # is the audited never-fail surface; a missing input marker here only
          # means an older floor build that does not write it yet.
          log "floor ('$FLOOR') painted but no input-alive signal within ''${INPUT_TIMEOUT}s — staying on the floor (cannot drop below it)"
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
    # The compositor process is GONE — settle so the kernel reclaims its DRM master
    # on card0 before greetd relaunches the selector and the next tier tries to
    # become master (prevents the EBUSY race on a crash/quick-exit handoff).
    drm_master_settle

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
          rm -f "$HANGMARK" 2>/dev/null || true   # crash-class drop is STICKY (never re-promoted)
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
        dropped one rung IMMEDIATELY (on the FIRST paint-timeout), because a hang is
        DETERMINISTIC: the compositor came up but never painted, and a retry hangs
        the same way. (Only the fast-EXIT crash path requires crashLoopCount
        consecutive crashes; a hang does not — waiting crashLoopCount × this budget
        would mean a ~minute of black screen on real HW before reaching the floor.)
        This catches the "compositor up but shell never paints / never exits"
        failure the bare crash-on-exit detection is blind to (the real-hardware
        pointer-only boot). Set to 0 to disable the watchdog (crash-only
        behaviour). The cage FLOOR is never dropped by this watchdog.
      '';
    };

    inputAliveTimeoutSeconds = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
      description = ''
        Input-alive watchdog budget (seconds) — the INPUT twin of
        shellPaintTimeoutSeconds. After a tier has PAINTED (touched
        /run/hart/session/shell-ready), its compositor must ALSO signal that its
        input pipeline is LIVE by touching /run/hart/session/input-alive within
        this many seconds. If it does NOT — while the compositor process is still
        alive — the tier is treated as INPUT-STARVED (it presents frames but the
        seat/libinput layer delivers no pointer/keyboard events: the real-hardware
        "pointer frozen at 0,0, nothing types" failure, #134, that the paint
        watchdog is blind to) and dropped one rung via the SAME deterministic-hang
        path as a paint timeout.

        DEFAULT 0 = DISABLED (a pure no-op, byte-identical to the paint-only
        behaviour). This MUST stay 0 until the tiers being run actually write
        HART_INPUT_ALIVE_FLAG, because a missing marker is AMBIGUOUS (real
        input-death VS a compositor build that does not emit the marker yet) and
        dropping on a missing WRITER would flap every healthy tier to the floor on
        every boot. Setting it > 0 is the operator's declaration "my tiers write
        this marker, so its absence is genuine input-death." Recommended enabled
        value: comparable to the paint budget (e.g. 25-45s) so a slow-but-fine
        cold-boot input enumeration still signals in time and is not mistaken for a
        dead seat.

        The marker is a STARTUP capability signal (input pipeline wired +
        delivering events into the seat), NOT "the user has interacted" — so an
        idle user is never read as a dead seat (the whole flap-safety of this
        dimension). The cage FLOOR is never dropped by this watchdog (nothing below
        it). Assessed only AFTER a confirmed first paint, so the paint watchdog
        (shellPaintTimeoutSeconds) must be enabled for this to run.
      '';
    };

    inputDeviceProbeCommand = lib.mkOption {
      type = lib.types.str;
      default =
        if pkgs ? libinput
        then "${pkgs.libinput}/bin/libinput list-devices"
        else "";
      defaultText = lib.literalExpression ''
        if pkgs ? libinput then "''${pkgs.libinput}/bin/libinput list-devices" else ""'';
      description = ''
        The command the input-alive watchdog runs to enumerate the seat's input
        device CAPABILITIES before it drops a painted-but-input-starved tier. It
        exists for ONE reason: to tell a GENUINE input-death (a keyboard or pointer
        is attached, so a missing input-alive beacon means the seat is wedged) apart
        from an EXPECTED missing beacon (the box is touchSCREEN-only or has no input
        device at all — the compositor does not emit the beacon on Touch events, so
        the marker's absence is normal, not death). Without this gate, arming the
        watchdog (inputAliveTimeoutSeconds > 0) on a touch-only device would flap a
        perfectly healthy painting tier down to the floor on every boot (FM3b).

        It is invoked WITHOUT arguments and its stdout is scanned for libinput-style
        `Capabilities:` lines: a `keyboard`/`pointer` capability proves a
        beacon-eligible device exists (the drop is justified); a seat that lists ONLY
        `touch` suppresses the drop. The decision is CONSERVATIVE toward the existing
        drop behaviour — an empty/inconclusive probe still allows the drop and falls
        back to the always-present kernel evdev table (/proc/bus/input/devices), so a
        normal keyboard/mouse box (and every CI VM) is never affected; ONLY a
        provably touch-only / device-less seat is spared. This is a pure
        SUPPRESS-the-drop guard: it can never CAUSE a drop, only prevent a wrong one.

        Defaults to `libinput list-devices` (the same enumerator the boot-log
        real-HW probe uses). Empty string -> rely solely on the evdev-table fallback
        (no libinput in the closure). Overridable for an unusual seat or for testing.
        Inert unless inputAliveTimeoutSeconds > 0 (the watchdog itself is off by
        default), and the cage FLOOR is never dropped regardless.
      '';
    };

    tierTermGraceSeconds = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 5;
      description = ''
        Seconds to wait after SIGTERM-ing a HUNG tier's compositor before
        SIGKILL. The grace lets the compositor drop DRM master cleanly
        (drmDropMaster) on its way out; a straight SIGKILL mid-scanout can orphan
        the DRM master on card0, so the NEXT tier the supervisor relaunches fails
        drmSetMaster with EBUSY ("device busy") and the ladder stalls on a black
        screen. Bounded so a stuck compositor still dies (then SIGKILL) — it only
        adds to a DROP, never to a healthy boot. 0 = SIGKILL immediately (the old
        behaviour; the nixosTest fakes set it low for speed).
      '';
    };

    drmMasterSettleSeconds = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 2;
      description = ''
        Seconds to pause after a tier's compositor process is GONE (crashed,
        hung-killed, or logged out) before returning to greetd — giving the kernel
        time to reclaim that process's DRM master + GBM/scanout state on card0 so
        the next tier can become master cleanly instead of racing the teardown
        (the EBUSY handoff race seen across all tiers on real HW). Small + bounded.
        0 = no settle (the nixosTest VMs have no real DRM master to reclaim, so
        they set it to 0 to stay fast).
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

    # cageCommand is the FLOOR — the tier the supervisor can never drop below and
    # the last thing standing between the user and a blank screen. cageCommand is
    # types.str (not nullOr), but the empty string "" would make
    # `tier_available cage` false at runtime, the floor would be unlaunchable, and
    # the selector hits `FATAL: no available session command` → greetd relaunches
    # the selector → fatal loop = the EXACT blank screen this module exists to
    # prevent. A runtime FATAL on a display manager is invisible to the user; catch
    # it at build/eval time instead with an assertion that fails the closure.
    assertions = [
      {
        assertion = sup.cageCommand != "";
        message = ''
          hart.sessionSupervisor.cageCommand must be a non-empty launch command:
          it is the never-fail FLOOR the supervisor can never drop below. An empty
          string makes the floor unlaunchable → the selector would loop on
          "FATAL: no available session command" = a blank screen, the precise
          failure this module prevents. Set it to the cage session launcher (the
          default "hart-shell-session").
        '';
      }
    ];

    # The latch + crash-window live under the existing hart state dir; the
    # tmpfs run dir holds the node_watchdog "unhealthy" signal flag.
    systemd.tmpfiles.rules = [
      # 0770 (group-writable), NOT 0750: the selector wrapper runs as hart-admin (in
      # the `hart` GROUP, not the `hart` OWNER) and MUST WRITE the session-tier latch
      # + the crash-window file here. At 0750 the group has only r-x, so every latch /
      # window write failed "Permission denied" and a tier-drop could NEVER persist —
      # the selector kept re-attempting hart-comp forever, the real-HW BOOT LOOP.
      # Confirmed in the boot journal: "hart-session-selector: line 133:
      # /var/lib/hart/session-tier.window: Permission denied". (Mirrors the same
      # group-write reason /run/hart/session below is already 0770 for the shell host.)
      #
      # DETERMINISM: hart-base.nix declares the IDENTICAL `d /var/lib/hart 0770`
      # rule (via its ${cfg.dataDir} default). Identical tmpfiles rules de-dupe
      # cleanly, so the mode is deterministic. This RESOLVES the former conflict —
      # hart-base used to set 0750 here while this module set 0770, and which mode
      # won was decided by tmpfiles file/line ordering (nondeterministic); a 0750
      # win silently reinstated the boot loop. We keep this rule (not removed) as a
      # belt-and-suspenders so the latch dir still exists at 0770 even if dataDir is
      # ever customized away from /var/lib/hart. Do NOT revert hart-base to 0750.
      "d /var/lib/hart 0770 hart hart -"
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

    # ── SEAT / DRM access for the greetd-launched compositor (real-HW root fix) ──
    # On real hardware the tier compositors (hart-comp/sway/cage) all failed to come
    # up with "permission denied" (/dev/dri or /dev/input EACCES) or "device busy"
    # (could not become DRM master, EBUSY) and the boot LOOPED with a frozen cursor at
    # 0,0 (DRM master was intermittently granted — the compositor drew its cursor —
    # but libinput devices never opened, so no pointer motion ever arrived). The seat
    # access pieces that make the ladder actually scan out on bare metal:
    #
    #   1. systemd-logind is THE seat manager (it always runs on a systemd box) and it
    #      owns the seat + the VTs. The greetd-launched compositor acquires the seat's
    #      DRM master + libinput devices through libseat's LOGIND backend
    #      (LIBSEAT_BACKEND=logind, forced on the greetd session below) — the
    #      canonical greetd-on-systemd path. NixOS's greetd PAM sets startSession=true
    #      (pam_systemd), so greetd's session IS a full ACTIVE logind graphical
    #      session on the seat, which is exactly what libseat-logind needs to
    #      TakeDevice the seat's DRM + input. This is how cage worked BEFORE the
    #      supervisor; every tier now rides the SAME proven path.
    #   2. hart-admin in video/render/input (hart-base.nix) — direct device-node
    #      access for /dev/dri (KMS+GPU) and /dev/input (libinput), belt-and-
    #      suspenders alongside the logind broker.
    #   3. greetd on its OWN VT (vt=7, below) so its session is the seat's ACTIVE
    #      session — the precondition for logind to grant DRM master on the seat.
    #
    # WHY NOT force seatd (the real-HW regression THIS corrects): an earlier fix
    # (c6899df4) forced LIBSEAT_BACKEND=seatd on the false premise that "greetd's
    # session is not a full logind session". It IS, on NixOS. Forcing seatd while
    # systemd-logind is ALSO managing the seat/VTs is two seat managers fighting —
    # the exact boot loop (DRM grabbed but input dead + EBUSY tier-drops, the cursor
    # stuck at 0,0). It "passed" the VM nixosTest only because a QEMU guest's trivial
    # single-VT seat never exposes the contention. seatd stays ENABLED below purely as
    # an idle fallback (it keeps the `seat` group valid and is available for a future
    # logind-less topology); with the env forcing logind, NO client ever connects to
    # seatd, so the idle daemon never touches the seat. (See the command comment.)
    services.seatd.enable = true;
    # ── The selector USER must be able to PERSIST a tier drop (never-black guard) ──
    # greetd runs the selector as hart-admin (below). The latch dir /var/lib/hart is
    # 0770 hart:hart (group-writable, declared above), so the selector can WRITE the
    # session-tier latch on a drop ONLY if hart-admin is in the `hart` GROUP (a group
    # member, not the owner). hart-base.nix already grants it, but the supervisor
    # declares EVERY access ITS selector needs so a future drift in hart-base's group
    # list can never silently re-introduce the real-HW BOOT LOOP: a drop that cannot
    # persist (EPERM on the latch) leaves the selector re-attempting the SAME top tier
    # forever — the exact 0750-vs-0770 / missing-group failure the latch-dir comment
    # above documents. `seat` (libseat/seatd) + `hart` (latch write) are both required;
    # identical supplementary-group entries de-dupe cleanly with hart-base's list.
    users.users.hart-admin.extraGroups = [ "hart" "seat" ];

    # ── Plymouth / fbcon must RELEASE DRM master before the compositor claims it ──
    # The boot splash (boot.plymouth, desktop.nix) holds DRM master on card0; if it
    # is still up when the first tier launches, the compositor's drmSetMaster fails
    # EBUSY. NixOS wires `plymouth-quit.service` to stop the splash, but by default
    # it is ordered only relative to the display-manager target — under greetd we
    # make the dependency explicit so the splash is GONE (DRM master released)
    # before greetd grabs the seat. `plymouth-quit-wait.service` blocks until
    # plymouthd has actually exited, so After it = the KMS scanout is free. Guarded
    # with an mkIf existence check so a node WITHOUT plymouth (the nixosTest VMs)
    # never references a missing unit.
    systemd.services.greetd = {
      after = [ "plymouth-quit-wait.service" ];
      # Don't let a stuck plymouth-quit BLOCK the login forever — it's an ordering
      # preference, not a hard requirement (a missing/failed splash must still let
      # the seat come up). `wants` (not `requires`) keeps greetd starting even if
      # the splash unit is absent or fails.
      wants = [ "plymouth-quit-wait.service" ];
    };

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
    #
    # vt = 7: run the greeter on its OWN VT, OFF the recovery range (tty2..tty6
    # stay free getty consoles — desktop.nix's Ctrl+Alt+F-key escape) and off
    # tty1 (where the boot console / plymouth lives). greetd activating its own
    # VT is what makes its session the seat's ACTIVE session, which is the
    # precondition for the compositor to legally hold DRM master on that seat.
    services.greetd = {
      enable = true;
      vt = 7;
      settings = {
        default_session = {
          # Wrap the selector so the whole session tree inherits LIBSEAT_BACKEND
          # =logind — the systemd-logind libseat backend, the canonical path for a
          # greetd-launched compositor on a systemd box. greetd's PAM sets
          # startSession=true (pam_systemd), so greetd's session IS a full ACTIVE
          # logind graphical session on the seat — exactly what libseat-logind needs
          # to TakeDevice the seat's DRM + input. (env(1) keeps this DRY: one wrapper,
          # every tier inherits.)
          #
          # MUST be forced explicitly, not left to probe: libseat tries the seatd
          # backend FIRST when a seatd SOCKET exists (services.seatd.enable runs the
          # daemon), so without this override every tier would silently pick seatd —
          # the dual-seat-manager fight with logind that froze input + EBUSY-looped
          # the boot on real HW. Forcing logind pins the single, proven seat manager.
          command = "${pkgs.coreutils}/bin/env LIBSEAT_BACKEND=logind ${selectorScript}";
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
