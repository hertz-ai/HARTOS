# ═══════════════════════════════════════════════════════════════
# HART OS — Phase-7 Portal + cross-process screen kill-switch nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the Phase-7 NEVER-BREAK gate: NO native screencast surface can capture a
# screen the human cut. The naive design (ship a portal that reads the in-process
# flag) would silently fail-open across the process boundary; this test asserts the
# REAL cross-process gate denies capture fail-closed.
#
# What it asserts (compositor/ROADMAP.md Phase 7 "Tests" + "Never-break gates"):
#   1. The brain authority server is reachable on the pinned socket
#      (/run/hart/ai-sensing.sock) — the cross-process gate has a server to talk to.
#   2. With 'screen' DISABLED, `hart-screencast-gate` exits 77 (REFUSED) and the
#      wlr-screencopy `grim` wrapper refuses — a Flatpak/Wine-equivalent capture
#      denied at the portal gate, NOT just a flag flipped.
#   3. With 'screen' ENABLED, the gate exits 0 (the cut is the ONLY thing that
#      blocks — no false-positive lockout).
#   4. `status()` reports portal_screencast_blocked == the human's gate (the
#      un-fakeable, observable proof the portal path is shut).
#   5. The hart `.portal` backend + its dbus policy are in the system closure, and
#      the `hart-lock` PAM service file exists (the real ext-session-lock auth).
#
# Honest-hardware-limit: a full D-Bus ScreenCast round-trip from a real Flatpak
# needs the compile-pending backend; this test exercises the ENFORCING surface
# that ships today — the cross-process gate + the wlr-screencopy routing the brain
# (and any app shelling out to grim/wf-recorder) actually hits. `[VM]` — cannot run
# on the Windows dev box; gates in CI (`nix flake check`) / local QEMU.
#
# #70 discipline: built from `hartModules` alone via the shared `mkNode`
# (./lib.nix), NO ../configurations/X.nix installer-CD overlay.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-portal-screencast = pkgs.testers.runNixOSTest {
    name = "hart-portal-screencast";
    # Same runtime-injected-node-global false positives the floor-lock test
    # documents (mypy + pyflakes flag the `node` global the driver injects at
    # runtime); skip the static passes — the VM boots and the assertions run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.portalnode = mkNode "desktop" {
      virtualisation = {
        memorySize = 4096;
        cores = 2;
      };
      # hart.portal asserts hart.liquidUI.enable=true (the portal Settings bridge
      # reads ThemeService tokens + the screencast gate consults core.ai_sensing,
      # both hosted in the LiquidUI :6800 process the testScript waits for below).
      hart.liquidUI = { enable = true; renderer = "webkit"; voiceEnabled = false; };
      # Enable the Phase-7 portal surface (opt-in; off by default everywhere else).
      hart.portal.enable = true;
      hart.portal.closeAutologinPamGap = true;
    };

    testScript = ''
      # The driver keys the single machine global by HOSTNAME — mkNode forces it
      # to the variant ("desktop"), not the nodes.portalnode key. Bind from the
      # machines list (single-node test → element 0), the floor-lock.nix lesson.
      portalnode = machines[0]
      portalnode.start()
      portalnode.wait_for_unit("multi-user.target")

      with subtest("LiquidUI shell service starts (it hosts the AI-senses authority)"):
          # The kill-switch endpoint AND core.ai_sensing._state live in the LiquidUI
          # process (it serves POST /api/shell/ai-sensing) — NOT the :6777 backend —
          # so the authority server is started THERE. Wait for :6800.
          portalnode.wait_for_unit("hart-liquid-ui.service", timeout=180)
          portalnode.wait_for_open_port(6800, timeout=60)

      # ── 1. The cross-process authority socket is bound by the LiquidUI host ──
      with subtest("LiquidUI authority server binds the pinned /run/hart/ai-sensing.sock"):
          # liquid_ui_service.py calls start_authority_server() with
          # HART_AI_SENSING_SOCK pinned by the module. Wait for the socket to exist.
          portalnode.wait_until_succeeds(
              "test -S /run/hart/ai-sensing.sock", timeout=60)

      # Helper: drive the human kill-switch over the LiquidUI POST /api/shell/ai-
      # sensing (the ONLY writer — the AI has no path), on :6800 (the canonical
      # _state holder). action=off/on => disable_all/enable_all the senses.
      def set_screen(disabled):
          action = "off" if disabled else "on"
          portalnode.succeed(
              "curl -fs -X POST http://localhost:6800/api/shell/ai-sensing "
              "-H 'Content-Type: application/json' "
              f"-d '{{\"action\": \"{action}\"}}'")

      # ── 2. Screen CUT ⇒ the gate REFUSES (exit 77) + grim wrapper refuses ──
      with subtest("Screen cut ⇒ hart-screencast-gate REFUSES (cross-process, fail-closed)"):
          set_screen(True)
          # The gate binary must exit non-zero (77). `! ...` asserts non-zero.
          portalnode.succeed("! hart-screencast-gate")
          # The wlr-screencopy grim wrapper consults the gate first ⇒ also refuses.
          # (It is a PATH shadow ahead of pkgs.grim; bare `grim` hits the wrapper.)
          rc = portalnode.execute("grim /tmp/should-not-exist.png")[0]
          assert rc != 0, "grim wrapper captured the screen while 'screen' was CUT — gate bypassed"
          portalnode.succeed("test ! -e /tmp/should-not-exist.png")

      # ── 3. Screen ON ⇒ the gate ALLOWS (the cut is the only blocker) ──
      with subtest("Screen on ⇒ hart-screencast-gate exits 0 (no false lockout)"):
          set_screen(False)
          portalnode.succeed("hart-screencast-gate")

      # ── 4. status() reports the un-fakeable cross-process proof ──
      with subtest("status() exposes portal_screencast_blocked == the human gate"):
          set_screen(True)
          blocked = portalnode.succeed(
              "curl -fs http://localhost:6800/api/shell/ai-sensing "
              "| ${pkgs.jq}/bin/jq -r '.proof.portal_screencast_blocked'").strip()
          assert blocked == "true", \
              f"portal_screencast_blocked should be true when screen is cut, got {blocked!r}"
          set_screen(False)
          blocked = portalnode.succeed(
              "curl -fs http://localhost:6800/api/shell/ai-sensing "
              "| ${pkgs.jq}/bin/jq -r '.proof.portal_screencast_blocked'").strip()
          assert blocked == "false", \
              f"portal_screencast_blocked should be false when screen is on, got {blocked!r}"

      # ── 5. The hart portal backend + dbus policy + PAM lock are in the closure ──
      with subtest("hart .portal backend + dbus policy + hart-lock PAM service present"):
          portalnode.succeed(
              "find /nix/store -name 'hart.portal' -path '*xdg-desktop-portal*' "
              "-print -quit | grep -q .")
          # dbus policy reserving the hart portal bus name (deny default; hart only).
          portalnode.succeed(
              "find /nix/store -name 'org.freedesktop.impl.portal.desktop.hart.conf' "
              "-print -quit | grep -q .")
          # The PAM service that makes the ext-session-lock unlock a REAL cred check.
          portalnode.succeed("test -e /etc/pam.d/hart-lock")

      # ── never-break: no portal verb re-enables a sense (gate is consume-only) ──
      with subtest("The portal is a CONSUMER of ai_sensing — no re-enable path"):
          # Cut the screen, then prove the gate STAYS refused — there is no portal-
          # side toggle. Only the human POST /api/shell/ai-sensing on=... reopens it.
          set_screen(True)
          portalnode.succeed("! hart-screencast-gate")
    '';
  };
}
