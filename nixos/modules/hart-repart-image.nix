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
{ config, lib, pkgs, modulesPath, hartRev, ... }:
let
  # STABLE filesystem labels (2026-08-22) -- the second half of a story that
  # started with PER-BUILD labels (2026-08-14).
  #
  # Round 1: these images are REPRODUCIBLE (seeded mkfs), so every build used
  # to carry the same "nixos"/"ESP" labels. With a stale stick and a fresh one
  # both on the bus, stage 1's by-label mount was a per-boot coin toss between
  # DIFFERENT BUILDS -- a good build sat unbooted while a broken one panicked,
  # and every diagnostic pointed at the wrong filesystem. Fix: rev-derived
  # labels, one identity per build.
  #
  # Round 2, measured on real HW 2026-08-22 (photo of the stage-1 prompt):
  # rev-derived labels make every CROSS-REV mount impossible. An OTA-applied
  # generation built from rev N+1 bakes fileSystems."/" =
  # by-label/hart-<revN+1>, but the flashed disk is labeled hart-<revN>.
  # Stage 1: "must mount the root filesystem on /mnt-root" -- unbootable, on
  # every OTA apply, by construction. /boot has the identical trap via
  # HART-<REV6>. Per-rev labels and self-updating are mutually exclusive.
  #
  # Resolution: the LABELS go stable (the mount reference must outlive the
  # rev), and BUILD IDENTITY moves to /etc/hart/image-rev (written below from
  # hartRev). What this consciously trades away: (a) the on-screen fsck line
  # no longer names the build, and (b) the two-sticks-of-different-revs coin
  # toss RETURNS at the root-mount level. Both accepted: dual-stick debugging
  # now has /etc/hart/image-rev + HARTJRNL once booted, and a fleet that can
  # never update is the worse failure. The hart-hartlog-create boot-disk guard
  # keys on the disk, not the label, and is unaffected.
  #
  # Name collisions checked 2026-08-22: HARTLOG + HARTSTATE are the flasher's
  # (hart_usb_flasher.py), HART-ROOT + HART-SWAP are reserved by hart-luks.nix
  # by-partlabel defaults for a future encrypted layout. hart-root / HART-ESP
  # collide with none of them. ext4 labels cap at 16 bytes, FAT32 at 11.
  rootLabel = "hart-root";
  espLabel  = "HART-ESP";
