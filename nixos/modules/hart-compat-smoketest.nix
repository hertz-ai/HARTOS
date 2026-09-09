{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — cross-OS runtime smoke-test (MEASURE foreign-OS app support, not claim)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY:
#   HART OS advertises that it runs Windows (.exe), Android (.apk), macOS (.app)
#   and Linux apps natively (hart-subsystems.nix). Historically the AppInstaller
#   reported some of those runtimes as "available" UNCONDITIONALLY — a CLAIM, not
#   a MEASUREMENT (the GNOME-hybrid capability audit found Wine success=True
#   unconditional, Android = `exec sleep infinity`). This module turns the claim
#   into a per-runtime FACT: after boot it actually EXECUTES a tiny test command
#   inside each enabled runtime and writes an HONEST status to a file the UI /
#   operator can read.
#
#   /run/hart/compat-status holds one `key=value` line per runtime, e.g.
#     windows=ok          ← `wine cmd /c echo HARTOK` actually printed HARTOK
#     android=ready       ← AOSP image present, no running session (a real launch
#                            would start it — we do NOT force-boot AOSP here)
#     android=no-image    ← hart-waydroid-init hasn't downloaded the image yet
#     macos=failed        ← darling is installed but the test exec did not succeed
#     linux=ok            ← native (always)
#     flatpak=ok          ← `flatpak --version` ran
#     appimage=skip       ← appimage-run not on PATH (subsystem disabled)
#     fs_ntfs=ok          ← the running kernel can mount NTFS (#145 storage interop)
#     fs_exfat=missing    ← no exFAT driver on THIS kernel (honest real-HW readout)
#   Each line is also echoed to the journal:
#     [hart-compat-smoketest] windows = ok
#   so `journalctl -b -u hart-compat-smoketest` shows the verdicts on a real boot.
#
# HONEST SCOPE — this is a REAL-EXEC SMOKE TEST, not a claim and not a full app:
#   It runs the lightest possible "did the runtime actually execute MY code"
#   probe per runtime (echo HARTOK), under a `timeout`, and classifies by whether
#   HARTOK came back. It does NOT install or run a real app, render a window, or
#   exercise graphics — a runtime that prints HARTOK from a shell could still fail
#   a heavy GUI app. But `ok` PROVES the translation layer loaded and executed a
#   foreign-OS command, which is exactly the historically-faked signal. The Android
#   probe deliberately does NOT force-boot the (heavy) AOSP container: an image-
#   present-but-not-running state is reported `ready`, not `failed`.
#
# NEVER-BLOCK-THE-DESKTOP + FAIL-SAFE (the never-fail contract):
#   * Runs in PARALLEL with the desktop — wantedBy multi-user.target, NOT
#     `before greetd`. It must NEVER delay first paint. (Wine/Waydroid/Darling
#     cold-start can take many seconds; gating the greeter on that would be a
#     regression.)
#   * `set -uo pipefail` (NOT -e): a probe that hangs/errors records `failed`
#     (or its skip/no-image state) and the script CONTINUES — one bad runtime
#     never aborts the others, and the unit ALWAYS `exit 0` (oneshot +
#     RemainAfterExit + bounded TimeoutStartSec) so it can never fail the boot.
#   * Every probe runs through `probe`, which captures to a FILE rather than a
#     command substitution, so its time bound is actually enforced (Wine's
#     detached daemons inherit a pipe's write end and hold the substitution
#     open long past the kill — see the helper), and the unit's outer
#     TimeoutStartSec is DERIVED from the sum of those bounds so it can never
#     again be smaller than the work it contains.
#   * `command -v <tool>` gates every probe, so the script auto-adapts to which
#     hart.subsystems.* are enabled: an absent tool records `skip` (the runtime
#     simply isn't installed on this build), never a false `failed`.

let
  cfg = config.hart;
  sub = config.hart.subsystems;

  # The honest per-runtime status file. One `key=value` line per runtime. In /run
  # (tmpfs) so it is re-derived every boot — a runtime verdict must never outlive
  # the runtime/image/network state it measured.
  statusFile = "/run/hart/compat-status";

  # ── Probe time budgets, in seconds ──────────────────────────────────────────
  # Every runtime probe runs under its own bound, and the unit's outer
  # TimeoutStartSec is DERIVED from their sum below so the two can never drift
  # apart again.
  #
  # They had drifted, and it cost the entire measurement. The outer bound was
  # hand-written as 360 while the inner bounds summed to 390 (120 wine + 30
  # waydroid-status + 60 waydroid-shell + 120 darling + 30 flatpak + 30
  # fsprobe). A run in which every probe merely reached its OWN limit could not
  # possibly finish inside the unit's limit, so systemd SIGKILLed the unit and
  # /run/hart/compat-status stayed EMPTY: no verdicts at all, which is the one
  # outcome this module exists to prevent. Real HW, 2026-09-10, twice in a row:
  # `Failed with result timeout`, and not a single [hart-compat-smoketest]
  # verdict line in the journal.
  budget = {
    wine = 90;
    waydroidStatus = 20;
    waydroidShell = 40;
    darling = 90;
    flatpak = 20;
    fsprobe = 30;
  };
  probeBudget = lib.foldl' (a: b: a + b) 0 (lib.attrValues budget);

  # Headroom over the probe sum for the script's own plumbing: the prefix
  # mkdir, the post-probe wineserver teardown, and the SIGTERM-then-SIGKILL
  # grace each bound allows. Deliberately generous. The unit is off the boot
  # path (a timer fires it ten minutes in), so a ceiling that is too high costs
  # nothing at all, while one that is too low costs every verdict.
  startTimeout = probeBudget + 150;

  # Tools referenced for the script's OWN plumbing (truncate / grep / echo). The
  # per-runtime RUNTIME tools (wine/waydroid/darling/flatpak/appimage-run) live in
  # the SYSTEM path (/run/current-system/sw/bin) ONLY when their subsystem is
  # enabled — so PATH puts the system path FIRST, then this minimal belt. The
  # iso_real_usb_boot lesson: coreutils/grep are not on the bare unit PATH.
  binPath = lib.makeBinPath (with pkgs; [ coreutils gnugrep ]);

  # ── The cross-OS runtime smoke-test ─────────────────────────────────────────
  # `set -uo pipefail` (NOT -e): a single probe failing must NEVER abort the run —
  # it must RECORD its honest status (failed / skip / no-image / ready) and move
  # on. Every runtime tool is run through the `probe` helper, which bounds it and
  # swallows its exit status, so a HANG or non-zero exit cannot fail the unit; the
  # classification is made purely from the captured text. `command -v` gates each
  # probe so an absent (disabled) subsystem records `skip`, never a false
  # `failed`. The unit always exits 0.
  smokeScript = pkgs.writeShellScript "hart-compat-smoketest" ''
    set -uo pipefail
    # System path FIRST so the per-subsystem runtime tools (wine/waydroid/darling/
    # flatpak/appimage-run) are found when their subsystem is enabled; then the
    # minimal coreutils/grep belt for the script's own plumbing.
    export PATH=/run/current-system/sw/bin:${binPath}''${PATH:+:$PATH}

    STATUS="${statusFile}"
    mkdir -p /run/hart 2>/dev/null || true

    # Truncate first — this is a fresh measurement every boot, never appended to a
    # stale file.
    : > "$STATUS" 2>/dev/null || true

    # record <runtime> <status> — append one honest key=value line + announce it to
    # the journal so a real-HW boot shows exactly what each runtime did.
    record() {
      printf '%s=%s\n' "$1" "$2" >> "$STATUS" 2>/dev/null || true
      echo "[hart-compat-smoketest] $1 = $2" >&2
    }

    # probe <seconds> <command...> -- run one runtime probe under a REAL time
    # bound, leaving its combined output in $PROBE_OUT and its exit status in
    # $PROBE_RC.
    #
    # WHY A FILE AND NOT "$(...)": a command substitution reads the pipe until
    # EOF, and EOF only arrives once EVERY holder of the write end has closed
    # it. Wine forks wineserver and winedevice.exe, which INHERIT that write
    # end and deliberately outlive the `wine` process. So `timeout` killed wine
    # exactly on schedule and the substitution went on blocking on the
    # surviving daemons: the inner bound quietly stopped bounding anything.
    # Measured on real HW 2026-09-10, same prefix, same command, back to back:
    # `timeout 20 wine cmd /c "echo HARTOK"` returned from a substitution after
    # 41s, and from this file redirect after exactly 20s. A file needs no such
    # handshake, so the bound written here is the bound that is enforced.
    #
    # stdin is /dev/null so a probe that decides to prompt cannot sit waiting
    # for an answer, and -k turns an ignored SIGTERM into a SIGKILL instead of
    # letting the probe run on past its bound.
    PROBE_OUT=""
    PROBE_RC=0
    PROBE_TMP="$(mktemp 2>/dev/null || echo /tmp/hart-compat-probe.$$)"
    trap 'rm -f "$PROBE_TMP" 2>/dev/null || true' EXIT
    probe() {
      local secs="$1"; shift
      : > "$PROBE_TMP" 2>/dev/null || true
      timeout -k 5 "$secs" "$@" >"$PROBE_TMP" 2>&1 </dev/null
      PROBE_RC=$?
      PROBE_OUT="$(cat "$PROBE_TMP" 2>/dev/null || true)"
      # 124 = the bound expired; 137 = it had to be SIGKILLed after that. Say so
      # in the journal: a runtime that HANGS and one that fails cleanly are both
      # recorded `failed`, and only this line tells the operator which happened.
      if [ "$PROBE_RC" = 124 ] || [ "$PROBE_RC" = 137 ]; then
        echo "[hart-compat-smoketest] probe exceeded its bound of $secs s: $*" >&2
      fi
    }

    # ── Windows / Wine ─────────────────────────────────────────────────────────
    # REAL exec: run `cmd /c echo HARTOK` through Wine; HARTOK back => the Win32
    # translation layer loaded + executed a Windows command. WINEDLLOVERRIDES skips
    # the mono/gecko auto-download prompts so the probe is NETWORK-FREE, and a
    # dedicated WINEPREFIX under hart-subsystems' /var/lib/hart/wine keeps it off
    # the user's prefix. timeout caps a cold-prefix init so it can't hang the unit.
    if command -v wine >/dev/null 2>&1; then
      mkdir -p /var/lib/hart/wine/smoke 2>/dev/null || true
      export WINEPREFIX=/var/lib/hart/wine/smoke
      export WINEDLLOVERRIDES="mscoree,mshtml="
      probe ${toString budget.wine} wine cmd /c "echo HARTOK"
      WIN_OUT="$PROBE_OUT"
      if printf '%s' "$WIN_OUT" | grep -q 'HARTOK'; then
        record windows ok
      else
        record windows failed
      fi
      # Wine's daemons (wineserver, winedevice.exe) outlive the `wine` process
      # by design, and a lingering wineserver keeps this unit's cgroup
      # populated, so systemd has to SIGKILL it at exit -- which is precisely
      # what the 2026-09-10 journal showed, seven processes at a time. Ask them
      # to leave, under a bound of their own, rather than being shot.
      timeout -k 5 20 wineserver -k >/dev/null 2>&1 </dev/null || true
      unset WINEPREFIX WINEDLLOVERRIDES
    else
      record windows skip
    fi

    # ── Android / Waydroid ───────────────────────────────────────────────────────
    # The AOSP system image is downloaded by hart-waydroid-init (network-dependent).
    #   * image absent              => no-image (init hasn't run / no network yet)
    #   * image present, session up => REAL exec `waydroid shell echo HARTOK`
    #                                  (HARTOK back => ART/Binder executed our code)
    #   * image present, no session => ready (a real app launch WOULD start it; we
    #                                  do NOT force-boot the heavy AOSP container in
    #                                  a smoke test)
    if command -v waydroid >/dev/null 2>&1; then
      if [ -f /var/lib/waydroid/images/system.img ]; then
        probe ${toString budget.waydroidStatus} waydroid status
        WD_STATUS="$PROBE_OUT"
        if printf '%s' "$WD_STATUS" | grep -qi 'RUNNING'; then
          probe ${toString budget.waydroidShell} waydroid shell echo HARTOK
          WD_OUT="$PROBE_OUT"
          if printf '%s' "$WD_OUT" | grep -q 'HARTOK'; then
            record android ok
          else
            record android failed
          fi
        else
          record android ready
        fi
      else
        record android no-image
      fi
    else
      record android skip
    fi

    # ── macOS / Darling ──────────────────────────────────────────────────────────
    # REAL exec: `darling shell echo HARTOK`; HARTOK back => the Darwin translation
    # layer executed a macOS-side command. Experimental + heavy, so capped with a
    # timeout; absent tool (default — macos is opt-in) => skip.
    if command -v darling >/dev/null 2>&1; then
      probe ${toString budget.darling} darling shell echo HARTOK
      MAC_OUT="$PROBE_OUT"
      if printf '%s' "$MAC_OUT" | grep -q 'HARTOK'; then
        record macos ok
      else
        record macos failed
      fi
    else
      record macos skip
    fi

    # ── Linux (native — always ok) ───────────────────────────────────────────────
    # Linux apps run on the host kernel directly; no translation layer to smoke-test.
    record linux ok

    # ── Flatpak ──────────────────────────────────────────────────────────────────
    # `flatpak --version` proves the flatpak runtime is installed + runnable. (A real
    # `flatpak run` needs an installed app + a session bus; --version is the honest
    # "the runtime is here" signal that matches the subsystem-enabled gate.)
    if command -v flatpak >/dev/null 2>&1; then
      probe ${toString budget.flatpak} flatpak --version
      if [ "$PROBE_RC" = 0 ]; then
        record flatpak ok
      else
        record flatpak skip
      fi
    else
      record flatpak skip
    fi

    # ── AppImage ─────────────────────────────────────────────────────────────────
    # appimage-run on PATH => the AppImage launcher is installed (the subsystem is
    # enabled). Running a real .AppImage needs an actual image file, so presence of
    # the launcher is the honest subsystem-enabled signal.
    if command -v appimage-run >/dev/null 2>&1; then
      record appimage ok
    else
      record appimage skip
    fi

    # ── Cross-OS filesystem interop (#145) — real-HW driver readout ──────────────
    # The storage module (hart-storage.nix) installs hart-storage-fsprobe ONLY when
    # hart.storage.enable is on, and `command -v` gates it here so a build WITHOUT
    # the interop set simply records nothing (an honest skip, never a false verdict).
    # The probe is read-only (modinfo / /proc/filesystems — it never LOADS a module
    # and never mounts a disk), so it is safe on every boot; it appends one honest
    # `fs_<name>=ok|missing` line per filesystem to the SAME status file + the
    # journal, so a real-HW boot shows whether THIS kernel can actually mount a
    # plugged NTFS / exFAT / FAT / ext4 / btrfs disk. The list is the canonical
    # hart.storage.filesystems set (single source of truth). Bounded + best-effort:
    # `timeout` + `|| true` so it can never delay or fail this oneshot.
    if command -v hart-storage-fsprobe >/dev/null 2>&1; then
      timeout -k 5 ${toString budget.fsprobe} hart-storage-fsprobe "$STATUS" ${lib.concatStringsSep " " config.hart.storage.filesystems} || true
    fi

    # Always succeed — this is a measurement, never a gate.
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.subsystems.smoketest = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run the post-boot cross-OS runtime smoke-test (hart-compat-smoketest): a
        oneshot that actually EXECUTES a tiny test command inside each ENABLED
        foreign-OS runtime (Windows/Wine, Android/Waydroid, macOS/Darling) plus
        the Linux distribution runtimes (Flatpak, AppImage), and writes an honest
        per-runtime status (`ok` / `failed` / `ready` / `no-image` / `skip`) to
        ${statusFile} (one key=value line per runtime, also echoed to the journal).

        This MEASURES the OS's cross-OS capability instead of letting the installer
        CLAIM it unconditionally. It runs in PARALLEL with the desktop (never
        `before greetd`), each probe is fail-safe (a hang/error records `failed`,
        never aborts), and the unit always succeeds so it can never block or fail
        the boot. Only runtimes whose hart.subsystems.* is enabled are probed (an
        absent tool records `skip`); Android does NOT force-boot the AOSP container
        (image-present-but-idle reports `ready`).

        Set to FALSE to skip the smoke-test entirely (the status file is simply not
        written; nothing else changes).
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (gated on the hart master toggle + the subsystems
  # master toggle + this smoke-test toggle)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && sub.enable && sub.smoketest.enable) {
    # The status file lives under the shared /run/hart (tmpfs). Other consumers
    # rely on this dir at 0750 hart hart (gpu-probe / model-bus / session-supervisor
    # all declare the same rule — tmpfiles de-dupes identical rules).
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
    ];

    # ── The cross-OS smoke-test oneshot — runs IN PARALLEL with the desktop ──────
    # Ordered AFTER hart.target (the runtimes' host services), hart-waydroid-init
    # (so the AOSP image had a chance to download), and network-online (best-effort,
    # for the Waydroid image). It is NOT `before greetd` — it must NEVER delay first
    # paint. It can never block/fail the boot: oneshot + RemainAfterExit + the script
    # always exits 0, and a bounded TimeoutStartSec so even a wedged runtime probe
    # can't wedge boot (the inner per-probe `timeout`s are the first belt).
    # DEFERRED off the boot path (2026-08-14): as a multi-user.target member the
    # smoke-test initialized a cold Wine prefix DURING bring-up -- the real-HW
    # trial showed its start timing out at 360s and winedevice.exe dying by
    # SIGKILL while the desktop was still settling. A smoke-test's value is the
    # STATUS FILE, not its timing; running 10 minutes after boot frees boot-time
    # CPU/thermals and reports the same truth. The timer (below) is the only
    # activation; probes and status semantics are unchanged.
    systemd.timers.hart-compat-smoketest = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "10min";
        AccuracySec = "1min";
      };
    };
    systemd.services.hart-compat-smoketest = {
      description = "HART OS — cross-OS runtime smoke-test (writes honest per-runtime status to ${statusFile})";
      after = [ "hart.target" "hart-waydroid-init.service" "network-online.target" ];
      # network-online is WANTED (best-effort) not REQUIRED — a no-network boot must
      # still run the smoke-test (it just reports android=no-image etc. honestly).
      wants = [ "network-online.target" ];
      # A nixos-rebuild switch must not re-run the smoke-test mid-session.
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "hart";
        ExecStart = "${smokeScript}";
        # DERIVED from the per-probe budgets, never hand-written: see the
        # `budget` attrset for what a hand-written 360 against an inner sum of
        # 390 did to this measurement. The outer belt still exists for the same
        # reason it always did, to cap a pathological hang OUTSIDE any single
        # probe's bound, but it can no longer be smaller than the work it is
        # meant to contain.
        TimeoutStartSec = toString startTimeout;
      };
    };
  };
}
