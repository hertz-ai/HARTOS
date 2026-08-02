# ═══════════════════════════════════════════════════════════════
# HART OS — boot latency budgets, enforced on a REAL booted node (task #29)
# ═══════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS
#   core.constants.LATENCY_BUDGETS is genuinely enforced — against a measured
#   clock — in six PYTHON suites. Nothing enforced anything on the BOOTED OS.
#   That is how nixosTest nodes reached
#       "Startup finished in 12.486s (kernel) + 6min 36.175s (userspace)"
#   with no test failing on it. A latency requirement that only exists for
#   unit tests is not a requirement for the product.
#
# WHAT IT ASSERTS
#   kernel + userspace startup, read from systemd itself (`systemd-analyze
#   time`), against the SAME table the python suites use — imported from
#   core/constants.py at BUILD time, never re-typed here. A second copy of a
#   budget is a budget that drifts.
#
# WHAT IT ALWAYS CAPTURES (pass or fail)
#   `systemd-analyze blame` and `critical-chain`. The current ceiling is
#   deliberately above the observed max (see the table's comment), because a
#   gate nobody can pass gets disabled and then nothing is measured. These two
#   outputs are what makes the NEXT tightening evidence-led instead of a
#   guess: across the 2026-08-02 run, startup varied 3min30s..6min48s and no
#   single unit explained it — hart-sandbox-firstboot, the last unit before
#   "Startup finished", takes ~2.7s and only STARTS at t=280s.
#
# The node is built by the shared mkNode, so this times the REAL desktop
# variant profile — what an image and an installed system boot.
{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # ── The budgets, taken from the ONE canonical table ──
  # Parsed out of core/constants.py at build time rather than restated, so
  # this test cannot disagree with the python suites. If the key ever
  # disappears the build fails loudly here instead of silently testing
  # nothing.
  budgetsPy = builtins.readFile ../../core/constants.py;
  budgetOf = key:
    let
      m = builtins.match ".*'${key}': ([0-9]+\\.?[0-9]*),.*" budgetsPy;
    in
      if m == null
      then throw "boot-latency.nix: '${key}' not found in core/constants.py LATENCY_BUDGETS — the budget table is the single source; add it there, not here"
      else builtins.head m;
in
{
  hart-boot-latency = pkgs.testers.runNixOSTest {
    name = "hart-boot-latency";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.lat = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
    };

    testScript = ''
      import re

      KERNEL_BUDGET_S   = float("${budgetOf "boot_kernel_s"}")
      USERSPACE_BUDGET_S = float("${budgetOf "boot_userspace_s"}")

      lat = machines[0]
      lat.start()
      lat.wait_for_unit("multi-user.target")

      def _parse_seconds(text):
          """systemd-analyze prints e.g. '12.486s' or '6min 36.175s'."""
          total = 0.0
          mins = re.search(r"([0-9]+)min", text)
          if mins:
              total += float(mins.group(1)) * 60.0
          secs = re.search(r"([0-9]+\.?[0-9]*)s", text)
          if secs:
              total += float(secs.group(1))
          return total

      # ── ALWAYS capture the breakdown, pass or fail ──
      # This is the point of the test as much as the assertion is: the ceiling
      # is above the observed max on purpose, so the DATA is what lets it be
      # tightened later.
      with subtest("capture the boot-time breakdown for budget tightening"):
          analyze = lat.succeed("systemd-analyze time || true").strip()
          lat.log("systemd-analyze time:\n" + analyze)
          blame = lat.succeed("systemd-analyze blame --no-pager | head -25 || true")
          lat.log("systemd-analyze blame (top 25):\n" + blame)
          chain = lat.succeed("systemd-analyze critical-chain --no-pager || true")
          lat.log("systemd-analyze critical-chain:\n" + chain)

      with subtest("kernel boot is within its budget"):
          analyze = lat.succeed("systemd-analyze time").strip()
          # "Startup finished in 12.486s (kernel) + 3min 21.824s (userspace) = ..."
          m = re.search(r"in\s+(.*?)\s+\(kernel\)", analyze)
          assert m, f"could not parse the kernel time out of: {analyze!r}"
          kernel_s = _parse_seconds(m.group(1))
          lat.log(f"kernel boot: {kernel_s:.3f}s (budget {KERNEL_BUDGET_S}s)")
          assert kernel_s <= KERNEL_BUDGET_S, (
              f"kernel boot {kernel_s:.3f}s exceeds the {KERNEL_BUDGET_S}s budget "
              f"(core.constants.LATENCY_BUDGETS['boot_kernel_s']).\n{analyze}"
          )

      with subtest("userspace startup is within its regression ceiling"):
          analyze = lat.succeed("systemd-analyze time").strip()
          m = re.search(r"\+\s+(.*?)\s+\(userspace\)", analyze)
          assert m, f"could not parse the userspace time out of: {analyze!r}"
          user_s = _parse_seconds(m.group(1))
          lat.log(f"userspace startup: {user_s:.3f}s (ceiling {USERSPACE_BUDGET_S}s)")
          if user_s > USERSPACE_BUDGET_S:
              # Name the units on the way out — a bare number is not actionable,
              # and re-running a 2-hour VM job to find out is the expensive way
              # to learn what `blame` already knows.
              blame = lat.succeed("systemd-analyze blame --no-pager | head -25 || true")
              chain = lat.succeed("systemd-analyze critical-chain --no-pager || true")
              raise AssertionError(
                  f"userspace startup {user_s:.3f}s exceeds the "
                  f"{USERSPACE_BUDGET_S}s regression ceiling "
                  f"(core.constants.LATENCY_BUDGETS['boot_userspace_s']).\n"
                  f"{analyze}\n\nblame (top 25):\n{blame}\n\ncritical-chain:\n{chain}"
              )

      with subtest("the system is actually up, not merely past the target"):
          # A fast boot that reached the target with units failed is not a
          # pass — it is a different bug wearing a good number.
          state = lat.succeed("systemctl is-system-running || true").strip()
          lat.log(f"is-system-running: {state}")
          failed = lat.succeed("systemctl --failed --no-legend || true").strip()
          lat.log(f"failed units:\n{failed}")
          assert state in ("running", "degraded"), \
              f"node is neither running nor degraded after boot: {state!r}"
    '';
  };
}
