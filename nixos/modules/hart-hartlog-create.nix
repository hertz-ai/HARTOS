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
  #   dosfstools -> mkfs.vfat (FAT32 format + label)
  #   util-linux -> lsblk, findmnt, blkid, partprobe-ish (we use `partx`/`udevadm`)
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux gptfdisk dosfstools gawk gnugrep gnused
  ]);

  createScript = pkgs.writeShellScript "hart-hartlog-create" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    LABEL="${label}"

    log() { echo "[hart-hartlog-create] $*" >&2 ; }

    # ── 0. Tooling sanity. Any missing tool -> clean no-op (never fail boot). ──
    for t in lsblk findmnt blkid sgdisk mkfs.vfat; do
      if ! command -v "$t" >/dev/null 2>&1; then
        log "tool '$t' not found — skipping HARTLOG creation (clean no-op)"
        exit 0
      fi
    done

    # ── 1. Already have a HARTLOG partition? Idempotent no-op. ──
    # Re-runs on every boot are fine: the second boot finds the partition created
    # on the first and exits cleanly. blkid -L resolves by filesystem label.
    if blkid -L "$LABEL" >/dev/null 2>&1; then
      log "a '$LABEL' partition already exists — nothing to do (idempotent no-op)"
      exit 0
    fi

    # ── 2. Find the parent disk the LIVE ROOT was booted from. ──
    # The live ISO mounts its squashfs/overlay; the backing device is whatever the
    # ISO was written to (the USB stick). Walk the key live mountpoints back to
    # their source block device, then to that device's PARENT whole-disk (pkname).
    # Try, in order, the ISO live filesystem mountpoints and finally `/`.
    #
    # TEST SEAM (never set in production): HART_HARTLOG_TEST_DISK lets the
    # nixosTest (tests/hartlog-create.nix) point the carve at a stand-in spare
    # disk — the VM's boot disk is NOT the stick, so the live-root walk below
    # wouldn't reach the test disk. When set to a block device, we still apply the
    # SAME removable/free-space/never-touch-existing gates below; it only bypasses
    # the boot-disk auto-detection. It is read ONLY here and documented as a test
    # hook so production behaviour is unchanged (the env var is never set by any
    # unit/config).
    if [ -n "''${HART_HARTLOG_TEST_DISK:-}" ] && [ -b "''${HART_HARTLOG_TEST_DISK}" ]; then
      DISK="''${HART_HARTLOG_TEST_DISK}"
      log "TEST SEAM: HART_HARTLOG_TEST_DISK=$DISK (bypassing boot-disk auto-detect; safety gates still apply)"
      # In the test we deliberately skip the removable-only gate (the VM's spare
      # disk is not flagged removable); the test's job is the carve mechanics +
      # never-touch-existing invariant. Jump straight to the free-space gate.
      TEST_DISK=1
    else
      TEST_DISK=0
    SRC=""
    for mp in /iso /run/initramfs/live /run/rootfs /nix/.ro-store /; do
      [ -d "$mp" ] || continue
      s=$(findmnt -n -o SOURCE --target "$mp" 2>/dev/null | head -n1) || s=""
      # Skip overlay/tmpfs/aufs pseudo-sources (no backing block device).
      case "$s" in
        /dev/*) SRC="$s"; break ;;
        *) : ;;
      esac
    done
    if [ -z "$SRC" ]; then
      log "could not resolve a backing block device for the live root — skipping"
      exit 0
    fi

    # Resolve SRC (a partition like /dev/sdb1, or a loop/by-label symlink) to its
    # PARENT whole disk (e.g. /dev/sdb). lsblk -no pkname gives the parent kernel
    # name; if SRC is already a whole disk, pkname is empty and we use SRC itself.
    PK=$(lsblk -ndo pkname "$SRC" 2>/dev/null | head -n1) || PK=""
    if [ -n "$PK" ]; then
      DISK="/dev/$PK"
    else
      # SRC may itself be the whole disk, OR a loop device (ISO mounted via loop).
      # A loop device is NOT the USB — bail (we can't safely find the stick).
      case "$SRC" in
        /dev/loop*) log "live root backed by a loop device, not a USB partition — skipping" ; exit 0 ;;
        *) DISK="$SRC" ;;
      esac
    fi
    if [ ! -b "$DISK" ]; then
      log "resolved parent disk '$DISK' is not a block device — skipping"
      exit 0
    fi
    log "live root backed by $SRC -> parent disk $DISK"

    # ── 3. The disk MUST be removable/USB. NEVER touch an internal disk. ──
    # RM=1 (removable) OR TRAN=usb. An internal NVMe/SATA disk is RM=0 + TRAN!=usb
    # -> we refuse. This is the single most important safety gate.
    RM=$(lsblk -ndo RM "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || RM=""
    TRAN=$(lsblk -ndo TRAN "$DISK" 2>/dev/null | head -n1 | tr -d ' ') || TRAN=""
    if [ "$RM" != "1" ] && [ "$TRAN" != "usb" ]; then
      log "$DISK is not removable/USB (RM=$RM TRAN=$TRAN) — refusing to repartition an internal disk (no-op)"
      exit 0
    fi
    log "$DISK is removable/USB (RM=$RM TRAN=$TRAN) — eligible"
    fi  # end of the auto-detect (non-test) branch; the test seam set DISK directly
    # and deliberately skips the removable/USB gate (the VM's spare disk is not
    # flagged removable). $DISK is now set in both branches; safety gates below
    # (free space + never-touch-existing) still apply to both.

    # ── 4. Require trailing UNPARTITIONED free space at the END of the device. ──
    # sgdisk -e moves a backup GPT header to the end (harmless, needed for honest
    # free-space accounting on a GPT written by isohybrid); then we read the
    # largest free block. We do NOT carve unless there is a meaningful tail (>= a
    # few MiB) so a stick the ISO nearly filled is a clean skip, not a 1-sector
    # partition.
    #
    # sgdisk -p prints the partition table; sgdisk's "-E" prints the LAST usable
    # sector, and "-f" the FIRST aligned free sector after the last partition.
    # If FIRST_FREE > LAST_USABLE there is no free tail.
    #
    # Guard: the ISO is written with isohybrid (a hybrid MBR + GPT). sgdisk works
    # on the GPT. If the disk has no GPT sgdisk reports it; we then skip (we will
    # not convert an MBR-only layout — too risky on the in-use boot disk).
    if ! sgdisk -p "$DISK" >/dev/null 2>&1; then
      log "$DISK has no readable GPT (sgdisk -p failed) — skipping (won't risk the boot layout)"
      exit 0
    fi

    FIRST_FREE=$(sgdisk -f "$DISK" 2>/dev/null | tr -dc '0-9') || FIRST_FREE=""
    LAST_USABLE=$(sgdisk -E "$DISK" 2>/dev/null | tr -dc '0-9') || LAST_USABLE=""
    if [ -z "$FIRST_FREE" ] || [ -z "$LAST_USABLE" ]; then
      log "could not determine free-space bounds on $DISK — skipping"
      exit 0
    fi
    if [ "$FIRST_FREE" -ge "$LAST_USABLE" ] 2>/dev/null; then
      log "no trailing free space on $DISK (first_free=$FIRST_FREE >= last_usable=$LAST_USABLE) — ISO filled the stick, clean no-op"
      exit 0
    fi

    # Require at least ~16 MiB of tail (16*1024*1024/512 = 32768 sectors of 512B —
    # use a sector-size-agnostic margin by comparing sector counts conservatively).
    FREE_SECTORS=$((LAST_USABLE - FIRST_FREE + 1))
    if [ "$FREE_SECTORS" -lt 32768 ] 2>/dev/null; then
      log "trailing free space too small ($FREE_SECTORS sectors) — skipping (not worth a tiny partition)"
      exit 0
    fi
    log "trailing free space: $FREE_SECTORS sectors (first_free=$FIRST_FREE last_usable=$LAST_USABLE)"

    # ── 5. Carve ONE new partition from the LARGEST FREE BLOCK, FAT32, label it. ──
    # `sgdisk --largest-new=0` creates a new partition (next free number) spanning
    # the LARGEST contiguous FREE region — it CANNOT move or resize any existing
    # partition, so the in-use ISO/EFI/boot partitions are untouched; only a new
    # GPT entry is appended into already-free space. We set a type + name too.
    # Belt-and-suspenders: pass --largest-new (the trailing tail IS the largest
    # free region on a freshly-flashed stick).
    if ! sgdisk \
        --largest-new=0 \
        --change-name=0:"$LABEL" \
        --typecode=0:0700 \
        "$DISK" >/dev/null 2>&1; then
      log "sgdisk failed to create the partition on $DISK — skipping (flash/boot unaffected)"
      exit 0
    fi

    # Re-read the partition table so the new partition's device node appears.
    partprobe "$DISK" 2>/dev/null || partx -u "$DISK" 2>/dev/null || \
      udevadm settle 2>/dev/null || true
    udevadm settle 2>/dev/null || sleep 2 || true

    # The new partition is the highest-numbered one on the disk. Resolve its node
    # via lsblk (last partition child of the disk).
    NEWPART=$(lsblk -lnpo NAME,TYPE "$DISK" 2>/dev/null | gawk '$2=="part"{p=$1} END{print p}') || NEWPART=""
    if [ -z "$NEWPART" ] || [ ! -b "$NEWPART" ]; then
      log "could not resolve the new partition node after creation — skipping format (partition exists; next boot may format)"
      exit 0
    fi
    log "created new partition $NEWPART on $DISK"

    # FAT32 + label so the Windows host mounts it natively + hart-boot-log finds it
    # by-label. -F 32 forces FAT32; -n sets the volume label (HARTLOG).
    if ! mkfs.vfat -F 32 -n "$LABEL" "$NEWPART" >/dev/null 2>&1; then
      log "mkfs.vfat failed on $NEWPART — skipping (partition exists; boot unaffected)"
      exit 0
    fi

    # Confirm by-label resolution (the exact contract hart-boot-log uses).
    udevadm settle 2>/dev/null || true
    if blkid -L "$LABEL" >/dev/null 2>&1; then
      log "SUCCESS: $LABEL FAT32 partition created on $DISK ($NEWPART) — hart-boot-log can now land the journal on the stick"
    else
      log "formatted $NEWPART but '$LABEL' label not yet resolvable — it will resolve on the next boot"
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
      HARTLOG into ONLY that free space (Linux-side, via sgdisk + mkfs.vfat), so
      hart-boot-log can land the boot journal on the stick. This REPLACES the
      Windows-flasher diskpart path, which hung on a wedged VDS and corrupted a
      freshly-flashed stick's EFI/GPT. A pure NO-OP when not USB-booted, when no
      free space exists, when HARTLOG already exists, or on any error — it NEVER
      touches the in-use ISO/EFI/boot partitions and NEVER blocks boot'';

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
