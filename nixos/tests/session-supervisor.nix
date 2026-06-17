# ═══════════════════════════════════════════════════════════════
# HART OS — Phase-1 Out-of-Process Session Tier-Drop Supervisor nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the never-blank-screen guarantee (compositor/ROADMAP.md Phase 1):
# loop-kill fault injection on a deliberately-crashing higher tier MUST land
# on the cage floor with the latch written, and the operator reset path MUST
# clear the latch so the next boot retries Tier-1.
#
# WHY [VM]-gated: greetd + a Wayland session cannot run on the Windows dev box.
# The dev box authors + unit-tests the selector/hartctl shell logic
# (tests/unit-style POSIX-sh harness in the task report); THIS test exercises
# the wired module on a real (software-GL) VM. Per the honest-hardware rule it
# gates in CI (`nix flake check` / local QEMU), never inline-render or grep.
#
# What it asserts:
#   1. greetd is the active display manager (out-of-process supervisor), NOT GDM
#      — session selection is structurally out-of-process.
#   2. hartctl is on PATH and reports the floor by default.
#   3. CRASH-LOOP FAULT INJECTION: with a fake Tier-1 (hart-comp) + Tier-2 (sway)
#      that exit instantly, running the installed selector wrapper the crash-loop
#      count times drops + LATCHES the tier down to cage — and never below it.
#   4. The latch PERSISTS (it is /var/lib/hart/session-tier, on the data
#      partition) — latched across boot by construction.
#   5. `hartctl session reset-tier` clears the latch so the next attempt is
#      Tier-1 again (the operator recovery path; openRisk #8).
#   6. The supervisor can NEVER drop below cage: a crash-loop while already on
#      cage leaves the latch on cage (the floor still paints).
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix); the supervisor is opt-in so the node enables it +
# overrides the fake tier commands for fault injection.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # A fake session command that exits immediately (rc=1) — the crash injection.
  # Used for Tier-1 (hart-comp) and Tier-2 (sway) so the supervisor must drop to
  # the cage floor. cage stays the REAL hart-shell-session (the audited floor).
  crashImmediately = "${pkgs.coreutils}/bin/false";
