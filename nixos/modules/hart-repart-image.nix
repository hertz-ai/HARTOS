# HART OS — no-VM bootable UEFI raw image via systemd-repart.
#
# WHY THIS EXISTS (2026-07-24): the raw-desktop INSTALLED image used to be built
# by nixos-generators' `raw-efi` format, which assembles the ext4 root inside a
# qemu VM (make-disk-image). On GitHub Actions that leg ran ~5h and blew the
# 300-min job cap, so the installed image was never published — every USB flash
# has been the live ISO (RAM overlay, read-only /nix/store, "Disk 100%" reading
# the squashfs, no persistence). `image.repart` assembles the disk OFFLINE with
# `fakeroot systemd-repart` in a plain stdenvNoCC derivation — no qemu, no KVM,
# no loop device — so the build is closure-bound (~ISO-desktop time) and fits the
# cap. This module owns the fileSystems + bootloader that the nixos-generators
# format module used to provide.
#
# Imported ONLY by mkRepartImage in flake.nix (never in hartModules), so the ISO
# builds (mkSystem) and every nixosTest eval stay byte-identical. UEFI-ONLY by
# design (the 2026-07-16 raw-image pivot).
{ config, lib, pkgs, modulesPath, ... }:
{
  imports = [
    "${modulesPath}/image/repart.nix"
    # Parity with the outgoing nixos-generators raw-efi format (virtio for VM boot;
    # bare-metal storage coverage comes from hart-boot-root-initrd, enabled in
    # desktop.nix, plus the extra modules pinned below for USB/SATA/NVMe).
    "${modulesPath}/profiles/qemu-guest.nix"
  ];

  # ── UEFI-only: systemd-boot in the ESP, no GRUB, no NVRAM writes ──
  # canTouchEfiVariables=false keeps the image portable (a dd'd stick boots via
  # the removable-media path EFI/BOOT/BOOTX64.EFI with no firmware NVRAM entry).
  boot.loader.grub.enable = false;
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;

  # ── Installed-system filesystems (previously supplied by nixos-generators) ──
  # device is a stable by-label path so no root= cmdline is needed; the labels are
  # set on the partitions below. autoResize + growPartition are ALSO set by
  # desktop.nix's hartImageKind=="raw" branch — identical values merge (bool
  # mergeEqualOption), so this module stays self-contained without conflicting.
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
    autoResize = true;
  };
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/ESP";
    fsType = "vfat";
  };
  # First boot: growpart (initrd) expands the LAST partition (root) to fill the
  # target device, then autoResize grows the ext4. Method-independent and already
  # proven on the old raw path; works on a repart GPT because root is last.
  boot.growPartition = true;

  # Broaden the initrd's storage coverage so the installed image boots from a USB
  # stick / SATA SSD / NVMe on real hardware (the repart initrd is narrower than
  # the ISO's all-hardware profile). Merges with hart-boot-root-initrd.
  boot.initrd.availableKernelModules = [
    "usb_storage" "uas" "ahci" "nvme" "sd_mod" "ext4" "vfat"
  ];

  # ── The image: ESP + root assembled OFFLINE by systemd-repart (no qemu) ──
  image.repart = {
    name = "hart-os";           # -> $out/hart-os.raw  (matches release.yml *.raw find)
    # Compression left OFF on purpose: release.yml xz-compresses the bare .raw and
    # splits it into <1.9GiB parts + reassemble.sh, exactly like the ISO.
    partitions = {
      "10-esp" = {
        contents = {
          "/EFI/BOOT/BOOTX64.EFI".source =
            "${pkgs.systemd}/lib/systemd/boot/efi/systemd-bootx64.efi";
          "/EFI/Linux/${config.system.boot.loader.ukiFile}".source =
            "${config.system.build.uki}/${config.system.boot.loader.ukiFile}";
        };
        repartConfig = {
          Type = "esp";
          Format = "vfat";
          Label = "ESP";
          # Must fit the desktop UKI (kernel + full initrd in one .efi). Generous
          # to start; tighten after a real boot proves the UKI size.
          SizeMinBytes = "1G";
        };
      };
      "20-root" = {
        storePaths = [ config.system.build.toplevel ];
        repartConfig = {
          Type = "root";          # x86-64 root discoverable-partition GUID
          Format = "ext4";
          Label = "nixos";
          # EXPLICIT size, deliberately NOT Minimize. `Minimize = "guess"` looked
          # right (size the image to the closure) and is what killed two builds:
          # with no upper bound, systemd-repart probes by creating a 1 TiB ext4
          # FIRST (268435456 4k blocks, 67M inodes), copying the closure into it,
          # measuring, then rebuilding minimized. The probe alone costs ~17GB of
          # inode tables plus a 1GB journal plus the copied closure, on a CI runner
          # with 103GB free and 42GB already used, and it dies without printing an
          # error (runs 30263310599 and 30287421697, ~54 minutes each).
          #
          # 28G, and the difference from the old 44G is CI disk, not target disk.
          # Inside the Nix sandbox systemd-repart has no loop device, so it mkfs's
          # into a temp file and then COPIES that file into the .raw. The temp file
          # stays sparse (mke2fs punches holes for unused blocks) but the copy does
          # not -- `copy_bytes` after mkfs writes the partition out dense -- so this
          # number is very nearly the peak disk the build costs, on top of what the
          # closure already occupies in /nix/store. Run 30312462459 died exactly
          # there: 61GB free, temp filesystem written, then ENOSPC partway into the
          # copy, silently, because the copy loop never reaches a log statement.
          #
          # 28G is sized from the MEASURED closure: 23531757072 bytes = 21.9 GiB
          # (run 30334471869's "Closure budget" step). Plus ext4 metadata and the
          # journal that is ~23 GiB, so this leaves ~5 GiB of headroom.
          #
          # The 44G and 40G that came before were sized off repart's `Minimize`
          # probe, which reported 9627552 4k blocks (36.7 GiB) -- inflated by the
          # inode tables of the 1 TiB filesystem the probe itself creates, and
          # therefore never a measurement of this closure at all. Do not size this
          # partition from a Minimize block count; size it from `nix path-info -S`
          # on the toplevel, which CI now prints before every image build.
          #
          # If the closure ever outgrows this, mke2fs fails LOUDLY with a "does not
          # fit" error -- clearly distinguishable from the silent ENOSPC above.
          #
          # None of this constrains the installed system: the image ships
          # xz-compressed and growPartition + autoResize expand the root to fill the
          # real disk on first boot, so the on-disk size is the target's, not this.
          SizeMinBytes = "28G";
          SizeMaxBytes = "28G";
        };
      };
    };
  };
}
