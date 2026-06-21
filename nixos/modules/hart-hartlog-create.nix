{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Live-OS self-create of the HARTLOG diagnostic partition
# ═══════════════════════════════════════════════════════════════
#
# THE problem this solves:
#   The HARTLOG FAT32 partition (read by hart-boot-log.nix to land the boot
#   journal on the stick for a Windows host to read) USED to be created by the
#   Windows flasher via diskpart. That path is DOUBLY broken:
#     1. It HANGS on a wedged Windows VDS (the same WMI/Storage wedge that hangs
#        Get-Disk — diskpart `create partition` can block 15+ minutes).
#     2. A half-completed `diskpart create partition` CORRUPTED a freshly-flashed
#        stick's EFI/GPT — the resulting boot failed with
#        `start_image returned 0x8000000000000001` (= EFI_LOAD_ERROR).
#   So creating HARTLOG on Windows risks bricking the very stick we just flashed.
#
# THE fix:
#   Move HARTLOG creation OFF Windows entirely, INTO the Live OS. On first boot
#   from the USB stick, this oneshot:
#     1. Identifies the parent disk the live root was booted from (the device the
#        ISO was written to) — robustly, by walking the mounted live filesystems
#        back to their parent block device.
#     2. Confirms that disk is REMOVABLE/USB (never touches an internal disk).
#     3. Confirms there is no partition already labelled HARTLOG.
#     4. Confirms there is trailing UNPARTITIONED free space at the END of the
#        device.
#     5. Carves ONE new partition into ONLY that trailing free space (sgdisk
#        --largest-new) and FAT32-formats it labelled HARTLOG (mkfs.vfat -n).
#   It runs EARLY (before hart-boot-log's capture), ORDERED BEFORE the boot-log
#   capture units, so the very first boot's journal can already land on HARTLOG.
#
# THE never-touch-the-ISO / never-block-boot contract (read carefully):
#   - NO-OP if the system was NOT booted from a removable/USB device.
#   - NO-OP if a HARTLOG partition already exists (idempotent across reboots —
#     the second boot finds it and exits 0).
#   - NO-OP if there is no trailing free space (the ISO filled the stick).
#   - It NEVER repartitions / reformats / shifts the in-use ISO/EFI/boot
#     partitions. sgdisk `--largest-new` allocates from the EXISTING free pool
#     ONLY (it cannot move or resize an existing partition), and we additionally
#     assert that the free region we will consume starts AFTER the last existing
#     partition's end sector. The EFI/GPT the boot depends on is never rewritten
#     beyond appending one new GPT entry into already-free space.
#   - ANY error (tool missing, sgdisk/mkfs failure, ambiguous disk, unexpected
#     output) is caught + logged, and the unit exits 0. It NEVER fails or blocks
#     boot. The HARTLOG partition is a debug convenience; the OS must boot
#     regardless.
#   - `set -u` only (NOT -e): a probe failing must not abort the script; we
#     decide explicitly at each gate.
#
# VM/HW-gated: "creates HARTLOG in the trailing free space of the real USB the
# ISO was written to" needs a real flash + USB boot to fully confirm (the
# Windows dev box has no USB-booted live root). The structural test
# (tests/hartlog-create.nix) proves the unit + tooling are in the closure,
# the ordering before hart-boot-log is correct, the non-removable / no-free /
# already-exists gates each no-op cleanly, and the carve script parses under sh.

let
  cfg = config.hart;
  hlog = config.hart.hartlogCreate;

  # ONE source of truth for the label, shared with hart-boot-log.nix (which reads
  # by-label) and the flasher. Default to the boot-log module's label so the two
  # always agree; an operator overriding one must override both.
  label = hlog.label;

  # Minimal-PATH discipline (the iso_real_usb_boot lesson: awk/lspci/xxd/curl were
  # OFF the minimal unit PATH). Every tool the script uses by name is here.
  #   gptfdisk  -> sgdisk (GPT partition add, by largest free block)
  #   parted    -> mkpart for the isohybrid MBR (DOS-label) case sgdisk can't carve
  #   dosfstools -> mkfs.vfat (FAT32 format + label)
  #   util-linux -> lsblk, findmnt, blkid, losetup (follow a loop back to its disk),
  #                 partx/udevadm (re-read the table)
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux gptfdisk parted dosfstools gawk gnugrep gnused
  ]);

  createScript = pkgs.writeShellScript "hart-hartlog-create" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    LABEL="${label}"
    # LOUD decision marker (the never-silent-no-op contract). A tmpfs file the
    # operator/host can read to see EXACTLY which disk was picked, the free space
    # found, and WHY a no-op happened — so a silent no-op is never undebuggable
    # again. We also echo every decision to the journal (>&2) AND to the boot
    # console (/dev/console, best-effort) so it's visible even before journald.
    STATUS_DIR="/run/hart"
    STATUS="$STATUS_DIR/hartlog-create.status"
    mkdir -p "$STATUS_DIR" 2>/dev/null || true
    : > "$STATUS" 2>/dev/null || true

    # log: journal + status marker. cecho: ALSO to the boot console.
    log() {
      echo "[hart-hartlog-create] $*" >&2
      echo "$*" >> "$STATUS" 2>/dev/null || true
    }
    cecho() {
      log "$*"
      echo "[hart-hartlog-create] $*" > /dev/console 2>/dev/null || true
    }
    # Record the final outcome line + the picked disk so the marker always ends
    # with an unambiguous verdict (DECISION=...). Call right before every exit.
    decide() {  # decide <CREATED|NOOP|FAIL> <reason>
      echo "DECISION=$1 DISK=''${DISK:-none} FREE_SECTORS=''${FREE_SECTORS:-unknown} REASON=$2" >> "$STATUS" 2>/dev/null || true
      cecho "DECISION=$1 disk=''${DISK:-none} reason=$2"
    }

    DISK=""
    FREE_SECTORS=""

    # ── 0. Tooling sanity. Any missing tool -> clean no-op (never fail boot). ──
    for t in lsblk findmnt blkid sgdisk mkfs.vfat; do
      if ! command -v "$t" >/dev/null 2>&1; then
        decide NOOP "tool '$t' not found"
        exit 0
      fi
    done

    # ── 1. Already have a HARTLOG partition? Idempotent no-op. ──
    # Re-runs on every boot are fine: the second boot finds the partition created
    # on the first and exits cleanly. blkid -L resolves by filesystem label.
    if blkid -L "$LABEL" >/dev/null 2>&1; then
      decide NOOP "a '$LABEL' partition already exists (idempotent)"
      exit 0
    fi

    # ── 2. Find the REAL USB disk the firmware booted (the ISO was written to). ──
    # The live ISO is a hybrid ISO9660; the live root is a squashfs/overlay on a
    # loop/iso9660 mount, so naively walking `/`'s SOURCE to a parent disk fails
    # (it lands on the overlay/tmpfs, no backing block device). We resolve the
    # REAL stick by walking the iso9660/squashfs live mountpoints to their backing
    # SOURCE, then mapping THAT to a whole disk — and, crucially, FOLLOWING a loop
    # device back to the block device its backing file lives on (the isohybrid USB
    # is frequently surfaced to the ISO mount as a loop over /dev/sdX).
    #
    # TEST SEAM (never set in production): HART_HARTLOG_TEST_DISK lets the
    # nixosTest (tests/hartlog-create.nix) point the carve at a stand-in spare
    # disk — the VM's boot disk is NOT the stick, so the live-root walk below
    # wouldn't reach the test disk. When set to a block device, we still apply the
    # SAME free-space/never-touch-existing gates below; it only bypasses the
    # boot-disk auto-detection + the removable gate. Read ONLY here, documented as
    # a test hook so production behaviour is unchanged (no unit/config sets it).
    if [ -n "''${HART_HARTLOG_TEST_DISK:-}" ] && [ -b "''${HART_HARTLOG_TEST_DISK}" ]; then
      DISK="''${HART_HARTLOG_TEST_DISK}"
      cecho "TEST SEAM: HART_HARTLOG_TEST_DISK=$DISK (bypassing auto-detect; safety gates still apply)"
      TEST_DISK=1
    else
      TEST_DISK=0

    # resolve_disk_from_src SRC -> echoes the whole-disk device, following loops.
    # SRC may be: a partition (/dev/sdb1), a whole disk (/dev/sdb), a loop
    # (/dev/loop0, the ISO mounted via loop), or empty. We return the USB WHOLE
    # disk in every resolvable case; empty on failure.
    # NOTE: failure arms print NOTHING (empty stdout) + return 1 — we deliberately
    # avoid printf of an empty single-quoted literal, because a bare double
    # single-quote inside THIS Nix multi-line string would prematurely close it
    # (it is the Nix string terminator). No output == the empty result the callers
    # already test for, so a bare `return 1` is correct + Nix-safe.
    resolve_disk_from_src() {
      _src="$1"
      case "$_src" in
        /dev/loop*)
          # The ISO is loop-mounted: the stick is the disk the loop's BACKING FILE
          # lives on. losetup -nO BACK-FILE gives that file's path; map the file to
          # its filesystem's SOURCE device, then to that device's parent disk.
          _bf=$(losetup -nO BACK-FILE "$_src" 2>/dev/null | head -n1) || _bf=""
          if [ -n "$_bf" ] && [ -e "$_bf" ]; then
            _bsrc=$(findmnt -n -o SOURCE --target "$_bf" 2>/dev/null | head -n1) || _bsrc=""
            case "$_bsrc" in
              /dev/loop*) return 1 ;;                    # loop-on-loop: give up
              /dev/*)
                _bpk=$(lsblk -ndo pkname "$_bsrc" 2>/dev/null | head -n1) || _bpk=""
                if [ -n "$_bpk" ]; then printf '/dev/%s' "$_bpk"; else printf '%s' "$_bsrc"; fi
                return 0 ;;
              *) return 1 ;;
            esac
          fi
          return 1 ;;
        /dev/*)
          # A real partition or whole disk: map to its parent whole disk (pkname),
          # or use it directly if it already IS a whole disk (pkname empty).
          _pk=$(lsblk -ndo pkname "$_src" 2>/dev/null | head -n1) || _pk=""
          if [ -n "$_pk" ]; then printf '/dev/%s' "$_pk"; else printf '%s' "$_src"; fi
          return 0 ;;
        *) return 1 ;;
      esac
    }

    # Walk the live filesystems most-specific-first. The iso9660 / squashfs live
    # mounts carry the REAL backing device; `/` (overlay) is the LAST resort.
    SRC=""
    RESOLVED=""
    for mp in /iso /run/initramfs/live /run/rootfs /nix/.ro-store /; do
      [ -d "$mp" ] || continue
      s=$(findmnt -n -o SOURCE --target "$mp" 2>/dev/null | head -n1) || s=""
      [ -n "$s" ] || continue
      d=$(resolve_disk_from_src "$s") || d=""
      if [ -n "$d" ] && [ -b "$d" ]; then
        SRC="$s"; RESOLVED="$d"; break
      fi
    done

    # Fallback: match the disk that actually CARRIES the ISO by its volume label.
    # desktop.nix sets isoImage.volumeID = "HART_OS", so the iso9660 filesystem is
    # labelled HART_OS — find the block device with that label and take its parent
    # disk. This catches firmwares/initrd shapes where the mount-source walk above
    # lands on an overlay/loop we can't follow.
    if [ -z "$RESOLVED" ]; then
      isodev=$(blkid -L "HART_OS" 2>/dev/null | head -n1) || isodev=""
      if [ -n "$isodev" ]; then
        RESOLVED=$(resolve_disk_from_src "$isodev") || RESOLVED=""
        [ -n "$RESOLVED" ] && SRC="$isodev (by HART_OS label)"
      fi
    fi

    if [ -z "$RESOLVED" ] || [ ! -b "$RESOLVED" ]; then
      decide NOOP "could not resolve the live-boot USB disk (overlay/loop unfollowable, no HART_OS label match)"
      exit 0
    fi
    DISK="$RESOLVED"
    cecho "live root backed by $SRC -> USB disk $DISK"

    # ── 3. The disk MUST be removable/USB. NEVER touch an internal disk. ──
    # RM=1 (removable) OR TRAN=usb. An internal NVMe/SATA disk is RM=0 + TRAN!=usb
    # -> we refuse. This is the single most important safety gate.
    RM=$(lsblk -ndo RM "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || RM=""
    TRAN=$(lsblk -ndo TRAN "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || TRAN=""
    if [ "$RM" != "1" ] && [ "$TRAN" != "usb" ]; then
      decide NOOP "$DISK is not removable/USB (RM=$RM TRAN=$TRAN) — refusing an internal disk"
      exit 0
    fi
    cecho "$DISK is removable/USB (RM=$RM TRAN=$TRAN) — eligible"
    fi  # end of the auto-detect (non-test) branch; the test seam set DISK directly
    # and deliberately skips the removable/USB gate (the VM's spare disk is not
    # flagged removable). $DISK is now set in both branches; safety gates below
    # (free space + never-touch-existing) still apply to both.

    # ── 4. Determine the partition-table TYPE (GPT vs isohybrid MBR/DOS). ──
    # An isohybrid ISO can be written GPT or MBR (DOS). sgdisk only carves GPT;
    # forcing it on an MBR disk would CONVERT the table (destroying the isohybrid
    # boot layout). So branch on the real label type from lsblk PTTYPE / blkid.
    PTTYPE=$(lsblk -ndo PTTYPE "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || PTTYPE=""
    if [ -z "$PTTYPE" ]; then
      PTTYPE=$(blkid -p -s PTTYPE -o value "$DISK" 2>/dev/null) || PTTYPE=""
    fi
    cecho "$DISK partition table type: ''${PTTYPE:-unknown}"

    NEWPART=""
    if [ "$PTTYPE" = "gpt" ]; then
      # ── 4a/5a. GPT path: trailing free space + sgdisk --largest-new. ──
      # sgdisk -p prints the table; "-E" = LAST usable sector, "-f" = FIRST aligned
      # free sector after the last partition. FIRST_FREE >= LAST_USABLE = no tail.
      if ! sgdisk -p "$DISK" >/dev/null 2>&1; then
        decide NOOP "$DISK PTTYPE=gpt but sgdisk -p failed (won't risk the boot layout)"
        exit 0
      fi
      FIRST_FREE=$(sgdisk -f "$DISK" 2>/dev/null | tr -dc '0-9') || FIRST_FREE=""
      LAST_USABLE=$(sgdisk -E "$DISK" 2>/dev/null | tr -dc '0-9') || LAST_USABLE=""
      if [ -z "$FIRST_FREE" ] || [ -z "$LAST_USABLE" ]; then
        decide NOOP "could not determine GPT free-space bounds on $DISK"
        exit 0
      fi
      if [ "$FIRST_FREE" -ge "$LAST_USABLE" ] 2>/dev/null; then
        FREE_SECTORS=0
        decide NOOP "no trailing free space on $DISK (first_free=$FIRST_FREE >= last_usable=$LAST_USABLE) — ISO filled the stick"
        exit 0
      fi
      FREE_SECTORS=$((LAST_USABLE - FIRST_FREE + 1))
      if [ "$FREE_SECTORS" -lt 32768 ] 2>/dev/null; then
        decide NOOP "trailing free space too small ($FREE_SECTORS sectors < 16 MiB)"
        exit 0
      fi
      cecho "GPT trailing free space: $FREE_SECTORS sectors (first_free=$FIRST_FREE last_usable=$LAST_USABLE)"

      # --largest-new=0 spans the LARGEST contiguous FREE region; it CANNOT move or
      # resize an existing partition — only a new GPT entry is appended into free
      # space, so the in-use ISO/EFI/boot partitions are untouched.
      if ! sgdisk --largest-new=0 --change-name=0:"$LABEL" --typecode=0:0700 "$DISK" >/dev/null 2>&1; then
        decide FAIL "sgdisk failed to create the GPT partition on $DISK (flash/boot unaffected)"
        exit 0
      fi

    else
      # ── 4b/5b. isohybrid MBR (DOS-label) path: parted mkpart in the free tail. ──
      # sgdisk MUST NOT run here (it would convert MBR->GPT and destroy the boot
      # layout). parted appends a PRIMARY partition into the trailing free space of
      # a DOS table without rewriting the existing primaries. We require the disk
      # to actually have a dos label (or be unpartitioned) — anything else we skip
      # rather than guess. parted reports free regions via `unit s print free`.
      if [ "$PTTYPE" != "dos" ] && [ -n "$PTTYPE" ]; then
        decide NOOP "$DISK has an unrecognised table type '$PTTYPE' (not gpt/dos) — won't risk the boot layout"
        exit 0
      fi
      # Largest trailing free region in SECTORS from parted's "free" rows.
      # Each free row: "START END SIZE Free Space". Take the LAST (trailing) one.
      FREE_ROW=$(parted -ms "$DISK" unit s print free 2>/dev/null | gawk -F: '$0 ~ /free/ {s=$2; e=$3} END{if (s!="") print s" "e}') || FREE_ROW=""
      if [ -z "$FREE_ROW" ]; then
        FREE_SECTORS=0
        decide NOOP "no trailing free space on the MBR disk $DISK (ISO filled the stick)"
        exit 0
      fi
      FSTART=$(printf '%s' "$FREE_ROW" | gawk '{print $1}' | tr -dc '0-9')
      FEND=$(printf '%s' "$FREE_ROW" | gawk '{print $2}' | tr -dc '0-9')
      if [ -z "$FSTART" ] || [ -z "$FEND" ] || [ "$FSTART" -ge "$FEND" ] 2>/dev/null; then
        FREE_SECTORS=0
        decide NOOP "MBR free-space bounds unusable on $DISK (start=$FSTART end=$FEND)"
        exit 0
      fi
      FREE_SECTORS=$((FEND - FSTART + 1))
      if [ "$FREE_SECTORS" -lt 32768 ] 2>/dev/null; then
        decide NOOP "MBR trailing free space too small ($FREE_SECTORS sectors < 16 MiB)"
        exit 0
      fi
      # MBR allows at most 4 primaries; if the isohybrid already used all 4 we can't
      # add one. Count existing primaries (parted numbers them 1..N).
      NPRI=$(parted -ms "$DISK" unit s print 2>/dev/null | gawk -F: 'BEGIN{n=0} /^[0-9]+:/{n++} END{print n}') || NPRI=0
      if [ "$NPRI" -ge 4 ] 2>/dev/null; then
        decide NOOP "MBR disk $DISK already has 4 primary partitions — no slot for HARTLOG"
        exit 0
      fi
      cecho "MBR trailing free space: $FREE_SECTORS sectors (start=$FSTART end=$FEND, $NPRI existing primaries)"
      # Append a primary FAT32 partition spanning the trailing free region. parted
      # only writes the new entry; the existing primaries are untouched.
      if ! parted -s -a optimal "$DISK" mkpart primary fat32 "''${FSTART}s" "''${FEND}s" >/dev/null 2>&1; then
        decide FAIL "parted failed to create the MBR partition on $DISK (flash/boot unaffected)"
        exit 0
      fi
    fi

    # ── 6. Re-read the table, resolve the new node, FAT32-format + label it. ──
    partprobe "$DISK" 2>/dev/null || partx -u "$DISK" 2>/dev/null || true
    udevadm settle 2>/dev/null || sleep 2 || true

    # The new partition is the highest-numbered one on the disk.
    NEWPART=$(lsblk -lnpo NAME,TYPE "$DISK" 2>/dev/null | gawk '$2=="part"{p=$1} END{print p}') || NEWPART=""
    if [ -z "$NEWPART" ] || [ ! -b "$NEWPART" ]; then
      decide FAIL "could not resolve the new partition node on $DISK after creation (partition exists; next boot may format)"
      exit 0
    fi
    cecho "created new partition $NEWPART on $DISK"

    # FAT32 + label so the Windows host mounts it natively + hart-boot-log finds it
    # by-label. -F 32 forces FAT32; -n sets the volume label (HARTLOG).
    if ! mkfs.vfat -F 32 -n "$LABEL" "$NEWPART" >/dev/null 2>&1; then
      decide FAIL "mkfs.vfat failed on $NEWPART (partition exists; boot unaffected)"
      exit 0
    fi

    udevadm settle 2>/dev/null || true
    if blkid -L "$LABEL" >/dev/null 2>&1; then
      decide CREATED "$LABEL FAT32 partition created on $DISK ($NEWPART) — hart-boot-log can now land the journal on the stick"
    else
      decide CREATED "formatted $NEWPART on $DISK but '$LABEL' label not yet resolvable (will resolve next boot)"
    fi
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.hartlogCreate = {
    enable = lib.mkEnableOption ''
      Live-OS self-creation of the HARTLOG diagnostic partition. On first boot
      from a removable/USB stick that has trailing unpartitioned free space and
      no existing HARTLOG partition, HART OS carves a FAT32 partition labelled
      HARTLOG into ONLY that free space (Linux-side: sgdisk on a GPT isohybrid,
      parted mkpart on a DOS/MBR isohybrid), so hart-boot-log can land the boot
      journal on the stick. It resolves the REAL booted USB by following the
      iso9660/overlay live mount source to its backing block device (and a loop
      device back to the disk its backing file lives on), or by matching the disk
      carrying the ISO's HART_OS volume label. Every decision (which disk it
      picked, the free space found, and why it no-op'd) is logged LOUDLY to the
      journal, the boot console, and /run/hart/hartlog-create.status — so a silent
      no-op is never undebuggable. This REPLACES the Windows-flasher diskpart path,
      which hung on a wedged VDS and corrupted a freshly-flashed stick's EFI/GPT. A
      pure NO-OP when not USB-booted, when no free space exists, when HARTLOG
      already exists, or on any error — it NEVER touches the in-use ISO/EFI/boot
      partitions and NEVER blocks boot'';

    label = lib.mkOption {
      type = lib.types.str;
      # Default to the boot-log module's label so the two ALWAYS agree (ONE
      # contract). hart-boot-log reads by this label; we write it.
      default = config.hart.bootLog.label;
      defaultText = lib.literalExpression "config.hart.bootLog.label";
      description = ''
        The filesystem LABEL to give the diagnostic partition. Must match
        hart.bootLog.label (the label hart-boot-log looks up). Defaults to it so
        the create-side and the read-side are kept in lockstep automatically.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled OR not USB-booted)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && hlog.enable) {

    # The one-shot that creates HARTLOG. Runs EARLY and ORDERED BEFORE the
    # boot-log capture units so the very first boot's journal can land on the
    # partition this unit created. It must never gate anything — nothing waits on
    # it, and a stall/timeout cannot wedge boot.
    systemd.services.hart-hartlog-create = {
      description = "HART OS — create the HARTLOG diagnostic partition in the USB's free space (Live OS, replaces the Windows diskpart path)";
      wantedBy = [ "multi-user.target" ];
      # After the block layer + udev are up (so lsblk/findmnt/sgdisk see the disk),
      # but BEFORE the boot-log capture so the first boot already has HARTLOG.
      after = [ "local-fs.target" "systemd-udev-settle.service" ];
      before = [ "hart-boot-log-early.service" "hart-boot-log-periodic.timer" ];
      # A nixos-rebuild switch must not re-run a one-shot mid-session.
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${createScript}";
        # A slow USB stick's sgdisk/mkfs must not wedge boot — bounded + best-effort.
        TimeoutStartSec = "120s";
      };
      # Even if the unit itself errored at the systemd level (it shouldn't — the
      # script always exits 0), never let it fail the boot transaction.
      unitConfig.DefaultDependencies = true;
    };

    # The create CLI on PATH so an operator can also run it by hand from a TTY
    # (`hart-hartlog-create`) — handy if the first-boot auto-run was a no-op and
    # the user later frees space.
    environment.systemPackages = [
      (pkgs.runCommand "hart-hartlog-create-cli" { } ''
        mkdir -p $out/bin
        ln -s ${createScript} $out/bin/hart-hartlog-create
      '')
      pkgs.gptfdisk
      pkgs.dosfstools
    ];
  };
}
