# ═══════════════════════════════════════════════════════════════
# HART OS — Display management nixosTest (hart-display.nix)
# ═══════════════════════════════════════════════════════════════
#
# Proves the boot-safe, degrade-not-die display module SHIPS its capabilities and
# never threatens the boot:
#   1. TOOLS: wlr-randr + kanshi are in the closure / PATH (the settings backend's
#      resolution/scale/multi-monitor backend + manual use).
#   2. FONT SCALING (compositor-agnostic env + fontconfig lever, NOT shell JS):
#      hart.display.fontScale = 1.25 emits GDK_DPI_SCALE = 1.25 into the login env
#      AND a fontconfig dpi edit (= 96 * 1.25 = 120) into /etc/fonts.
#   3. NEVER-FAIL kanshi: the daemon is a USER service (can never block the SYSTEM
#      boot) with a CAPPED Restart (StartLimitBurst) so a compositor without
#      wlr-output-management can't restart-storm — and the SYSTEM manager has no
#      such unit (proves it is user-scoped, not boot-critical).
#   4. DEGRADE-TO-SAFE-DEFAULT + persisted-profile dir: running the daemon's own
#      ExecStartPre seed creates an empty (no-op = compositor default) kanshi config
#      and is idempotent (a saved layout is never clobbered).
#
# Honest-hardware-limit: a VM has no real multi-output DRM / wlr-output-management,
# so this proves the SHIPPED wiring (tools, env, fontconfig, the never-fail user
# unit, the seed/degrade behaviour) — live multi-monitor arrange is a real-HW /
# Tier-2-sway check. `[VM]` — cannot run on the Windows dev box; gates in CI.
#
# #70 discipline: built from `hartModules` via the shared `mkNode` (./lib.nix) and
# imports ../modules/hart-display.nix directly so it runs whether or not flake.nix
# has registered the module yet (the held-file follow-up the Wire phase lands).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  display-management = pkgs.testers.runNixOSTest {
    name = "hart-display-management";
    # Same runtime-injected node-global false positives the other hart tests
    # document; the VM boots and the assertions run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    # "server" variant: lightest node (display management is variant-neutral). We
    # only assert units / env / files (no live graphical session), so no GUI is
    # needed. fontconfig is force-enabled so the font-scaling lever materialises in
    # /etc/fonts on this otherwise-headless node (it is already on for desktop).
    nodes.dispnode = mkNode "server" {
      imports = [ ../modules/hart-display.nix ];

      virtualisation = {
        memorySize = 1024;
        cores = 2;
      };

      hart.display.enable = true;
      hart.display.fontScale = 1.25;
      hart.display.multiMonitor = true;

      fonts.fontconfig.enable = true;
    };

    testScript = ''
      dispnode = machines[0]
      dispnode.start()
      dispnode.wait_for_unit("multi-user.target")

      with subtest("1. wlr-randr + kanshi are in the closure / PATH"):
          dispnode.succeed("command -v wlr-randr")
          dispnode.succeed("command -v kanshi")

      with subtest("2. font scaling is set via env + fontconfig (not shell JS)"):
          # GDK_DPI_SCALE = 1.25 exported into the login environment (a login shell
          # sources the full /etc/profile -> /etc/set-environment chain).
          val = dispnode.succeed("bash -l -c 'echo GDKDPI=$GDK_DPI_SCALE'")
          assert "GDKDPI=1.25" in val, "fontScale 1.25 not in GDK_DPI_SCALE:\n" + val
          # fontconfig dpi edit (96 * 1.25 = 120) materialised under /etc/fonts —
          # found via the module's own marker so we never pick another conf file.
          fc = dispnode.succeed(
              "grep -rl 'hart.display.fontScale' /etc/fonts").strip().split()[0]
          conf = dispnode.succeed("cat " + fc)
          assert "120" in conf, "fontconfig dpi (=120) not written:\n" + conf

      with subtest("3. kanshi is a NEVER-FAIL user service (never boot-critical)"):
          unit = dispnode.succeed("cat /etc/systemd/user/hart-kanshi.service")
          assert "kanshi" in unit, unit
          assert "Restart=on-failure" in unit, unit
          assert "StartLimitBurst=3" in unit, "restart storm not capped:\n" + unit
          # It is USER-scoped: the SYSTEM manager has no such unit -> it can never
          # block or fail the system boot transaction.
          dispnode.fail("systemctl cat hart-kanshi.service")

      with subtest("4. seed creates a safe-default kanshi config + is idempotent"):
          # Pull the real ExecStartPre seed out of the unit and run it under a temp
          # HOME — the same script the daemon runs at session start.
          seed = dispnode.succeed(
              "sed -n 's/^ExecStartPre=//p' /etc/systemd/user/hart-kanshi.service"
          ).strip()
          assert seed, "no ExecStartPre seed wired into hart-kanshi"
          dispnode.succeed(
              f"rm -rf /tmp/seedhome && mkdir -p /tmp/seedhome && "
              f"HOME=/tmp/seedhome XDG_CONFIG_HOME=/tmp/seedhome/.config {seed}")
          dispnode.succeed("test -e /tmp/seedhome/.config/kanshi/config")
          # The default config is a no-op (no profile body) -> compositor keeps its
          # safe default mode (every output enabled at preferred). Degrade-to-safe.
          dispnode.succeed(
              "grep -q 'managed by the display settings backend' "
              "/tmp/seedhome/.config/kanshi/config")
          # Idempotent: a user-saved layout is never clobbered by a re-seed.
          dispnode.succeed(
              "echo 'profile saved { output \"X\" enable }' > "
              "/tmp/seedhome/.config/kanshi/config")
          dispnode.succeed(
              f"HOME=/tmp/seedhome XDG_CONFIG_HOME=/tmp/seedhome/.config {seed}")
          out = dispnode.succeed("cat /tmp/seedhome/.config/kanshi/config")
          assert "profile saved" in out, "seed clobbered a saved layout:\n" + out
    '';
  };
}
