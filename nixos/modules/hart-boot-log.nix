{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Persistent Boot-Diagnostic Log Partition
# ═══════════════════════════════════════════════════════════════
#
# THE problem this solves:
#   The HART OS live ISO's journal is in tmpfs (RAM) — it is WIPED on every
#   reboot and never lands on the USB stick, so a Windows host cannot read it
#   to debug a boot. Worse, the failure we most need to debug — the GTK4/Tier-1
#   "boots to only a mouse pointer" paint hang — leaves the compositor UP but
#   never settles, so the user can't hand-copy the journal out of a TTY either.
#
# THE fix:
#   If a partition labelled `HARTLOG` (vfat/FAT32) is present on any disk, this
#   module mounts it rw and writes a full diagnostic bundle to it — EARLY in
#   boot, AGAIN on a periodic timer (so a HUNG, never-settling boot still leaves
#   the journal-so-far), and ONCE MORE at shutdown. Each write fsync()s so an
#   abrupt power-off doesn't lose the bundle. FAT32 so the Windows host reads
#   the drive natively: plug the stick into Windows → the HARTLOG drive shows
#   up → open hart-boot-latest.log.
#
#   The flasher (scripts/hart_usb_flasher.py) creates the HARTLOG partition in
#   the stick's free space after a successful flash. So the loop is:
#     flash → boot (even if Tier-1 hangs) → plug into Windows → read the journal.
#
# ROBUSTNESS (the never-block-boot contract):
#   - If NO HARTLOG partition is present, every unit is a clean NO-OP: it logs
#     one line and exits 0. It never blocks, never slows, never fails boot.
#   - The capture is best-effort throughout (`|| true` on every probe) so a
#     missing tool / unavailable subsystem can never fail the unit.
#   - The mount is `nofail`-style (the script does the mount itself, guarded),
#     and the partition is synced + unmounted cleanly after each write.
#
# WHAT IT CAPTURES (the GTK4/Tier-1 paint-hang debug surface, specifically):
#   - `journalctl -b --no-pager` — the FULL current-boot journal.
#   - `systemctl --failed` + `systemctl status 'hart-*'` + their unit journals.
#   - The session-supervisor tier latch + crash window + which tier is active,
#     and the supervisor's own journal (the tier-drop decisions).
#   - Presence/absence + mtime of /run/hart/session/shell-ready (the paint
#     marker) — so "did the shell ever paint?" is answerable offline.
#   - The GTK4 host env + any GSK/GDK/EGL/GBM/WebKit GL errors from the journal,
#     so the GSK_RENDERER=cairo fix (75ba78d) is CONFIRMABLE from the host.
#   - dmesg tail, loginctl/active session, drm_info/the GPU if available.
#
# VM/HW-gated: the "writes the bundle to a real HARTLOG FAT32 partition" claim
# needs a real flash + boot to fully confirm (no HARTLOG partition exists on the
# Windows dev box). The structural test (tests/boot-log.nix) proves the units +
# tooling are in the closure, the no-HARTLOG path is a clean no-op, and the
# capture script parses under POSIX sh.

let
  cfg = config.hart;
  blog = config.hart.bootLog;

  # The label the flasher writes (scripts/hart_usb_flasher.py) — ONE source of
  # truth for the contract. vfat/FAT32 so the Windows host reads it natively.
  label = blog.label;

  # Where the HARTLOG partition is mounted while we write (a private mountpoint,
  # not a user-facing path). Created by tmpfiles below.
  mnt = "/run/hart/bootlog-mnt";

  # The supervisor's latch contract (kept in lockstep with
  # hart-session-supervisor.nix — same paths, read-only here).
  latchFile  = "/var/lib/hart/session-tier";
  windowFile = "/var/lib/hart/session-tier.window";
  readyFlag  = "/run/hart/session/shell-ready";

  # Every tool referenced by absolute store path — the unit PATH is minimal and
  # several of these (lsblk, dmesg, drm_info, loginctl) are NOT on it (the
  # iso_real_usb_boot lesson: awk/lspci/xxd/curl were off the minimal unit PATH).
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux systemd kmod gnugrep gawk
  ]);

  # drm_info is its own package; attr-guarded so a nixpkgs rev lacking it cannot
  # break evaluation (the rustdesk attr-guard pattern from desktop.nix).
  drmInfoBin =
    if pkgs ? drm_info then "${pkgs.drm_info}/bin/drm_info"
    else "";

  # ── The diagnostic-bundle capture script ──────────────────────────────────
  # Pure POSIX sh. `set -u` only (NOT -e): a probe failing must NEVER abort the
  # bundle — we want a PARTIAL bundle from a hung boot, not nothing. Every probe
  # is `|| true`-guarded. The script:
  #   1. Finds a block device with the HARTLOG label (no-op + exit 0 if absent).
  #   2. Mounts it rw vfat at $MNT.
  #   3. Writes the bundle to hart-boot-<short-boot-id>.log AND overwrites the
  #      stable hart-boot-latest.log, fsync'ing each.
  #   4. Syncs + unmounts cleanly.
  # Called by the early-boot, periodic-timer, and shutdown units with a $PHASE
  # arg ("early" / "periodic" / "shutdown") that is stamped in the header.
  captureScript = pkgs.writeShellScript "hart-boot-log-capture" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    PHASE="''${1:-periodic}"
    LABEL="${label}"
    MNT="${mnt}"
    LATCH="${latchFile}"
    WINDOW="${windowFile}"
    READY="${readyFlag}"
    DRM_INFO="${drmInfoBin}"

    log() { echo "[hart-boot-log] $*" >&2 ; }

    # ── 1. Find the HARTLOG partition. NO-OP cleanly if it isn't there. ──
    # blkid is the canonical label→device lookup; findfs is the simplest. If
    # neither finds it, there is no log partition (an old stick / a plain ISO
    # flash without the free-space partition) — exit 0, never fail boot.
    DEV=""
    if command -v findfs >/dev/null 2>&1; then
      DEV=$(findfs LABEL="$LABEL" 2>/dev/null) || DEV=""
    fi
    if [ -z "$DEV" ] && command -v blkid >/dev/null 2>&1; then
      DEV=$(blkid -L "$LABEL" 2>/dev/null) || DEV=""
    fi
    if [ -z "$DEV" ] || [ ! -b "$DEV" ]; then
      log "no '$LABEL' partition present — nothing to write (clean no-op, phase=$PHASE)"
      exit 0
    fi
    log "found '$LABEL' at $DEV (phase=$PHASE)"

    # ── 2. Mount it rw (vfat/FAT32 so the Windows host reads it natively). ──
    mkdir -p "$MNT" 2>/dev/null || true
    # Already mounted from a previous phase this boot? Reuse it.
    if ! mountpoint -q "$MNT" 2>/dev/null; then
      if ! mount -t vfat -o rw,flush,umask=0000 "$DEV" "$MNT" 2>/dev/null; then
        # Retry without an explicit fs type (let the kernel auto-detect) — a
        # quick-formatted stick can surface as vfat under a different probe.
        if ! mount -o rw "$DEV" "$MNT" 2>/dev/null; then
          log "could not mount $DEV at $MNT — skipping (boot continues)"
          exit 0
        fi
      fi
    fi

    BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null | tr -d '-' | cut -c1-12) || BOOT_ID="unknown"
    STAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null) || STAMP="?"
    PER_BOOT="$MNT/hart-boot-$BOOT_ID.log"
    LATEST="$MNT/hart-boot-latest.log"

    # ── 3. Build the bundle into a temp file, then copy to both targets. ──
    # Build off-partition first (in tmpfs) so a slow FAT write doesn't interleave
    # with the probes, then a single copy + fsync lands it atomically-ish.
    TMP=$(mktemp 2>/dev/null) || TMP="/tmp/hart-boot-log.$$"
    {
      echo "════════════════════════════════════════════════════════════"
      echo " HART OS boot diagnostic bundle"
      echo "   phase    : $PHASE"
      echo "   written  : $STAMP (UTC)"
      echo "   boot_id  : $BOOT_ID"
      echo "   hostname : $(cat /proc/sys/kernel/hostname 2>/dev/null || echo '?')"
      echo "   os       : $(cat /etc/os-release 2>/dev/null | grep -m1 PRETTY_NAME || echo '?')"
      echo "════════════════════════════════════════════════════════════"
      echo ""

      echo "───────────── session-supervisor tier state ─────────────"
      # The single most important signal for the Tier-1 paint hang: which tier
      # is latched, the crash window, and whether the shell ever painted.
      if [ -r "$LATCH" ]; then
        echo "latched tier (active on next/this boot): $(cat "$LATCH" 2>/dev/null)"
      else
        echo "latched tier: <absent> (fresh/un-latched boot -> starts at startTier)"
      fi
      if [ -r "$WINDOW" ]; then
        echo "crash window (epoch timestamps, one per fast exit):"
        cat "$WINDOW" 2>/dev/null || true
      else
        echo "crash window: <absent> (no recent crash-loop accounting)"
      fi
      echo ""
      echo "── shell-ready paint marker ($READY) ──"
      if [ -e "$READY" ]; then
        echo "PRESENT — the glass shell signalled its first painted frame"
        echo "  mtime: $(date -r "$READY" -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || stat -c '%y' "$READY" 2>/dev/null || echo '?')"
      else
        echo "ABSENT — the shell NEVER painted a first frame this boot"
        echo "  (= the Tier-1/Tier-2 pointer-only hang; the paint-watchdog should"
        echo "   have dropped a tier. Cross-check the supervisor journal below.)"
      fi
      echo ""

      echo "───────────── systemctl --failed ─────────────"
      systemctl --failed --no-pager --no-legend 2>/dev/null || echo "(systemctl --failed unavailable)"
      echo ""

      echo "───────────── hart-* unit status ─────────────"
      systemctl status 'hart-*' --no-pager 2>/dev/null || echo "(systemctl status hart-* unavailable)"
      echo ""

      echo "───────────── session-supervisor (greetd) journal ─────────────"
      # The tier-drop decisions are logged by the selector wrapper with the
      # [hart-session-supervisor] prefix to greetd's journal.
      journalctl -b --no-pager -u greetd.service 2>/dev/null || echo "(greetd journal unavailable)"
      echo ""
      echo "── tier-drop decisions (grep of the whole boot journal) ──"
      journalctl -b --no-pager 2>/dev/null | grep -i 'hart-session-supervisor\|session tier\|latched\|paint-watchdog\|HUNG' || echo "(no supervisor decision lines)"
      echo ""

      echo "───────────── GTK4 host / GSK / GDK / EGL / GBM / WebKit GL ─────────────"
      # The exact error class the GSK_RENDERER=cairo fix (75ba78d) addresses.
      # Surfacing these here makes the fix CONFIRMABLE from the Windows host: a
      # clean boot shows GSK_RENDERER=cairo in the env dump and NO GL hang lines.
      echo "── GL-relevant env from the glass-shell unit(s) ──"
      systemctl show 'hart-liquid-ui-renderer.service' -p Environment --no-pager 2>/dev/null || true
      ( systemctl cat 'hart-*glass*' --no-pager 2>/dev/null | grep -i 'GSK_RENDERER\|GDK_GL\|LIBGL\|WLR_RENDERER\|WEBKIT_DISABLE\|HardwareAcceleration' ) || true
      echo ""
      echo "── GL/EGL/GBM/GSK/WebKit error lines from the boot journal ──"
      journalctl -b --no-pager 2>/dev/null | grep -iE 'gsk|gdk|egl|gbm|glx|webkit|renderer|dri|drm|wlroots|wayland|software rendering|llvmpipe|MESA|failed to (create|bind|make)' | tail -n 400 || echo "(no GL-class lines)"
      echo ""

      echo "───────────── active session / loginctl ─────────────"
      loginctl --no-pager 2>/dev/null || echo "(loginctl unavailable)"
      echo ""
      loginctl session-status 2>/dev/null || true
      echo ""

      echo "───────────── GPU / DRM ─────────────"
      if [ -n "$DRM_INFO" ] && [ -x "$DRM_INFO" ]; then
        "$DRM_INFO" 2>/dev/null | head -n 200 || echo "(drm_info produced no output)"
      else
        echo "(drm_info not in closure — falling back to sysfs)"
        for d in /sys/class/drm/card*/device/uevent; do
          [ -r "$d" ] && { echo "== $d =="; cat "$d" 2>/dev/null; }
        done
      fi
      echo "── /dev/dri ──"
      ls -l /dev/dri 2>/dev/null || echo "(no /dev/dri — no DRM/KMS node)"
      echo ""

      echo "───────────── dmesg (tail) ─────────────"
      dmesg 2>/dev/null | tail -n 300 || echo "(dmesg unavailable — kernel.dmesg_restrict?)"
      echo ""

      echo "───────────── FULL current-boot journal (journalctl -b) ─────────────"
      # The full journal LAST so a reader hits the curated summary first but the
      # complete record is always present for deep dives.
      journalctl -b --no-pager 2>/dev/null || echo "(journalctl -b unavailable)"
      echo ""
      echo "═══════════════════ end of bundle (phase=$PHASE) ═══════════════════"
    } > "$TMP" 2>&1 || true

    # ── 4. Land it on the FAT32 partition + fsync so a power-off can't lose it. ──
    # Per-boot file (history across this boot's phases) + the stable latest file
    # the Windows host always opens. cp then `sync` the mountpoint's device.
    cp -f "$TMP" "$PER_BOOT" 2>/dev/null || log "write of $PER_BOOT failed"
    cp -f "$TMP" "$LATEST"   2>/dev/null || log "write of $LATEST failed"
    rm -f "$TMP" 2>/dev/null || true

    # fsync the partition so an abrupt power-off after a hung boot still keeps the
    # bytes (FAT has no journal; `sync` flushes the page cache to the device).
    sync "$MNT" 2>/dev/null || sync || true
    log "wrote bundle to $LATEST (+ $PER_BOOT), phase=$PHASE"

    # ── Unmount cleanly so the Windows host sees a consistent filesystem. On the
    # periodic phase we KEEP it mounted (re-mount churn on a slow stick is worse
    # than holding the mount). On every OTHER phase (early/shutdown/manual) we
    # unmount IF it is mounted — regardless of which phase mounted it (a prior
    # periodic tick may hold the mount), so shutdown always leaves a clean fs and
    # an early-boot crash before the first periodic tick still leaves a clean fs.
    if [ "$PHASE" != "periodic" ] && mountpoint -q "$MNT" 2>/dev/null; then
      sync || true
      umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true
    fi
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.bootLog = {
    enable = lib.mkEnableOption ''
      the persistent boot-diagnostic log partition. When a partition labelled
      HARTLOG (FAT32) is present, HART OS writes the full current-boot journal +
      tier-supervisor state + GTK4/GL diagnostics to it early in boot, on a
      periodic timer (so a HUNG boot still leaves a record), and at shutdown — so
      a Windows host can read the boot journal off the stick WITHOUT the user
      hand-copying from a TTY. A pure NO-OP when no HARTLOG partition exists'';

    label = lib.mkOption {
      type = lib.types.str;
      default = "HARTLOG";
      description = ''
        The filesystem LABEL of the diagnostic-log partition. Must match the
        label the flasher (scripts/hart_usb_flasher.py) writes when it creates
        the FAT32 partition in the stick's free space. ONE source of truth for
        the on-stick contract; changing it here requires changing the flasher.
      '';
    };

    intervalSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 20;
      description = ''
        The periodic re-capture interval (seconds). This is what makes a HUNG
        boot debuggable: the Tier-1 pointer-only hang never settles + never
        exits, so only a periodic capture leaves the journal-so-far on the
        stick. Architecture default 20s (frequent enough to catch the hang,
        infrequent enough not to thrash a slow USB stick).
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled OR when no HARTLOG present)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && blog.enable) {

    # Private mountpoint for the HARTLOG partition (tmpfs /run, never persisted).
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
      "d ${mnt}    0755 root root -"
    ];

    # ── EARLY-boot capture ───────────────────────────────────────────────────
    # Fire as early as the journal + a mounted-able block layer exist, so even a
    # boot that hangs seconds later leaves an initial bundle. DefaultDependencies
    # stay on (we WANT it ordered after local-fs/systemd-journald) but it must
    # never gate anything — nothing waits on it.
    systemd.services.hart-boot-log-early = {
      description = "HART OS — early boot diagnostic capture to the HARTLOG partition";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" "systemd-journald.service" ];
      # A nixos-rebuild switch must not re-run a one-shot capture mid-session.
      restartIfChanged = false;
      # Best-effort: never block the boot transaction.
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = false;
        ExecStart = "${captureScript} early";
        # A capture stall (e.g. a very slow USB stick) must not wedge boot.
        TimeoutStartSec = "90s";
      };
    };

    # ── PERIODIC capture (THE hung-boot debugger) ────────────────────────────
    # A monotonic timer that re-captures every intervalSeconds. The Tier-1
    # pointer-only hang never exits, so this periodic tick is the ONLY thing that
    # lands the journal-so-far on the stick. OnBootSec small so the first tick is
    # quick; OnUnitActiveSec = the configured interval.
    systemd.services.hart-boot-log-periodic = {
      description = "HART OS — periodic boot diagnostic capture to the HARTLOG partition";
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${captureScript} periodic";
        TimeoutStartSec = "90s";
      };
    };
    systemd.timers.hart-boot-log-periodic = {
      description = "HART OS — periodic boot-diagnostic capture timer (catches a HUNG boot)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "15s";
        OnUnitActiveSec = "${toString blog.intervalSeconds}s";
        # No Persistent — this is purely a live-boot debugger; a missed tick
        # while powered off is meaningless.
        AccuracySec = "2s";
      };
    };

    # ── SHUTDOWN capture (final state on a clean power-off / reboot) ──────────
    # A oneshot ordered before shutdown.target that runs its ExecStop at
    # shutdown time (RemainAfterExit + ExecStop is the systemd idiom for "do work
    # on the way down"). Captures the final journal so a clean reboot still
    # leaves the last-known-good state on the stick.
    systemd.services.hart-boot-log-shutdown = {
      description = "HART OS — shutdown-time diagnostic capture to the HARTLOG partition";
      wantedBy = [ "multi-user.target" ];
      # Order so its ExecStop runs as the system goes down, before the block
      # layer + journald are torn down.
      before = [ "shutdown.target" "umount.target" ];
      after = [ "local-fs.target" ];
      conflicts = [ "shutdown.target" ];
      # Don't let a nixos-rebuild switch stop+restart this (which would fire the
      # ExecStop capture mid-session); it is a shutdown-only hook.
      restartIfChanged = false;
      stopIfChanged = false;
      unitConfig = {
        DefaultDependencies = false;
        # Stay loaded the whole uptime so the ExecStop fires on the way down.
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # ExecStart is a no-op marker; the real work is ExecStop at shutdown.
        ExecStart = "${pkgs.coreutils}/bin/true";
        ExecStop = "${captureScript} shutdown";
        TimeoutStopSec = "60s";
      };
    };

    # The capture binary + its probe tools on the system PATH (so an operator can
    # also run it by hand from a recovery TTY: `hart-boot-log-capture manual`).
    environment.systemPackages = [
      (pkgs.runCommand "hart-boot-log-cli" { } ''
        mkdir -p $out/bin
        ln -s ${captureScript} $out/bin/hart-boot-log-capture
      '')
    ]
    ++ lib.optional (pkgs ? drm_info) pkgs.drm_info;
  };
}
