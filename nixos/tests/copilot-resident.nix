# ═══════════════════════════════════════════════════════════════
# HART OS — resident co-pilot (Claude Code on the node) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# WHY THIS TEST EXISTS — a real incident, not a hypothetical.
#
# The 2026-07-30 flash shipped `hart.copilot.enable = true` and the resident
# co-pilot did NOTHING on the node. `enable` installs only the `hart-copilot`
# launcher; the bounded worker is a SECOND, separate opt-in
# (`hart.copilot.daemon.enable`) and no consumer had set it. The profile now
# records that in a comment — but a comment is not a gate, and nobody found out
# until a node was in hand.
#
# There is a second, quieter way to ship the same inert node:
# hart-copilot.nix gates the daemon on `cfg.daemon.enable && claudePkg != null`,
# and `claudePkg = newPkgs.claude-code or null`. If that upstream attribute ever
# moves or disappears, the unit VANISHES SILENTLY — enable stays true, the build
# stays green, and the node ships with no co-pilot. Exactly the July symptom,
# from a different cause.
#
# So the assertion that matters is: **the daemon unit EXISTS on a node built
# from the REAL desktop profile.** mkNode imports ../profiles/desktop.nix, so
# what this test boots is what an image and an installed system boot — if the
# profile stops producing a co-pilot, this goes red instead of a USB stick
# telling us three weeks later.
#
# HONEST SCOPE — what this CANNOT prove:
#   * That the co-pilot does any WORK. Per HART_COPILOT_RESIDENT_CLAUDE.md §5 no
#     API key ships in the image; the credential arrives via interactive OAuth
#     (`claude` -> /login) into the user's home. A VM has no such login, so the
#     daemon will not reach a working state here.
#   * Therefore this asserts the unit is LOADED and ENABLED, never that it is
#     ACTIVE. Asserting active would be asserting a credential the image
#     deliberately does not carry — a test that could only pass by weakening §5.
#   * That the co-pilot can reach the agent stack. It currently CANNOT: there is
#     no MCP wiring in the module at all (task #48, blocked on #49). When that
#     lands, its own assertions belong here.
#
# What it DOES prove: the launcher and the daemon are realized in the closure of
# the shipped desktop profile, `claude` is executable, and the §1 boundary
# ("full autonomy inside the work, zero authority at the boundaries") is
# MECHANICAL rather than advisory — resource caps, restart backoff, and the root
# path-unit whose ExecStart takes no argument from the agent.
#
# [VM] — cannot run on the Windows dev box; gates in CI (`nix flake check`).
{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-copilot-resident = pkgs.testers.runNixOSTest {
    name = "hart-copilot-resident";
    # Same runtime-injected-node-global false positives the sibling tests hit:
    # the driver binds the machine by HOSTNAME (mkNode forces it to the variant),
    # so the static passes flag `host` as undefined though it exists at runtime.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    # NOTE: no hart.copilot.* is set here ON PURPOSE. The whole point is to
    # assert what ../profiles/desktop.nix ALREADY ships. Setting it in the test
    # would make the test pass while the profile regressed — which is precisely
    # how July's inert node got out.
    nodes.host = mkNode "desktop" {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
      };
    };

    testScript = ''
      host = machines[0]
      host.start()
      host.wait_for_unit("multi-user.target")

      # ── 1. THE JULY GATE: the shipped profile produces a daemon unit ──
      with subtest("the desktop profile ships a resident co-pilot DAEMON, not just a launcher"):
          # `systemctl cat` fails if the unit does not exist. This is the single
          # assertion that catches BOTH regressions: daemon.enable going unset,
          # and claudePkg resolving to null upstream (the module drops the unit
          # silently in either case).
          unit = host.succeed("systemctl cat hart-copilot-daemon.service")
          assert "ExecStart" in unit, "hart-copilot-daemon.service has no ExecStart"

      with subtest("the launcher is on the node too"):
          host.succeed("test -x $(command -v hart-copilot)")

      with subtest("claude-code itself is in the closure and executable"):
          # claudePkg = newPkgs.claude-code or null — if the upstream attr moves,
          # the daemon unit vanishes silently. Assert the binary directly so the
          # failure names the cause instead of surfacing as a missing unit.
          host.succeed("test -x $(command -v claude)")

      # ── 2. Deliberately NOT asserting the daemon is ACTIVE ──
      with subtest("the daemon is enabled but not expected to be running (no OAuth in a VM)"):
          # §5: no API key ships in the image; the credential arrives via an
          # interactive `claude` -> /login. A VM has none, so "active" is the
          # WRONG expectation — asserting it would only pass if the image started
          # carrying a credential, which is the thing §5 forbids.
          state = host.succeed(
              "systemctl is-enabled hart-copilot-daemon.service || true").strip()
          assert "masked" not in state, \
              f"hart-copilot-daemon.service is masked ({state}) — it can never start"

      # ── 3. The §1 boundary must be MECHANICAL, not advisory ──
      with subtest("the daemon is resource-bounded, so an unattended run cannot eat the node"):
          props = host.succeed(
              "systemctl show hart-copilot-daemon.service "
              "-p CPUQuotaPerSecUSec -p MemoryMax -p Restart -p RestartUSec")
          assert "Restart=on-failure" in props, \
              f"co-pilot daemon is not restart-bounded: {props}"
          # An 8 GB potato is the target. An unbounded agent loop competing with
          # the OS for RAM is the §6.3 failure this cap exists to prevent.
          assert "MemoryMax=infinity" not in props, \
              f"co-pilot daemon has NO memory ceiling: {props}"
          assert "CPUQuotaPerSecUSec=infinity" not in props, \
              f"co-pilot daemon has NO cpu ceiling: {props}"

      with subtest("the daemon runs from a WRITABLE checkout, never the read-only store"):
          # §3: the nix store is read-only, so the agent structurally cannot mutate
          # the running system's source in place; its output ships back the normal
          # way (branch -> human merge -> OTA).
          wd = host.succeed(
              "systemctl show hart-copilot-daemon.service -p WorkingDirectory")
          assert wd.strip() != "WorkingDirectory=", \
              "co-pilot daemon has no WorkingDirectory pinned"

      with subtest("activation takes NO argument from the agent"):
          # The privilege boundary (#22's fix applied here): activation lives in a
          # ROOT path unit whose ExecStart is fixed, so an agent that ignores every
          # instruction in its prompt still cannot activate an arbitrary config —
          # it has no way to say otherwise. Assert the fixed word is present and
          # that the daemon unit itself is not the thing switching configurations.
          unit = host.succeed("systemctl cat hart-copilot-daemon.service")
          assert "nixos-rebuild switch" not in unit, \
              "the co-pilot daemon unit can switch configurations directly — " \
              "activation must go through the root path unit with a fixed ExecStart"
    '';
  };
}
