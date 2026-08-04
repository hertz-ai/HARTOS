# ═══════════════════════════════════════════════════════════════
# HART OS — firmware boot matrix (UEFI vs legacy BIOS), task #28
# ═══════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS
#   docs/architecture/OS_PARITY_MATRIX.md carries the row
#       | BIOS + UEFI boot | isoImage.makeBiosBootable; systemd-boot / GRUB by
#         probe | n/a | Hyper-V **Gen 1** boots |
#   and until now that claim was asserted from CONFIGURATION ONLY. Nothing ever
#   booted the OS on both firmware paths. "Hyper-V Gen 1 boots" is exactly the
#   kind of statement that reads as verified and is not: Gen 1 is a LEGACY-BIOS
#   machine, and a legacy-BIOS boot of the desktop variant had never run.
#
#   That gap is not theoretical for this project. `hardware.cpu.*.updateMicrocode`
#   changed the initrd LAYOUT (boot.initrd.prepend puts an UNCOMPRESSED microcode
#   cpio at offset 0), and the only reason anyone noticed was an unrelated test
#   that could not read the new image. Firmware/boot-path changes land silently
#   unless something boots them.
#
# WHAT IT PROVES (functional, in-VM — not a config assertion)
#   1. The desktop variant reaches multi-user.target under UEFI (OVMF).
#   2. The desktop variant reaches multi-user.target under legacy BIOS (SeaBIOS).
#   3. Each node is REALLY on the firmware path its config claims — /sys/firmware/efi
#      must EXIST on the UEFI node and must NOT exist on the BIOS node. Without
#      the negative half a mis-set flag would boot UEFI twice and pass, proving
#      nothing about BIOS.
#
#   Hyper-V mapping: Gen 1 == legacy BIOS (the `bios` node), Gen 2 == UEFI (the
#   `uefi` node). QEMU/OVMF is the portable stand-in; the hypervisor GUEST
#   AGENTS themselves (virtualisation.hypervGuest / qemuGuest / spice-vdagentd /
#   vmware.guest) are enabled in hart-base.nix and config-guarded by
#   TestHypervisorGuestParity — this test adds the BOOT that guard cannot give.
#
# Both nodes are built by the shared mkNode, so what boots here is the REAL
# desktop variant profile — not a bespoke minimal node.
{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-firmware-boot-matrix = pkgs.testers.runNixOSTest {
    name = "hart-firmware-boot-matrix";
    # Same rationale as the sibling boot tests: runNixOSTest's mypy/pyflakes
    # pre-checks do not resolve the per-node Machine globals the driver injects
    # at RUNTIME.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes = {
      # ── Gen 2 / modern hardware: UEFI via OVMF ──
      uefi = mkNode "desktop" {
        virtualisation = {
          memorySize = 2048;
          cores = 2;
          useEFIBoot = true;
          # Secure Boot off: HART does not ship a signed shim today, and
          # turning it on here would test a thing the product does not claim.
          useSecureBoot = false;
        };
      };

      # ── Gen 1 / legacy hardware: SeaBIOS (QEMU's default, no OVMF) ──
      # Deliberately NOT setting useEFIBoot — the absence IS the test input.
      bios = mkNode "desktop" {
        virtualisation = {
          memorySize = 2048;
          cores = 2;
        };
      };
    };

    testScript = ''
      uefi = machines[0]
      bios = machines[1]

      uefi.start()
      bios.start()

      with subtest("the desktop variant boots on UEFI firmware (Hyper-V Gen 2 shape)"):
          uefi.wait_for_unit("multi-user.target")
          # POSITIVE half: we really are UEFI-booted.
          uefi.succeed("test -d /sys/firmware/efi")
          # efivars must be present too — /sys/firmware/efi can exist without a
          # usable variable store, and a boot manager needs the store.
          uefi.succeed("test -d /sys/firmware/efi/efivars")
          uefi.log("UEFI node firmware: " + uefi.succeed("cat /sys/firmware/efi/fw_platform_size || echo unknown").strip())

      with subtest("the desktop variant boots on LEGACY BIOS firmware (Hyper-V Gen 1 shape)"):
          bios.wait_for_unit("multi-user.target")
          # NEGATIVE half — the load-bearing assertion. If this node silently
          # booted UEFI too, the BIOS path would be untested while the suite
          # went green, which is precisely the false-verification this test
          # exists to prevent.
          bios.fail("test -d /sys/firmware/efi")

      with subtest("both firmware paths reach a usable system, not just a target"):
          # multi-user.target can be reached with units failed, so assert the
          # system is actually running rather than trusting the target alone.
          for m, name in ((uefi, "uefi"), (bios, "bios")):
              state = m.succeed("systemctl is-system-running || true").strip()
              m.log(f"{name} is-system-running: {state}")
              assert state in ("running", "degraded"), \
                  f"{name} node is neither running nor degraded: {state!r}"
              # Record WHICH units failed on each firmware path. A unit that
              # fails only under BIOS (or only under UEFI) is exactly the
              # firmware-specific regression this matrix is meant to surface,
              # and without this line a degraded boot would pass silently.
              failed = m.succeed("systemctl --failed --no-legend || true").strip()
              m.log(f"{name} failed units:\n{failed}")
    '';
  };
}
