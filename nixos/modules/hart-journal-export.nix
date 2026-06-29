{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS - Journal export to an EXTERNAL removable USB stick
# ═══════════════════════════════════════════════════════════════
#
# THE problem this solves:
#   hart-boot-log.nix lands the boot journal on a HARTLOG partition that lives in
#   the live boot stick's OWN free space. But the failure we most need to debug -
#   the software-rendered glass shell pegging the CPU so typing lags ~500ms and
#   the compositor / in-shell terminal eventually wedge - leaves the user unable
#   to copy ANYTHING out (no working terminal, a frozen shell). And HARTLOG only
#   exists if the live stick had trailing free space + the carve succeeded.
#
#   The dead-simple field recovery a user CAN always do is: plug in a SECOND,
#   ordinary FAT32 USB stick. This module watches for exactly that - any plugged
#   in removable vfat stick that is NOT the live boot medium - and dumps the
#   current-boot journal onto it on a short timer + at shutdown, so the journal
#   walks out of the machine on a stick the user already knows how to read on
#   Windows/macOS/Linux. No TTY, no working compositor required.
#
# THE design (why it is a low-level systemd unit, INDEPENDENT of the shell):
#   A systemd TIMER (every intervalSeconds, ~15s) fires a oneshot that captures
#   the journal regardless of whether the compositor / glass shell is alive. Because
#   it rides the timer + journald + the block layer ONLY (it is ordered after
#   local-fs + systemd-journald, NOT after any graphical target), it KEEPS dumping
#   even when the shell hangs - so the export captures the PRE-hang state leading
#   up to the wedge, not just a post-mortem. A second oneshot captures once more on
#   the way DOWN at shutdown.
#
# WHAT it writes, and WHERE:
#   To  hart-journal-<hostname>.txt  on the eligible stick:
#     - a small header (phase, time, hostname, boot id, and the GPU render verdict
#       read from /run/hart/gpu-render - software vs hardware is THE context for
#       the CPU-pegging-while-software-rendered lag; we REUSE hart-gpu-probe's
#       existing signal, we do NOT add a second probe).
#     - `journalctl -b -p warning -n 200`  - the last 200 warning-and-above lines
#       (the quick "what went wrong" summary a human reads first).
#     - `journalctl -b --no-pager`  - the FULL current-boot journal, capped at
#       ~5 MB so a runaway log can never fill a small stick.
#
# THE never-touch / never-block contract (read carefully):
#   - It NEVER writes to the live boot medium. The boot stick's vfat EFI partition
#     and its HARTLOG diagnostic partition are explicitly EXCLUDED, and the whole
#     disk that carries the ISO (volume label HART_OS) + the disks backing / and
#     /nix/store are excluded too. Only a removable vfat partition on a DIFFERENT
#     disk is ever a target - so an internal EFI partition / the boot stick are
#     never clobbered.
#   - NO eligible stick present  ->  a clean NO-OP (one log line, exit 0). It never
#     blocks, slows, or fails boot/shutdown.
#   - It MOUNTS rw, writes, fsyncs, and ALWAYS unmounts after each write (unlike
#     hart-boot-log, which keeps its dedicated HARTLOG mounted): this stick is a
#     user-removable second stick that can be yanked at any moment, so we never
#     hold the mount across ticks - a yanked stick must not leave a wedged mount.
#   - `set -u` only (NOT -e): a probe failing must never abort the dump - we want a
#     partial dump from a half-wedged system, and every probe is `|| true`-guarded.
#
# VM/HW-gated: the real "a user plugs in a second FAT32 stick on bare metal and
# the journal lands on it" needs a real flash + boot + a second USB. The
# structural/behavioural nixosTest (tests/journal-export.nix) attaches a spare
# disk, formats it vfat with a NON-boot label, runs the REAL export script, and
# asserts hart-journal-<host>.txt lands with the journal sections - AND that a
# HART_OS/HARTLOG-labelled disk is EXCLUDED (the never-clobber-the-boot-stick
# invariant). It proves every link short of a physical second stick.

let
  cfg = config.hart;
  jx = config.hart.journalExport;

  # Where the target stick is mounted while we write (a private mountpoint, never
  # user-facing). Created by tmpfiles below; always unmounted after each write.
  mnt = "/run/hart/journal-mnt";

  # The labels that identify the LIVE BOOT MEDIUM - never a target.
  #   isoLabel    : the ISO9660 volume label desktop.nix sets (isoImage.volumeID).
  #   bootLogLabel: hart-boot-log's HARTLOG partition (single source of truth - we
  #                 import it so the exclusion can never drift from the writer).
  isoLabel     = jx.isoLabel;
  bootLogLabel = config.hart.bootLog.label;

  # The GPU render verdict hart-gpu-probe writes (hardware|software). REUSED here
  # as pure CONTEXT in the dump header - we read the existing signal, we never run
  # a second GPU probe.
  gpuRenderFile = "/run/hart/gpu-render";

  # Every tool referenced by absolute store path - the unit PATH is minimal and
  # several of these (lsblk/blkid/findmnt/findfs) are NOT on it (the
  # iso_real_usb_boot lesson: awk/lspci/xxd/curl were off the minimal unit PATH).
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux systemd gnugrep gawk
  ]);

  # ── The journal-export capture script ─────────────────────────────────────
  # Pure POSIX sh. `set -u` only (NOT -e): a probe failing must NEVER abort the
  # dump. Called by the timer-driven + shutdown units with a $PHASE arg
  # ("periodic" / "shutdown" / "manual") stamped into the header.
  captureScript = pkgs.writeShellScript "hart-journal-export" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    PHASE="''${1:-periodic}"
    ISO_LABEL="${isoLabel}"
    BOOTLOG_LABEL="${bootLogLabel}"
    MNT="${mnt}"
    GPU_FILE="${gpuRenderFile}"
    JOURNAL_CAP_BYTES=5000000

    log() { echo "[hart-journal-export] $*" >&2 ; }

    # ── A filename-safe hostname for hart-journal-<hostname>.txt ──
    HOST=$(cat /proc/sys/kernel/hostname 2>/dev/null | tr -cd 'A-Za-z0-9._-') || HOST=""
    [ -n "$HOST" ] || HOST="hart"
    BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null | tr -d '-' | cut -c1-12) || BOOT_ID="unknown"
    STAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null) || STAMP="?"
    GPU=$(cat "$GPU_FILE" 2>/dev/null | tr -cd 'a-z') || GPU=""
    [ -n "$GPU" ] || GPU="unknown"

    # parent_disk <dev> -> echoes the whole-disk /dev path (partition -> its disk;
    # whole disk -> itself). Empty output + return 1 on failure.
    parent_disk() {
      _d="$1"
      [ -n "$_d" ] || return 1
      _pk=$(lsblk -ndo pkname "$_d" 2>/dev/null | head -n1) || _pk=""
      if [ -n "$_pk" ]; then printf '/dev/%s' "$_pk"; else printf '%s' "$_d"; fi
      return 0
    }

    # ── Build the EXCLUDE set: whole-disks we must NEVER write to. ──
    # Newline-separated /dev/<disk> entries. The live ISO medium + its HARTLOG
    # partition + the disks backing the running system.
    EXCLUDE=""
    add_exclude() {  # add_exclude <dev>
      _x=$(parent_disk "$1") || _x=""
      if [ -n "$_x" ]; then
        EXCLUDE="$EXCLUDE
$_x"
      fi
    }

    # (a) the live medium (HART_OS volume label) + the HARTLOG diag partition.
    for L in "$ISO_LABEL" "$BOOTLOG_LABEL"; do
      [ -n "$L" ] || continue
      d=$(blkid -L "$L" 2>/dev/null) || d=""
      [ -n "$d" ] && add_exclude "$d"
    done
    # (b) the disks that back the running system (root / nix store / live mounts).
    for mp in / /nix/store /nix/.ro-store /iso /run/initramfs/live; do
      [ -d "$mp" ] || continue
      s=$(findmnt -n -o SOURCE --target "$mp" 2>/dev/null | head -n1) || s=""
      case "$s" in
        /dev/*) add_exclude "$s" ;;
      esac
    done

    is_excluded() {  # is_excluded <disk> -> 0 (true) if in the exclude set
      printf '%s\n' "$EXCLUDE" | grep -qxF "$1"
    }

    # ── Enumerate vfat partitions; keep the removable, non-boot ones. ──
    # NAME/TYPE/FSTYPE/PKNAME carry no spaces, so a plain read loop is safe (we do
    # NOT key off LABEL, which can contain spaces - exclusion is by DISK + the
    # removable gate, both space-free). Collect into a temp file so the consuming
    # while-loop runs in THIS shell (a pipe-fed loop is a subshell + loses state).
    # Each row: "<dev> <pkname-or-empty> <force>"  (force=1 skips the removable gate).
    LISTF=$(mktemp 2>/dev/null) || LISTF="/tmp/hart-jx-list.$$"

    # TEST SEAM (never set in production): HART_JOURNAL_TEST_DEVICE lets the
    # nixosTest (tests/journal-export.nix) point the export at a stand-in spare disk
    # - a VM's virtio disk is NOT flagged removable, so the removable gate below
    # would refuse it. When set to a block device, it becomes the SOLE candidate
    # with force=1 (the removable gate is bypassed) - but the never-touch-the-boot-
    # medium EXCLUDE check below STILL applies, so the test can also prove a
    # HART_OS/HARTLOG-labelled disk is refused. Read ONLY here, documented as a test
    # hook so production behaviour is unchanged (no unit/config sets it).
    # Rows are space-separated "<dev> <pkname-or-dash> <force>" - ALWAYS 3 fields
    # ("-" stands in for an empty pkname, e.g. a whole-disk vfat with no partition
    # table, so `read -r DEV PK FORCE` never field-shifts the force flag).
    TEST_DEV="''${HART_JOURNAL_TEST_DEVICE:-}"
    if [ -n "$TEST_DEV" ] && [ -b "$TEST_DEV" ]; then
      _tpk=$(lsblk -ndo pkname "$TEST_DEV" 2>/dev/null | head -n1) || _tpk=""
      [ -n "$_tpk" ] || _tpk="-"
      printf '%s %s 1\n' "$TEST_DEV" "$_tpk" > "$LISTF" 2>/dev/null || true
      log "TEST SEAM: HART_JOURNAL_TEST_DEVICE=$TEST_DEV (removable gate bypassed; exclude check still applies)"
    else
      lsblk -lnpo NAME,TYPE,FSTYPE,PKNAME 2>/dev/null \
        | gawk '$2=="part" && $3=="vfat"{ pk=($4==""?"-":$4); print $1" "pk" 0" }' \
        > "$LISTF" 2>/dev/null || true
    fi

    WROTE=0
    SEEN=0
    while read -r DEV PK FORCE; do
      [ -n "$DEV" ] || continue
      [ -b "$DEV" ] || continue
      SEEN=$((SEEN + 1))

      # Resolve the candidate's whole disk ("-" pkname = a whole-disk fs).
      if [ -n "$PK" ] && [ "$PK" != "-" ]; then DISK="/dev/$PK"; else DISK=$(parent_disk "$DEV") || DISK=""; fi
      [ -n "$DISK" ] || continue

      # NEVER the live boot medium / a system disk (applies even to the test seam).
      if is_excluded "$DISK"; then
        log "skip $DEV (on excluded boot/system disk $DISK)"
        continue
      fi

      # MUST be removable/USB: the parent disk is RM=1, OR hot-pluggable, OR a USB
      # transport. An internal SATA/NVMe (with e.g. a vfat EFI partition) is RM=0 /
      # not-hotplug / nvme-or-sata transport -> refused. The test seam (force=1)
      # bypasses ONLY this gate (a VM disk is not flagged removable).
      if [ "$FORCE" != "1" ]; then
        RM=$(lsblk -ndo RM "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || RM=""
        HOT=$(lsblk -ndo HOTPLUG "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || HOT=""
        TRAN=$(lsblk -ndo TRAN "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || TRAN=""
        if [ "$RM" != "1" ] && [ "$HOT" != "1" ] && [ "$TRAN" != "usb" ]; then
          log "skip $DEV (parent $DISK not removable/USB: RM=$RM HOTPLUG=$HOT TRAN=$TRAN)"
          continue
        fi
      else
        RM="forced"; HOT="forced"; TRAN="forced"
      fi

      log "eligible external USB target: $DEV (disk $DISK, RM=$RM HOTPLUG=$HOT TRAN=$TRAN)"

      # ── Mount rw (vfat). If a stale mount is held, drop it first. ──
      mkdir -p "$MNT" 2>/dev/null || true
      if mountpoint -q "$MNT" 2>/dev/null; then
        umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true
      fi
      if ! mount -t vfat -o rw,flush,umask=0000 "$DEV" "$MNT" 2>/dev/null; then
        if ! mount -o rw "$DEV" "$MNT" 2>/dev/null; then
          log "could not mount $DEV at $MNT - skipping it (others still tried)"
          continue
        fi
      fi

      OUT="$MNT/hart-journal-$HOST.txt"

      # Build off-stick first (tmpfs), then a single copy + fsync lands it.
      TMP=$(mktemp 2>/dev/null) || TMP="/tmp/hart-journal.$$"
      {
        echo "============================================================"
        echo " HART OS journal export"
        echo "   phase    : $PHASE"
        echo "   written  : $STAMP (UTC)"
        echo "   hostname : $HOST"
        echo "   boot_id  : $BOOT_ID"
        echo "   gpu      : $GPU  (from $GPU_FILE: hardware|software)"
        echo "   target   : $DEV  (disk $DISK)"
        echo "   note     : full journal capped at ~$JOURNAL_CAP_BYTES bytes"
        echo "============================================================"
        echo ""
        echo "----- journalctl -b -p warning -n 200 (warnings and above) -----"
        journalctl -b -p warning -n 200 --no-pager 2>/dev/null || echo "(journalctl warnings unavailable)"
        echo ""
        echo "----- journalctl -b --no-pager (full current boot, capped) -----"
        journalctl -b --no-pager 2>/dev/null | head -c "$JOURNAL_CAP_BYTES" || true
        echo ""
        echo "===== end of export (phase=$PHASE) ====="
      } > "$TMP" 2>&1 || true

      cp -f "$TMP" "$OUT" 2>/dev/null || log "write of $OUT failed"
      rm -f "$TMP" 2>/dev/null || true

      # fsync so a yank right after the write still keeps the bytes (FAT has no
      # journal; `sync` flushes the page cache to the device).
      sync "$MNT" 2>/dev/null || sync || true

      # ALWAYS unmount: this is a user-removable second stick; never hold the mount
      # across ticks (a yanked-while-mounted stick would wedge).
      umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true

      log "wrote $OUT to $DEV (phase=$PHASE)"
      WROTE=$((WROTE + 1))
    done < "$LISTF"
    rm -f "$LISTF" 2>/dev/null || true

    if [ "$WROTE" -eq 0 ]; then
      log "no eligible external USB stick (saw $SEEN vfat partition(s)) - clean no-op (phase=$PHASE)"
    fi
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.journalExport = {
    enable = lib.mkEnableOption ''
      journal export to an EXTERNAL removable USB stick. When an ordinary FAT32
      USB stick (NOT the live boot medium) is plugged in, HART OS dumps the
      full current-boot journal + the last 200 warning-and-above lines to
      hart-journal-<hostname>.txt on it, on a short timer and at shutdown. The
      capture runs as a low-level systemd unit INDEPENDENT of the shell /
      compositor, so it keeps exporting even when the glass shell hangs (it
      captures the pre-hang state). A pure NO-OP when no eligible external stick
      is present, and it NEVER writes to the live boot medium (the HART_OS ISO
      disk + the HARTLOG partition + the disks backing / and /nix/store are
      excluded), so it can never clobber the boot stick or block boot/shutdown'';

    intervalSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 15;
      description = ''
        The periodic export interval (seconds). Short by design: the
        software-render CPU-peg can wedge the shell within seconds, so a frequent
        tick captures the pre-hang state on the external stick. Default 15s
        (frequent enough to catch the slide into a hang, infrequent enough not to
        thrash a slow USB stick). The capture always unmounts after each tick, so
        the stick is safe to remove between ticks.
      '';
    };

    isoLabel = lib.mkOption {
      type = lib.types.str;
      default = "HART_OS";
      description = ''
        The ISO9660 volume label of the live boot medium (desktop.nix sets
        isoImage.volumeID = "HART_OS"). The whole disk carrying this label is
        EXCLUDED as an export target so the boot stick is never written to. Keep
        in lockstep with isoImage.volumeID. (The HARTLOG partition is excluded
        separately via hart.bootLog.label - a single source of truth.)
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled OR no external stick present)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && jx.enable) {

    # Private mountpoint for the external stick (tmpfs /run, never persisted).
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
      "d ${mnt}    0755 root root -"
    ];

    # ── PERIODIC export (the live, keeps-dumping-through-a-hang exporter) ──────
    # The oneshot the timer drives. Ordered after the journal + block layer ONLY -
    # NOT after any graphical target - so it runs independent of the shell /
    # compositor and keeps capturing the pre-hang state. Best-effort: never gates
    # anything, nothing waits on it.
    systemd.services.hart-journal-export = {
      description = "HART OS - export the boot journal to an external removable USB stick";
      after = [ "local-fs.target" "systemd-journald.service" ];
      # A nixos-rebuild switch must not re-run a one-shot capture mid-session.
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${captureScript} periodic";
        # A slow USB stick must not wedge anything - bounded + best-effort.
        TimeoutStartSec = "90s";
      };
    };

    systemd.timers.hart-journal-export = {
      description = "HART OS - periodic external-USB journal-export timer (survives a shell hang)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        # First tick soon after boot, then every intervalSeconds.
        OnBootSec = "15s";
        OnUnitActiveSec = "${toString jx.intervalSeconds}s";
        # No Persistent - purely a live exporter; a missed tick while powered off
        # is meaningless.
        AccuracySec = "2s";
      };
    };

    # ── SHUTDOWN export (final journal on the way down) ───────────────────────
    # RemainAfterExit + ExecStop is the systemd idiom for "do work at shutdown".
    # Ordered before shutdown/umount so its ExecStop runs while the block layer +
    # journald still exist.
    systemd.services.hart-journal-export-shutdown = {
      description = "HART OS - shutdown-time journal export to an external removable USB stick";
      wantedBy = [ "multi-user.target" ];
      before = [ "shutdown.target" "umount.target" ];
      after = [ "local-fs.target" ];
      conflicts = [ "shutdown.target" ];
      # Don't let a nixos-rebuild switch stop+restart this (which would fire the
      # ExecStop capture mid-session); it is a shutdown-only hook.
      restartIfChanged = false;
      stopIfChanged = false;
      unitConfig = {
        DefaultDependencies = false;
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

    # The export binary on PATH so an operator can also run it by hand from a
    # recovery TTY: `hart-journal-export manual`.
    environment.systemPackages = [
      (pkgs.runCommand "hart-journal-export-cli" { } ''
        mkdir -p $out/bin
        ln -s ${captureScript} $out/bin/hart-journal-export
      '')
    ];
  };
}
