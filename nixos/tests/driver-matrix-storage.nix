# ═══════════════════════════════════════════════════════════════
# HART OS — driver matrix, STORAGE-CONTROLLER slice (nvme + ahci)
# ═══════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS
#   driver-matrix.nix proves the kernel BINDS a driver to xhci / usbhid / HDA /
#   e1000 / virtio — but its own header (its lines 28-32) DEFERS the storage
#   controllers that need a backing disk (nvme, ahci, usb-storage) to a "second
#   slice", claiming only usb_storage is already covered (by hart-boot-root-
#   initrd, which boots a USB root). That left NVMe and SATA/AHCI — the
#   INSTALLED raw image's PRIMARY boot media on real hardware (an M.2 NVMe on
#   every modern laptop, a 2.5" SATA SSD on the rest) — with their driver
#   binding proven NOWHERE in a VM. The live ISO boots from USB (proven); the
#   installed image boots from an internal disk (unproven, until here).
#
#   The source-shape half is guarded by
#   tests/unit/test_nixos_configs.py::TestRawImageSinglePath
#   .test_repart_initrd_covers_internal_disk_roots_sata_and_nvme (the modules
#   are pinned in the initrd list). This is the BEHAVIOURAL half: attach a real
#   NVMe controller and a real ICH9 AHCI (SATA) controller, boot the REAL
#   desktop variant, and assert the kernel actually BOUND `nvme` / `ahci` to
#   them — the "boots in CI from virtio, VFS-panics on the real NVMe stick" gap,
#   for the disk types the image is actually meant to run from.
#
# NO BACKING FILE — deliberate, like driver-matrix.nix's device choices. QEMU's
# `null-co` block driver presents a zeroed virtual disk with no host file, so
# this needs no emptyDiskImages plumbing and cannot ENOSPC a runner: the disks
# exist only to give each controller something to enumerate so its driver binds.
#
# ONE NODE, BOTH CONTROLLERS — a VM job here costs ~2h; nvme and ahci are on
# different buses and do not mask each other (each assertion names its own
# driver), so two single-controller nodes would buy the same coverage for twice
# the wall clock. Same reasoning driver-matrix.nix states for its own node.
#
# #70 discipline: built from `hartModules` + the desktop PROFILE via the shared
# mkNode — what a test boots is what an image and an installed system boot.
{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-driver-matrix-storage = pkgs.testers.runNixOSTest {
    name = "hart-driver-matrix-storage";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.stg = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        qemu.options = [
          # ── NVMe controller (binds `nvme`) — an M.2 SSD's controller ──
          # `null-co` = zeroed disk, no host file. `serial=` is REQUIRED by the
          # nvme device model; `drive=` points at the blockdev node below.
          "-device" "nvme,serial=HARTNVME0,drive=hartnvme"
          "-blockdev" "driver=null-co,node-name=hartnvme,read-zeroes=on"
          # ── ICH9 AHCI SATA controller (binds `ahci`) — a 2.5\" SSD's path ──
          # ich9-ahci is the exact AHCI controller real Intel chipsets present;
          # the ide-hd hangs a disk off its port 0 so libata/ahci enumerates it.
          "-device" "ich9-ahci,id=hartahci"
          "-device" "ide-hd,drive=hartsata,bus=hartahci.0"
          "-blockdev" "driver=null-co,node-name=hartsata,read-zeroes=on"
        ];
      };
    };

    testScript = ''

      stg = machines[0]
      stg.start()
      stg.wait_for_unit("multi-user.target")

      def bound_devices(bus, driver):
          """Bus addresses currently BOUND to <driver>, read from sysfs.

          Copied verbatim from driver-matrix.nix (the proven, twice-corrected
          version): the kernel symlinks each BOUND device under the driver's
          directory by its bus address; the control files (bind/unbind/uevent/
          new_id) are regular files, and `module` is the one non-device symlink.
          That distinction is STRUCTURAL and identical on every bus, so it needs
          no per-bus name pattern and cannot go stale — the two CI reds this
          idiom already cost driver-matrix.nix were both from guessing name
          shapes, so it no longer guesses."""
          out = stg.succeed(
              f"find /sys/bus/{bus}/drivers/{driver}/ -maxdepth 1 -type l "
              f"-printf '%f\\n' 2>/dev/null || true"
          )
          return [e for e in (l.strip() for l in out.splitlines())
                  if e and e != "module"]

      def assert_bound(bus, driver, what):
          devs = bound_devices(bus, driver)
          stg.log(f"{what}: driver={driver} bus={bus} bound={devs}")
          assert devs, (
              f"{what}: NO device is bound to '{driver}' on the {bus} bus.\n"
              f"The controller was attached to the VM, so this is the "
              f"unclaimed-hardware case an INSTALLED image hits when its initrd "
              f"lacks the driver — the machine 'VFS: unable to mount root fs' "
              f"bricks on that disk type.\n"
              f"--- /sys/bus/{bus}/drivers/{driver}/ (raw) ---\n"
              + stg.succeed(
                  f"ls -1 /sys/bus/{bus}/drivers/{driver}/ 2>/dev/null || true")
              + f"--- /sys/bus/{bus}/devices/ ---\n"
              + stg.succeed(
                  f"ls -1 /sys/bus/{bus}/devices/ 2>/dev/null | head -40 || true")
              + f"--- drivers on the {bus} bus ---\n"
              + stg.succeed(f"ls -1 /sys/bus/{bus}/drivers/ 2>/dev/null | head -40 || true")
          )

      with subtest("NVMe controller binds the 'nvme' driver (internal M.2 root path)"):
          assert_bound("pci", "nvme", "NVMe controller")

      with subtest("AHCI/SATA controller binds the 'ahci' driver (SATA SSD root path)"):
          assert_bound("pci", "ahci", "AHCI SATA controller")

      # Block-device enumeration is LOGGED, not asserted: binding above is the
      # gate (a bound driver is the proof the initrd carries what a root on this
      # disk type needs), while device NAMES vary with probe order. The NVMe
      # namespace name is deterministic, so it is the one hard block check.
      with subtest("the NVMe namespace enumerates as a block device"):
          stg.succeed("udevadm settle || true")
          stg.succeed("test -b /dev/nvme0n1")

      with subtest("the attached disks are visible to the block layer (diagnostic)"):
          stg.log("lsblk:\n" + stg.succeed("lsblk -o NAME,TRAN,SIZE,TYPE || true"))
          # ata_port presence corroborates the AHCI disk enumerated, without
          # depending on the /dev/sdX name the probe order assigns.
          stg.log("ata ports:\n" + stg.succeed(
              "ls -1 /sys/class/ata_port/ 2>/dev/null || true"))
    '';
  };
}
