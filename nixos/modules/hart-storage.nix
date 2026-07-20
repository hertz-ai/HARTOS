{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Cross-OS storage interop + Disk Utility  [#145 / #157]
# ═══════════════════════════════════════════════════════════════
#
# #157 EXTENSION (the Disk Utility surface, additive on top of #145 interop):
#   #145 made HART OS READ/WRITE a disk from any OS (ntfs/exfat/vfat/ext4/btrfs +
#   on-demand udisks mount). #157 finishes the FORMAT/REPAIR/RESIZE/HEALTH surface:
#     * xfs + f2fs are added to the supported filesystem set (7 total), so a disk
#       formatted xfs (RHEL/Fedora default) or f2fs (flash/Android) mounts too, and
#       the Disk Utility can FORMAT to either. Kernel-native drivers, AVAILABLE not
#       REQUIRED -> still boot-safe (root is the ISO overlay, never xfs/f2fs).
#     * The full tooling for every supported FS lands on PATH: mkfs.*/fsck.* for
#       formatting + repairing, e4defrag/btrfs-defragment/xfs_fsr for defrag,
#       resize2fs/btrfs-resize/xfs_growfs/ntfsresize/resize.f2fs for shrink/grow,
#       parted/sgdisk for partition shrink/grow, smartmontools/nvme-cli/hdparm for
#       the disk-health surface. smartmontools in particular FIXES the already-
#       shipped /api/shell/storage/smart route, which called `smartctl` that was in
#       NO closure (a real production bug - the route always 500'd).
#     * A boot-time DISK-HEALTH oneshot (hart-disk-health.sh) snapshots an honest
#       per-device SMART verdict to /run/hart/disk-health, mirroring hart-gpu-probe
#       / hart-display-health. Read-only, bounded, always exit 0 -> never bricks.
#   The backend ops that INVOKE this tooling (defrag / fsck / format / resize /
#   trim / health) live in shell_system_apis.py section 11 (Disk Utility), gated so
#   no destructive op (format/resize) runs without an explicit confirm.
#   Memory (zram/swap/OOM) is the sibling module hart-memory.nix.
#
# ── #145 base (cross-OS read/write interop) ──────────────────────────────────
#
# THE problem this solves (#145 "Interop by design"):
#   A user plugs in a disk that was formatted on ANOTHER operating system —
#   a Windows NTFS drive, a camera/phone exFAT card, a Linux ext4/btrfs disk,
#   a FAT32 stick — and expects HART OS to read AND write it, just like macOS
#   or Windows would. Without the kernel drivers AND the userspace mount
#   helpers AND an auto-mount authority, that disk either silently fails to
#   appear or mounts read-only. Before this module the only filesystem support
#   was the NTFS/exFAT/vfat kernel modules that hart-kernel.nix force-loads
#   ONLY when `windowsNative.enable` is on (a Wine-path side effect, desktop-
#   only, with no userspace mount helpers / no ext4-or-btrfs-from-another-distro
#   coverage / no guaranteed auto-mount daemon under the cage/sway tier).
#
# THE fix — three layers, all additive + non-boot-blocking:
#   1. boot.supportedFilesystems = ntfs/exfat/vfat/ext4/btrfs — pulls the kernel
#      drivers AND the userspace mount helpers (mount.ntfs/ntfs-3g, fsck.exfat)
#      into the system so `mount` (and udisks) can handle a disk from any OS.
#   2. services.udisks2 — the mount AUTHORITY the file manager + the glass shell
#      call to mount removable media ON DEMAND (polkit-gated, per-user under
#      /run/media). This is the auto-mount path; it is NEVER an fstab/.mount unit,
#      so a missing/faulting disk can never stall local-fs.target and wedge boot.
#   3. format + repair tooling (mkfs.*/fsck.*) on PATH so "read/write ALL
#      filesystems" includes formatting and repairing them, not just mounting.
#
# THE degrade-not-die contract (why this can NEVER brick/black/hang boot):
#   - It adds ZERO `fileSystems."…"` entries and ZERO systemd .mount/.automount
#     units for external disks. Every external mount is on-demand via udisks
#     (user/shell initiated), so a disconnected or corrupt disk is simply never
#     mounted — local-fs.target does not wait on it.
#   - boot.supportedFilesystems only makes the drivers AVAILABLE; it never makes
#     any of them REQUIRED for boot (root is the ISO overlay, not ntfs/btrfs).
#   - An unmountable / corrupt / unknown-filesystem disk makes `mount` (and
#     `udisksctl mount`) fail CLEANLY and FAST (-EINVAL "wrong fs type / bad
#     superblock"); it never hangs the kernel or the session.
#   The behavioural proof of all of the above is tests/storage-filesystems.nix.
#
# VM-provable vs HW-gated: the nixosTest proves the round-trip read/write across
# all five filesystems, the udisks mount path, and the unmountable-disk degrade
# in a VM. Plugging a PHYSICAL NTFS/exFAT disk and seeing it auto-appear is the
# real-HW half: hart-storage-fsprobe.sh (installed below) is the read-only driver
# READOUT for real iron - it reports, per filesystem, whether THIS kernel can
# mount it (`fs_<name>=ok|missing`), called every boot by hart-compat-smoketest
# (-> /run/hart/compat-status + the journal) and by `hart sandbox test-windows`.
# Its decision logic is unit-tested portably (tests/unit/test_hart_storage_fsprobe.py).
#
# KNOWN honest limits (not regressions, recorded so they aren't re-litigated):
#   - A Windows disk left in Fast-Startup / hybrid-shutdown carries a dirty
#     hiberfil; ntfs3 then mounts it READ-ONLY to protect the Windows session.
#     That is correct degrade (RO, not a wedge); full write needs the user to
#     disable Fast Startup (the #130 firmware-prep checklist).
#   - APFS / HFS+ (macOS) read is NOT included here (apfs-fuse is unfree +
#     fragile); a separate opt-in if/when it is wanted.

let
  cfg = config.hart;
  scfg = config.hart.storage;

  # The cross-OS filesystem real-HW driver probe (the real-HW half the VM cannot
  # reach). Standalone .sh so its decision logic lives in ONE place every caller
  # shares AND a portable unit test runs the REAL bytes against a stub modinfo +
  # fake /proc/filesystems (tests/unit/test_hart_storage_fsprobe.py) - the same
  # pattern as hart-audio-unmute.sh. Installed on PATH (below) when storage is
  # enabled so hart-compat-smoketest (every-boot status file) and hart-sandbox
  # (`hart sandbox test-windows`) both call it via `command -v hart-storage-fsprobe`
  # rather than re-implementing "is this filesystem's driver available" twice.
  fsProbe = pkgs.writeShellScriptBin "hart-storage-fsprobe"
    (builtins.readFile ./hart-storage-fsprobe.sh);

  # The boot-time DISK-HEALTH snapshot (#157). Shipped verbatim (runCommand, not
  # writeShellScript) so the file's own `#!/bin/sh` is preserved and it is
  # POSIX-linted at build time, AND so the SAME bytes are run by the dev-box unit
  # test (tests/unit/test_hart_disk_health.py, env-overridable paths) - one source
  # of truth, the same pattern as hart-display-health.nix.
  diskHealthScript = pkgs.runCommand "hart-disk-health"
    { nativeBuildInputs = [ pkgs.coreutils ]; }
    ''
      mkdir -p $out/bin
      cp ${./hart-disk-health.sh} $out/bin/hart-disk-health
      chmod +x $out/bin/hart-disk-health
      ${pkgs.dash}/bin/dash -n $out/bin/hart-disk-health
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.storage = {
    enable = lib.mkEnableOption ''
      Cross-OS storage interop (#145): read/write NTFS, exFAT, FAT32/vfat, ext4,
      and btrfs disks from any operating system, with on-demand udisks auto-mount
      and the format/repair tooling for each filesystem. Adds only AVAILABLE
      drivers + an on-demand mount authority (never an fstab/.mount unit), so a
      missing or corrupt disk can never block or fail boot
    '';

    filesystems = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "ntfs" "exfat" "vfat" "ext4" "btrfs" "xfs" "f2fs" ];
      description = ''
        The cross-OS filesystems HART OS is built to read AND write. Each name is
        added to boot.supportedFilesystems (kernel driver + userspace mount
        helper). The default is the #145 interop set (Windows NTFS, camera/phone
        exFAT, FAT32/vfat, Linux ext4 + btrfs) PLUS the #157 additions xfs
        (RHEL/Fedora default) and f2fs (flash/Android), so the Disk Utility can
        mount AND format all seven. Each driver is made AVAILABLE, never REQUIRED
        for boot (root is the ISO overlay), so adding a filesystem here can never
        block boot. ZFS is intentionally NOT here (it is force-disabled per-variant;
        broken in this nixpkgs for the pinned kernel).
      '';
    };

    autoMount = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Enable services.udisks2 — the polkit-gated mount authority the file
        manager and the glass shell call to mount removable media ON DEMAND
        (under /run/media). This is auto-mount via a user/shell action, NOT an
        fstab or systemd .mount unit, so it can never stall local-fs.target.
      '';
    };

    healthProbe.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run the boot-time DISK-HEALTH snapshot (hart-disk-health): a oneshot that
        records an honest per-device SMART verdict (name / path / size / rota /
        model / smart=passed|failed|unknown) to /run/hart/disk-health (one
        key=value line each, also echoed to the journal) AFTER greetd is up. It is
        the real-HW storage observability twin of hart-gpu-probe /
        hart-display-health: a measurement an operator / the Disk Utility reads
        after a real boot. It MOUNTS nothing, WRITES nothing to any disk, is
        per-device `timeout`-bounded, and ALWAYS exits 0, so it can never block,
        fail, or brick the boot. Set FALSE to skip the snapshot (the verdict file
        is simply not written; nothing else changes).
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && scfg.enable) {

    # ── 1. Cross-OS filesystem support (drivers + userspace mount helpers). ──
    # Attrset form (the same form desktop.nix uses for `…zfs = lib.mkForce
    # false`), so this merges cleanly with the per-variant ZFS disable. Setting a
    # filesystem here makes its driver AVAILABLE + brings in its mount helper; it
    # never makes any of them REQUIRED for boot.
    boot.supportedFilesystems = lib.genAttrs scfg.filesystems (_: true);

    # ── 2. On-demand mount authority (udisks2). ──
    # The desktop GNOME profile also pulls udisks2 in, but the cage/sway glass
    # tier does NOT run GNOME, so enable it explicitly here (defence-in-depth,
    # the same pattern as the explicit NetworkManager enable in desktop.nix —
    # a core capability must not ride on a side effect of another session).
    services.udisks2.enable = lib.mkIf scfg.autoMount true;

    # gvfs gives the file manager (Nautilus) + GIO the mount-integration backend
    # that surfaces a udisks-mounted volume in the UI. mkDefault so GNOME's own
    # enable (also true) never collides, and a variant can drop it.
    services.gvfs.enable = lib.mkDefault true;

    # ── 3. Format + repair tooling for full read/write interop. ──
    # "read/write ALL filesystems" includes FORMATTING and REPAIRING them, and
    # udisks/`mount` need these helpers present to handle a foreign disk:
    #   ntfs3g     -> mount.ntfs (mount helper) + mkfs.ntfs + ntfsfix + ntfsresize
    #   exfatprogs -> mkfs.exfat + fsck.exfat
    #   e2fsprogs  -> mkfs.ext4 + fsck.ext4 + e4defrag + resize2fs (often in base; explicit)
    #   btrfs-progs-> mkfs.btrfs + btrfs check + btrfs filesystem defragment/resize
    #   dosfstools -> mkfs.vfat + fsck.fat
    #   util-linux -> mount/lsblk/blkid/wipefs/fstrim/zramctl (the disk-inspection surface)
    #   ── #157 Disk Utility additions (format/repair/resize/defrag/health) ──
    #   xfsprogs     -> mkfs.xfs + xfs_repair + xfs_growfs + xfs_fsr (xfs defrag)
    #   f2fs-tools   -> mkfs.f2fs + fsck.f2fs + resize.f2fs (flash/Android FS)
    #   parted       -> parted + partprobe (partition shrink/grow)
    #   gptfdisk     -> sgdisk (GPT partition table edit; also pulled by hartlog)
    #   smartmontools-> smartctl (FIXES the broken /api/shell/storage/smart route -
    #                   it was in NO closure, so the SMART route always 500'd)
    #   nvme-cli     -> nvme (NVMe SSD health + identify for the health surface)
    #   hdparm       -> hdparm (ATA disk identify/standby for the health surface)
    environment.systemPackages = (with pkgs; [
      ntfs3g
      exfatprogs
      e2fsprogs
      btrfs-progs
      dosfstools
      util-linux
      # #157 Disk Utility tooling (de-duped by the nix store - parted/gptfdisk are
      # also referenced by hart-hartlog-create.nix; same store path, no bloat).
      xfsprogs
      f2fs-tools
      parted
      gptfdisk
      smartmontools
      nvme-cli
      hdparm
    ]) ++ [
      # The real-HW driver readout (the VM proves the mount; this answers "did the
      # interop config deliver the drivers on THIS physical kernel"). Read-only +
      # unprivileged + always exit 0, so the every-boot smoke-test + the sandbox
      # validator can call it on real iron without risk. (See its docstring.)
      fsProbe
    ];

    # ── 4. Boot-time DISK-HEALTH snapshot (#157) ─────────────────────────────
    # The real-HW storage observability oneshot, mirroring hart-gpu-probe /
    # hart-display-health: it snapshots an honest per-device SMART verdict to
    # /run/hart/disk-health AFTER greetd is up (never `before greetd`, so it can
    # never delay first paint). It MOUNTS nothing + WRITES nothing to any disk +
    # is per-device timeout-bounded + always exits 0, so it can never block, fail,
    # or brick the boot. Gated on hart.storage.healthProbe.enable (default true).
    systemd.tmpfiles.rules = lib.mkIf scfg.healthProbe.enable [
      # Shared /run/hart (tmpfs) at 0750 hart hart - gpu-probe / display-health /
      # session-supervisor all declare the same rule; tmpfiles de-dupes it.
      "d /run/hart 0750 hart hart -"
    ];

    systemd.services.hart-disk-health = lib.mkIf scfg.healthProbe.enable {
      description = "HART OS - boot-time disk-health snapshot (writes per-device SMART verdict to /run/hart/disk-health)";
      wantedBy = [ "multi-user.target" ];
      # AFTER greetd (parallel with the desktop) - NEVER before it: nothing reads
      # this file at boot, so it must never gate the seat. After udev settle so the
      # block devices + their SMART surface exist.
      after = [ "greetd.service" "systemd-udev-settle.service" ];
      # A nixos-rebuild switch must not re-run the snapshot mid-session.
      restartIfChanged = false;
      # smartctl (smartmontools) + nvme (nvme-cli) + lsblk (util-linux) for the
      # readout; coreutils for mkdir/printf/timeout/dirname; gnugrep/gawk for the
      # parse. The script does NOT hardcode store paths so the SAME file is
      # dev-box unit-testable (tests/unit/test_hart_disk_health.py).
      path = with pkgs; [ coreutils gnugrep gawk util-linux smartmontools nvme-cli ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${diskHealthScript}/bin/hart-disk-health";
        # The script bounds each SMART read itself; this outer belt caps the whole
        # run so even a pathological enumeration can't wedge the boot.
        TimeoutStartSec = "60";
      };
    };
  };
}