in
{
  imports = [
    "${modulesPath}/image/repart.nix"
    # Parity with the outgoing nixos-generators raw-efi format (virtio for VM boot;
    # bare-metal storage coverage comes from hart-boot-root-initrd, enabled
    # below for every repart image, plus the extra modules pinned for
    # USB/SATA/NVMe).
    "${modulesPath}/profiles/qemu-guest.nix"
  ];

  # ── Bare-metal storage initrd for EVERY repart image ──
  # This used to ride on desktop.nix's PROFILE enabling hart.bootRootInitrd —
  # which meant a repart raw-server/raw-edge (routed here since the mkImage
  # special case was deleted, 2026-08-08) would boot in a VM (virtio via
  # qemu-guest above) but VFS-panic from a USB/SATA/NVMe root on real
  # hardware, exactly the initrd-lacks-usb_storage class the desktop already
  # debugged. The IMAGE module is the right owner: every dd-able disk image
  # needs the storage drivers of the disks it may be dd'd onto, regardless of
  # variant. mkDefault, so a variant profile that already sets it (desktop)
  # merges cleanly, and ISO closures are untouched (this module is imported
  # only by mkRepartSystem).
  hart.bootRootInitrd.enable = lib.mkDefault true;

  # ── UEFI-only: systemd-boot in the ESP, no GRUB, no NVRAM writes ──
  # canTouchEfiVariables=false keeps the image portable (a dd'd stick boots via
  # the removable-media path EFI/BOOT/BOOTX64.EFI with no firmware NVRAM entry).
  boot.loader.grub.enable = false;
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;
  # Two generations, no more: the ESP is 1G ("10-esp" below) and every
  # generation parks its own kernel+initrd there (~90M measured on the real
  # box). Unbounded, ~10 OTA applies brick entry-writing on a full ESP.
  # Two = the running system plus exactly one rollback target, which is all
  # hart-ota's canary auto-revert needs.
  boot.loader.systemd-boot.configurationLimit = 2;

  # Build identity, now that the fs labels are stable (see the label comment
  # at the top): which build this image was, readable after boot and from a
  # mounted stick, without depending on the label.
  environment.etc."hart/image-rev".text = hartRev;

  # ── OTA self-naming: a raw node updates to the RAW config ──
  # hart-ota switches to `$FLAKE#${hart.ota.flakeAttr}`. The default is
  # hart-<variant>, which for this image would be the ISO-kind closure --
  # measured on real HW 2026-08-22: it cannot mount a raw root, so every OTA
  # apply staged an unbootable generation. The raw image must name its raw
  # sibling. mkDefault so an operator override still wins. NOTE: only
  # hart-desktop-raw exists as a nixosConfiguration today; a raw server/edge
  # fleet needs real `hart-<variant>-raw` attrs added to flake.nix first
  # (raw-server/raw-edge are currently anonymous packages).
  hart.ota.flakeAttr = lib.mkDefault "hart-${config.hart.variant}-raw";

  # ── Load the store DB on first boot (pairs with /nix-path-registration) ──
  # The image ships the closure's FILES plus a registration manifest (see the
  # "20-root" partition below); this is the half that makes nix believe them.
  # Without it every store path on a dd'd stick is "not valid" to nix, which
  # takes out `nixos-rebuild switch`, the system-profile rollback anchor, and
  # OTA as a whole — measured on real HW 2026-08-20, where the DB knew 1 path.
  #
  # Runs ONCE: the manifest is deleted after a successful load, so this is a
  # no-op on every later boot. `|| true` and the -f guard are deliberate — a
  # box that cannot register its store must still BOOT (it simply stays as
  # un-updatable as it is today), which is the never-fail posture the rest of
  # this image is built on. Same contract as nixpkgs' sd-image.nix.
  boot.postBootCommands = ''
    if [ -f /nix-path-registration ]; then
      echo "[HART OS] registering the initial Nix store (first boot)..."
      if ${config.nix.package.out}/bin/nix-store --load-db < /nix-path-registration; then
        rm -f /nix-path-registration
        echo "[HART OS] store registered — nixos-rebuild/OTA can now reference it."
      else
        echo "[HART OS] store registration FAILED — OTA stays unavailable; boot continues." >&2
      fi
    fi
  '';

  # ── Installed-system filesystems (previously supplied by nixos-generators) ──
  # device is a stable by-label path so no root= cmdline is needed; the labels are
  # set on the partitions below. autoResize + growPartition are ALSO set by
  # desktop.nix's hartImageKind=="raw" branch — identical values merge (bool
  # mergeEqualOption), so this module stays self-contained without conflicting.
  fileSystems."/" = {
    device = "/dev/disk/by-label/${rootLabel}";
    fsType = "ext4";
    autoResize = true;
  };
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/${espLabel}";
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
          Label = espLabel;
          # Must fit the desktop UKI (kernel + full initrd in one .efi). Generous
          # to start; tighten after a real boot proves the UKI size.
          SizeMinBytes = "1G";
        };
      };
      "20-root" = {
        storePaths = [ config.system.build.toplevel ];
        # ── The store DB registration, without which NOTHING nix works ──
        # `storePaths` copies the closure's FILES into the partition but ships no
        # database: systemd-repart has no notion of nix's sqlite. Verified on the
        # flashed box 2026-08-20 — the DB knew exactly ONE path, db.sqlite was an
        # empty 45KB schema, and even the RUNNING system answered:
        #   error: path '/nix/store/...-nixos-system-hart-node-...' is not valid
        #
        # Consequences, all of which were live on that machine:
        #   * `nixos-rebuild switch` can reference nothing it already has, so an
        #     OTA would try to fetch an entire system closure (~22 GiB measured
        #     below) into the 3.1G the stick had free. OTA could never work.
        #   * `nix-env --set` on the system profile fails outright ("don't know
        #     how to build these paths"), so the rollback anchor cannot even be
        #     created — hart-first-boot's registration step needs this to succeed.
        #   * a `nix-collect-garbage` would consider the whole store unreachable.
        #
        # This is the standard NixOS image contract (sd-image.nix ships the same
        # file and loads it in postBootCommands); the repart path simply never
        # adopted it. closureInfo computes the registration for the SAME closure
        # storePaths already copies, so it adds a manifest, not another copy.
        contents = {
          "/nix-path-registration".source =
            "${pkgs.closureInfo { rootPaths = [ config.system.build.toplevel ]; }}/registration";
        };
        repartConfig = {
          Type = "root";          # x86-64 root discoverable-partition GUID
          Format = "ext4";
          Label = rootLabel;
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
          #
          # ── 26G, and THIS bound comes from the TARGET DEVICE, not from CI ──
          # Every earlier number here (44G, 40G, 28G) was chosen against GitHub
          # runner disk pressure. That is the BUILD constraint. The image also has to
          # land on a real stick, which is the SHIP constraint, and it is smaller:
          #
          #   1 GiB ESP + 28 GiB root = 29.0 GiB image
          #   SanDisk Cruzer Blade    = 28.7 GiB usable  -> DOES NOT FIT, by ~0.3 GiB
          #
          # It would have failed at write time, after a 7.4 GB download, having built
          # and published cleanly -- neither constraint is discoverable from the other,
          # so both belong in this comment. 1 GiB ESP + 26 GiB root = 27 GiB leaves
          # ~1.7 GiB spare on a nominal "32 GB" stick.
          #
          # 26G still clears the payload: the built image measured 25 GiB allocated
          # (du on hart-os.raw, run 30336832563), of which ~24 GiB is the root
          # filesystem -- 21.9 GiB of closure plus ext4 metadata, journal and the UKI.
          # That leaves ~2 GiB of slack for closure growth.
          #
          # Too big for the runner fails as a SILENT ENOSPC mid-copy (six builds, see
          # above). Too big for the stick fails at flash time. Check both.
          SizeMinBytes = "26G";
          SizeMaxBytes = "26G";
        };
      };
    };
  };
}
