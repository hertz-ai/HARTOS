# ═══════════════════════════════════════════════════════════════
# HART OS — hart-install dual-boot survival nixosTest (plan step 6)
# ═══════════════════════════════════════════════════════════════
#
# The dual-boot promise, tested: a disk carrying a fake "Windows" ESP gets HART
# installed beside it, and the Windows boot files SURVIVE BYTE-IDENTICAL. Plus
# the union promise: what lands on the target composes hart.lib.mkInstalledSystem
# (profile + hardware-configuration.nix), never a stock NixOS config.
#
# HONEST SCOPE: runs `hart-install --no-install` — everything REAL up to and
# including the composed target (format exactly one partition, reuse the ESP,
# nixos-generate-config, write the union flake + boot.nix, copy the offline
# source tree) but NOT the closure build: `nixos-install` on the full desktop
# closure is a multi-GB build a shard VM cannot carry. The composition itself is
# eval-gated upstream on every push (nixosConfigurations.hart-desktop-installed
# builds the SAME mkInstalledSystem the written flake calls), so the seam skips
# repeating that proof, not making it. The full install→reboot→both-OSes-boot
# end needs a bigger VM budget or real hardware and stays listed in the plan.
#
# #70 discipline: mkNode from hartModules alone; installer enabled per-test.
{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-installer-dualboot = pkgs.testers.runNixOSTest {
    name = "hart-installer-dualboot";
    node.specialArgs = specialArgs;

    nodes.machine = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # The "user's disk": 2 GiB spare, partitioned by the test into a fake
        # Windows layout (ESP + empty space for HART's root).
        emptyDiskImages = [ 2048 ];
      };
      hart.installer.enable = true;
      environment.systemPackages = [ pkgs.gptfdisk pkgs.dosfstools ];
    };

    testScript = ''
      machine.start()
      machine.wait_for_unit("multi-user.target")

      with subtest("installer tooling is on the live system"):
          machine.succeed("command -v hart-install")
          machine.succeed("command -v hart-write-install-config")
          machine.succeed("command -v nixos-generate-config")
          # The offline story: the HART source tree is baked into the medium.
          machine.succeed("test -e /etc/hart/src/nixos/flake.nix")

      with subtest("build the fake dual-boot disk: ESP with 'Windows' + free root partition"):
          machine.succeed(
              "sgdisk --zap-all /dev/vdb",
              "sgdisk -n 1:0:+256M -t 1:ef00 -c 1:ESP /dev/vdb",
              "sgdisk -n 2:0:0     -t 2:8300 -c 2:root /dev/vdb",
              "udevadm settle",
              "mkfs.vfat -F32 /dev/vdb1",
              "mkdir -p /tmp/esp",
              "mount /dev/vdb1 /tmp/esp",
              # The files whose survival IS the dual-boot promise. BOOTX64.EFI is
              # Windows' fallback loader — the one thing an installer must never
              # replace.
              "mkdir -p /tmp/esp/EFI/Microsoft/Boot /tmp/esp/EFI/BOOT",
              "echo WINDOWS-BOOTMGR > /tmp/esp/EFI/Microsoft/Boot/bootmgfw.efi",
              "echo WINDOWS-FALLBACK > /tmp/esp/EFI/BOOT/BOOTX64.EFI",
              # RELATIVE paths, recorded from inside the mount: the check later
              # runs from a DIFFERENT mountpoint, and absolute /tmp/esp/... paths
              # would dangle after umount, failing the check for the wrong
              # reason (review C:C2).
              "cd /tmp/esp && sha256sum EFI/Microsoft/Boot/bootmgfw.efi EFI/BOOT/BOOTX64.EFI > /tmp/windows-esp.sha",
              "umount /tmp/esp",
          )

      with subtest("hart-install refuses a non-block-device target"):
          machine.fail("hart-install --root /dev/null --yes --no-install")

      with subtest("hart-install composes the target (esp reused, --no-install seam)"):
          # The test VM boots BIOS (no /sys/firmware/efi), so the ESP would not
          # be probed; pass it explicitly — the reuse path is identical.
          out = machine.succeed(
              "hart-install --root /dev/vdb2 --esp /dev/vdb1 --variant desktop "
              "--hostname dualboot-test --yes --no-install 2>&1")
          assert "not built" in out, f"--no-install must stop before the build: {out}"

      with subtest("exactly one partition was formatted: root is fresh ext4, ESP untouched"):
          fstype = machine.succeed("lsblk -no FSTYPE /dev/vdb2").strip()
          assert fstype == "ext4", f"root must be ext4, got: {fstype!r}"
          machine.succeed("mount | grep -q '/dev/vdb2 on /mnt'")

      with subtest("THE DUAL-BOOT PROMISE: the Windows boot files survive byte-identical"):
          machine.succeed(
              "mkdir -p /tmp/esp2",
              # On EFI firmware the ESP would still be mounted at /mnt/boot; on
              # this BIOS VM hart-install takes the grub path and leaves the ESP
              # alone entirely — mount it fresh and verify the recorded hashes
              # (relative paths, so -c re-anchors at the new mountpoint).
              "umount /mnt/boot 2>/dev/null || true",
              "mount -o ro /dev/vdb1 /tmp/esp2",
              "cd /tmp/esp2 && sha256sum -c /tmp/windows-esp.sha",
          )

      with subtest("the composed target is the UNION, not stock NixOS"):
          flake = machine.succeed("cat /mnt/etc/nixos/flake.nix")
          assert "mkInstalledSystem" in flake, "must compose hart.lib.mkInstalledSystem"
          assert 'variant = "desktop"' in flake, "variant must be threaded through"
          assert "hardware-configuration.nix" in flake, "NixOS hardware layer missing"
          machine.succeed("test -s /mnt/etc/nixos/hardware-configuration.nix")
          machine.succeed("test -s /mnt/etc/nixos/boot.nix")
          machine.succeed("test -s /mnt/etc/nixos/hostname.nix")
          # Offline forever: the source tree travelled to the target.
          machine.succeed("test -e /mnt/etc/hart/src/nixos/flake.nix")
          # BIOS VM -> grub path with the DISK substituted in (never @GRUB_DEVICE@).
          machine.succeed("grep -q 'useOSProber = true' /mnt/etc/nixos/boot.nix")
          machine.fail("grep -q '@GRUB_DEVICE@' /mnt/etc/nixos/boot.nix")

      with subtest("a second run onto the same root is not blocked by the first"):
          machine.succeed("umount -R /mnt")
          machine.succeed(
              "hart-install --root /dev/vdb2 --esp /dev/vdb1 --yes --no-install")
    '';
  };
}