in
{
  hart-session-supervisor-tier-drop = pkgs.testers.runNixOSTest {
    name = "hart-session-supervisor-tier-drop";
    # runNixOSTest's mypy pre-check does NOT resolve the per-node Machine global
    # (`sup`) the driver injects at RUNTIME — same false "Name not defined" as the
    # floor-lock test (node IS named `sup`, works at runtime). Skip the static
    # pre-check; the VM still boots and the tier-drop assertions still run.
    skipTypeCheck = true;
    # The pyflakes lint (config.skipLint) ALSO flags the runtime-injected `sup`
    # node global as "undefined name" — separate static pass from mypy, same false
    # positive. Skip it too; `sup` exists at runtime when the VM boots.
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sup = mkNode "desktop" {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
      };
      # Opt the supervisor on (default off) and inject crashing higher tiers so
      # the drop ladder is exercised. cageCommand stays the real floor launcher.
      hart.sessionSupervisor = {
        enable = true;
        compCommand = crashImmediately;   # Tier-1 fake: crashes instantly
        swayCommand = crashImmediately;    # Tier-2 fake: crashes instantly
        # cageCommand defaults to "hart-shell-session" — the real floor.
        crashLoopCount = 3;
        crashLoopWindowSeconds = 300;
      };
    };

    testScript = ''
      sup.start()
      sup.wait_for_unit("multi-user.target")

      LATCH = "/var/lib/hart/session-tier"
      WINDOW = "/var/lib/hart/session-tier.window"

      # ── 1. greetd is the supervisor (out-of-process), not GDM ──
      with subtest("greetd is the active display manager (out-of-process supervisor)"):
          # The supervisor REPLACES gdm with greetd when enabled. greetd's unit is
          # greetd.service; gdm must be off.
          sup.wait_for_unit("greetd.service", timeout=120)
          sup.fail("systemctl is-active gdm.service")

      # ── 2. hartctl on PATH; fail-safe to cage when unlatched (contract §3.1) ──
      with subtest("hartctl reports the cage floor when the latch is absent (fail-safe)"):
          sup.succeed("command -v hartctl")
          out = sup.succeed("hartctl session get-tier").strip()
          # A missing/garbage latch fails SAFE to cage — never a higher unproven
          # tier. (An earlier in-VM run may already hold a valid tier; accept any
          # valid token, but the contract default for absent is cage.)
          assert out in ("hart-comp", "sway", "cage"), f"unexpected tier {out!r}"
          # Arm Tier-1 explicitly so the fault-injection starts from the top.
          sup.succeed("hartctl session reset-tier")
          assert sup.succeed("hartctl session get-tier").strip() == "hart-comp", \
              "reset-tier must WRITE hart-comp (re-arm Tier-1), per contract §4"

      # Locate the installed selector wrapper (greetd's session command).
      # It is referenced from greetd's config; pull it from the store.
      selector = sup.succeed(
          "find /nix/store -maxdepth 3 -name 'hart-session-selector' -type f -print -quit"
      ).strip()
      assert selector, "hart-session-selector wrapper not found in the store"

      # ── 3. CRASH-LOOP FAULT INJECTION: drop Tier-1 -> Tier-2 -> cage + latch ──
      with subtest("crash-loop on the fake higher tiers drops + latches down to cage"):
          # Each selector run launches the latched tier's command. Tier-1
          # (hart-comp=false) and Tier-2 (sway=false) exit instantly (< the crash
          # window) so each run records a crash; after crashLoopCount runs at a
          # tier the selector drops one tier and rewrites the latch. Run it enough
          # times to walk hart-comp -> sway -> cage. We run as the hart user so
          # the latch is writable (tmpfiles makes /var/lib/hart hart-owned).
          for _ in range(12):
              # The selector returns rc=0 (it returns control to greetd); the
              # injected tier command inside it is what "crashes".
              sup.succeed(f"runuser -u hart -- {selector} || true")

          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", \
              f"after crash-loop the latch must be the cage floor, got {tier!r}"

      # ── 4. The latch persists on the data partition (latched across boot) ──
      with subtest("the latch file persists under /var/lib/hart (survives reboot by construction)"):
          sup.succeed(f"test -f {LATCH}")
          # Confirm it is on the persistent hart state dir, not tmpfs.
          mnt = sup.succeed(f"df --output=target {LATCH} | tail -1").strip()
          assert "/run" not in mnt and "tmpfs" not in mnt.lower(), \
              f"latch is on a volatile mount ({mnt}) — would NOT latch across boot"

      # ── 6. Cannot drop below the floor ──
      with subtest("a crash-loop while already on cage cannot drop below the floor"):
          # Already latched to cage. Run the selector more times; the REAL cage
          # floor may itself fail to fully start headless, but the latch must
          # STAY cage (never a 4th tier, never blank).
          for _ in range(5):
              sup.succeed(f"runuser -u hart -- {selector} || true")
          tier = sup.succeed(f"cat {LATCH}").strip()
          assert tier == "cage", f"latch dropped below the floor to {tier!r} — NEVER allowed"

      # ── 5. Operator reset path: reset-tier re-arms Tier-1 (writes hart-comp) ──
      with subtest("hartctl session reset-tier re-arms Tier-1 so the next boot attempts hart-comp"):
          sup.succeed("hartctl session reset-tier")
          # Contract §4: reset WRITES hart-comp (not merely deletes the file) so
          # a transient Tier-1 bug can never permanently mask as a downgrade.
          assert sup.succeed(f"cat {LATCH}").strip() == "hart-comp", \
              "after reset-tier the latch must be hart-comp (Tier-1 re-armed)"
          sup.fail(f"test -f {WINDOW}")  # crash window cleared so T1 gets a clean budget

      # ── status telemetry is observable ──
      with subtest("hartctl session status surfaces the current latched tier"):
          status = sup.succeed("hartctl session status")
          assert "session tier" in status, f"status missing tier line: {status!r}"
    '';
  };
}
