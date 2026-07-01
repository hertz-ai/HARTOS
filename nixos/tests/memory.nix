# ═══════════════════════════════════════════════════════════════
# HART OS — Memory sanity nixosTest (#157)
# ═══════════════════════════════════════════════════════════════
#
# Proves the hart-memory.nix surface BEHAVIOURALLY in a booted VM (not grep-on-
# source): a desktop node enables hart.memory and we assert, on real running
# systemd + a real kernel:
#
#   1. zram swap is LIVE — a /dev/zram* device exists AND is an ACTIVE swap with
#      priority 100 (so the kernel prefers compressed-RAM swap over any disk swap).
#      This is the compressed-RAM headroom a low-RAM node needs.
#   2. swappiness COORDINATION took effect — vm.swappiness == 100 (hart-memory's
#      mkOverride 900 wins over hart-base's mkDefault 10 on a zram desktop).
#   3. systemd-oomd is ACTIVE — the graceful userspace OOM protector is running.
#   4. the boot-time MEMORY-HEALTH snapshot wrote an honest readout to
#      /run/hart/memory-health (ok=1 + zram_present=1) without changing any kernel
#      state, and the system stays fully up (degrade-not-die: a memory probe can
#      never block/fail the boot).
#
# WHY [VM]-gated: it needs a real Linux kernel with zram + a real systemd-oomd +
# real /proc — it cannot run on the Windows dev box. The probe SCRIPT's
# classification logic is additionally unit-tested on the dev box
# (tests/unit/test_hart_memory_health.py).
#
# #70 discipline preserved: built from `hartModules` alone via the shared mkNode.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-memory = pkgs.testers.runNixOSTest {
    name = "hart-memory";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.mem = mkNode "desktop" {
      virtualisation = {
        # zram disksize is memoryPercent (50) of this RAM; 2 GiB gives a ~1 GiB
        # zram block, plenty to prove the device + active swap.
        memorySize = 2048;
        cores = 2;
      };

      # The dimension under test.
      hart.memory.enable = true;
      # Keep the defaults explicit so the test reads as the contract: zram on
      # (zstd, 50%), oomd on, health probe on.
      hart.memory.zram.enable = true;
      hart.memory.zram.algorithm = "zstd";
      hart.memory.zram.memoryPercent = 50;
      hart.memory.oomProtect = true;
      hart.memory.healthProbe.enable = true;
    };

    testScript = ''
      mem.start()
      mem.wait_for_unit("multi-user.target")

      # ── 1. zram swap is live: a zram device exists + is active swap, priority 100 ──
      with subtest("zram swap is active with priority 100"):
          # The zram swap unit (NixOS names it dev-zram0.swap) comes up.
          mem.wait_for_unit("dev-zram0.swap")
          mem.succeed("test -e /dev/zram0")
          # /proc/swaps lists the zram device as an ACTIVE swap.
          swaps = mem.succeed("cat /proc/swaps")
          assert "zram0" in swaps, f"zram must be an active swap: {swaps!r}"
          # Priority 100 (the last column of /proc/swaps for the zram row).
          prio = mem.succeed(
              "awk '/zram0/ {print $NF}' /proc/swaps"
          ).strip()
          assert prio == "100", f"zram swap priority must be 100, got {prio!r}"

      # ── 2. swappiness coordination: hart-memory's 100 wins on a zram desktop ──
      with subtest("vm.swappiness is coordinated to 100 (mkOverride beats the base mkDefault)"):
          sw = mem.succeed("cat /proc/sys/vm/swappiness").strip()
          assert sw == "100", f"vm.swappiness must be 100 with zram on, got {sw!r}"

      # ── 3. systemd-oomd is the active graceful OOM protector ──
      with subtest("systemd-oomd is active"):
          mem.wait_for_unit("systemd-oomd.service")
          mem.succeed("systemctl is-active systemd-oomd")

      # ── 4. the boot-time memory-health snapshot wrote an honest readout ──
      with subtest("the memory-health snapshot wrote an honest verdict + the OS stays up"):
          # Start it directly (deterministic, independent of greetd ordering); it is
          # read-only + always exits 0.
          mem.succeed("systemctl start hart-memory-health.service")
          mem.succeed("test -f /run/hart/memory-health")
          health = mem.succeed("cat /run/hart/memory-health")
          assert "ok=1" in health, f"memory-health must record ok=1: {health!r}"
          assert "zram_present=1" in health, \
              f"memory-health must see the live zram device: {health!r}"
          # DEGRADE-NOT-DIE: the probe changed no kernel state + the OS is fully up.
          mem.require_unit_state("multi-user.target", "active")
    '';
  };
}
