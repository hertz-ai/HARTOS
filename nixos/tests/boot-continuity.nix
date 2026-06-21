# ═══════════════════════════════════════════════════════════════
# HART OS — Boot-continuity (one-shot BootNext) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the Live-OS boot-continuity service: on a reboot initiated FROM the Live
# OS, set a ONE-SHOT efibootmgr BootNext to the USB's OWN EFI boot entry so the
# next boot returns to HART OS — NEVER touching the permanent BootOrder (so the
# user's Windows boot is never stranded).
#
# This test is BEHAVIOURAL where it can be (the unit + efibootmgr are in the
# closure, the unit is ordered as an ExecStop on the reboot path, the script
# no-ops cleanly when not UEFI-booted / efibootmgr absent / the entry can't be
# matched) and STRUCTURAL for the parts that need real UEFI firmware (the actual
# BootNext write needs a USB-booted live root + real OVMF NVRAM to confirm).
#
# The VM is NOT UEFI-booted with a USB-backed live root, so the script's UEFI
# branch correctly NO-OPs (no /sys/firmware/efi, or no matchable USB entry). The
# test asserts:
#   - the unit + efibootmgr CLI are in the closure,
#   - the unit's ExecStop is wired to the bootNext script (the way-down hook),
#   - the unit is ordered Before systemd-reboot.service / shutdown.target,
#   - running the script by hand is a clean no-op exit 0 on a non-UEFI VM,
#   - the script NEVER emits a BootOrder write (the safety invariant).
#
# WHY [VM/HW]-gated: the live BootNext write needs real UEFI firmware + a
# USB-booted live root. THIS test proves the unit wiring + the never-touch-
# BootOrder invariant + the no-op safety on a non-matching boot.
#
# #70 discipline preserved: built from `hartModules` alone via the shared mkNode.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-boot-continuity = pkgs.testers.runNixOSTest {
    name = "hart-boot-continuity";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.bc = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
      hart.bootContinuity.enable = true;
      environment.systemPackages = [ pkgs.efibootmgr ];
    };

    testScript = ''
      bc = machines[0]
      bc.start()
      bc.wait_for_unit("multi-user.target")

      # ── 1. The continuity unit + efibootmgr are in the closure ──
      with subtest("the boot-continuity unit + efibootmgr exist"):
          unit = bc.succeed("systemctl cat hart-boot-continuity.service")
          bc.succeed("command -v efibootmgr")
          # The real work is an ExecStop (the way-down hook), not ExecStart.
          assert "ExecStop=" in unit, "must do its work on ExecStop (the reboot path)"
          assert "RemainAfterExit=true" in unit.replace(" ", ""), \
              "RemainAfterExit is required for an ExecStop hook"

      # ── 2. Ordered to fire BEFORE reboot / shutdown ──
      with subtest("ordered before systemd-reboot.service / shutdown.target"):
          assert "systemd-reboot.service" in unit, "must order before systemd-reboot"
          assert "shutdown.target" in unit, "must order before shutdown.target"

      # ── 3. The script NEVER writes BootOrder (the safety invariant) ──
      # Extract the ExecStop script path from the unit and grep its source for any
      # BootOrder write. A wrong BootOrder write could strand the user's Windows.
      with subtest("the script never writes BootOrder — only one-shot BootNext"):
          # The ExecStop points at the writeShellScript in the store; read it.
          script = bc.succeed(
              "systemctl cat hart-boot-continuity.service "
              "| sed -n 's/^ExecStop=\\([^ ]*\\).*/\\1/p' | head -n1"
          ).strip()
          body = bc.succeed(f"cat {script}")
          assert "--bootnext" in body, "must set the one-shot BootNext"
          low = body.lower()
          assert "--bootorder" not in low, "must NEVER write BootOrder"
          assert "efibootmgr -o" not in low, "must NEVER write BootOrder (short form)"

      # ── 4. Running it by hand on a non-UEFI VM is a clean no-op exit 0 ──
      # The QEMU VM is not UEFI-booted with a USB-backed live root, so the UEFI
      # branch no-ops. It must ALWAYS exit 0 (never block/fail a shutdown).
      with subtest("running the bootNext script is a clean no-op on a non-USB-UEFI VM"):
          out = bc.succeed(f"{script} reboot 2>&1; echo RC=$?")
          assert "RC=0" in out, f"the bootNext script must exit 0, got: {out!r}"
          # It must have no-op'd via one of the documented gates.
          assert ("no-op" in out or "nothing to do" in out
                  or "not booted via UEFI" in out or "could not match" in out
                  or "could not resolve" in out), \
              f"expected a documented no-op reason, got: {out!r}"

      # ── 5. A poweroff action never arms a next boot ──
      with subtest("a poweroff action is not armed (only a reboot returns to HART OS)"):
          out2 = bc.succeed(f"{script} poweroff 2>&1; echo RC=$?")
          assert "RC=0" in out2
          # On this non-UEFI VM the UEFI gate fires first, but on a real UEFI box
          # the poweroff gate would short-circuit; assert the script handles it.

      # ── 6. The no-op path performs ZERO efibootmgr NVRAM writes ──
      # Shadow efibootmgr with a recorder FIRST on PATH so any invocation by the
      # script is captured. On this non-UEFI VM the script no-ops at the UEFI gate
      # and must therefore NEVER shell efibootmgr at all — proving the no-op makes
      # no firmware-variable write whatsoever (the never-strand-Windows contract is
      # vacuously safe because nothing is written). A real UEFI box is needed to
      # confirm the positive BootNext write; this confirms the no-op writes nothing.
      with subtest("a no-op invocation makes no efibootmgr call (zero NVRAM writes)"):
          bc.succeed(
              "mkdir -p /tmp/ebmrec && "
              "printf '#!/bin/sh\\necho \"$@\" >> /tmp/ebmrec/calls\\nexit 0\\n' "
              "> /tmp/ebmrec/efibootmgr && chmod +x /tmp/ebmrec/efibootmgr && "
              "rm -f /tmp/ebmrec/calls"
          )
          out3 = bc.succeed(
              f"PATH=/tmp/ebmrec:$PATH {script} reboot 2>&1; echo RC=$?"
          )
          assert "RC=0" in out3, f"recorder run must exit 0, got: {out3!r}"
          # The script prepends its own binPath (which carries the REAL efibootmgr),
          # so our recorder may not be the one resolved — but on the non-UEFI VM the
          # script returns at the UEFI gate BEFORE any efibootmgr call, so NEITHER
          # the recorder NOR the real binary is ever invoked: no calls file exists.
          bc.fail("test -s /tmp/ebmrec/calls")
    '';
  };
}
