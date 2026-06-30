{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Cross-OS storage interop (read/write ALL filesystems)  [#145]
# ═══════════════════════════════════════════════════════════════
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
      default = [ "ntfs" "exfat" "vfat" "ext4" "btrfs" ];
      description = ''
        The cross-OS filesystems HART OS is built to read AND write. Each name is
        added to boot.supportedFilesystems (kernel driver + userspace mount
        helper). The default is the #145 interop set (Windows NTFS, camera/phone
        exFAT, FAT32/vfat, Linux ext4 + btrfs). ZFS is intentionally NOT here (it
        is force-disabled per-variant; broken in this nixpkgs for the pinned
        kernel).
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
    #   ntfs3g     -> mount.ntfs (mount helper) + mkfs.ntfs + ntfsfix
    #   exfatprogs -> mkfs.exfat + fsck.exfat
    #   e2fsprogs  -> mkfs.ext4 + fsck.ext4 (usually already in base; explicit)
    #   btrfs-progs-> mkfs.btrfs + btrfs check
    #   dosfstools -> mkfs.vfat + fsck.fat
    #   util-linux -> mount/lsblk/blkid/wipefs (the disk-inspection surface)
    environment.systemPackages = (with pkgs; [
      ntfs3g
      exfatprogs
      e2fsprogs
      btrfs-progs
      dosfstools
      util-linux
    ]) ++ [
      # The real-HW driver readout (the VM proves the mount; this answers "did the
      # interop config deliver the drivers on THIS physical kernel"). Read-only +
      # unprivileged + always exit 0, so the every-boot smoke-test + the sandbox
      # validator can call it on real iron without risk. (See its docstring.)
      fsProbe
    ];
  };
}
