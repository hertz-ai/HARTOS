# ═══════════════════════════════════════════════════════════════
# HART OS — Native notification daemon (mako) + privacy gate nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves hart-notify.nix (#113) ships a REAL native `org.freedesktop.Notifications`
# daemon for the wlroots session AND that the AI's emitter honours the cross-process
# screen kill-switch — not just a flag, the actual fail-closed gate.
#
# What it asserts:
#   1. The daemon (mako + makoctl) and BOTH clients (the foreign-app `notify-send`,
#      the AI's gated `hart-notify-send`) are on PATH — any producer can post.
#   2. The notification daemon is a graphical-session USER service (never
#      boot-critical) that starts mako with the pinned store `--config`, and that
#      config carries the glass styling, the configured anchor/timeout, and the
#      Do-Not-Disturb + privacy mako modes.
#   3. REGRESSION / privacy core: with 'screen' CUT, `hart-notify-send` SUPPRESSES
#      the native toast fail-closed (exit 77, paints nothing) — the same
#      cross-process gate (core.ai_sensing.query_authority('screen')) the screencast
#      portal uses.
#   4. Happy path: with 'screen' ON the gate ALLOWS (the cut is the only blocker —
#      no false lockout; exit code is NOT the 77 gate-refusal).
#   5. Boundary (offline/error): with the authority server DOWN the emitter
#      fail-CLOSES (exit 77) — never paints on doubt, never crashes/hangs.
#
# Honest-hardware-limit: mako can only PAINT against a live wlroots compositor; a
# headless VM has none, so this exercises the daemon's wiring + config + the
# privacy gate (the parts that ship and enforce today), not pixels. `[VM]` — cannot
# run on the Windows dev box; gates in CI (`nix flake check`) / local QEMU.
#
# #70 discipline: built from `hartModules` alone via the shared `mkNode` (./lib.nix),
# and self-contained — it imports ../modules/hart-notify.nix directly so it runs
# whether or not flake.nix has registered the module yet (the held-file follow-up).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-notify = pkgs.testers.runNixOSTest {
    name = "hart-notify";
    # Same runtime-injected-node-global false positives the floor-lock / portal
    # tests document (the driver injects `node`/`machines` at runtime); skip the
    # static passes — the VM boots and the assertions run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.notifynode = mkNode "desktop" {
      # Self-contained: import the module directly so the test does not depend on
      # flake.nix having registered it yet (held-file follow-up). Importing the same
      # path twice (here + once hartModules carries it) is idempotent in NixOS.
      imports = [ ../modules/hart-notify.nix ];

      virtualisation = {
        memorySize = 4096;
        cores = 2;
      };

      # The AI emitter consults the cross-process screen kill-switch hosted by the
      # LiquidUI :6800 process (core.ai_sensing authority + POST /api/shell/ai-
      # sensing). The socket is only BOUND at /run/hart/ai-sensing.sock when
      # hart.portal pins HART_AI_SENSING_SOCK on the hart-liquid-ui unit AND grants
      # it ReadWritePaths=/run/hart (hart-portal.nix) — so enable BOTH, exactly the
      # proven portal-screencast.nix node, to drive the gate on/off.
      hart.liquidUI = { enable = true; renderer = "webkit"; voiceEnabled = pkgs.lib.mkForce false; };
      hart.portal.enable = true;

      # The daemon under test (default-ON on desktop; set explicit for clarity).
      hart.notifications.enable = true;
      hart.notifications.position = "top-right";
      hart.notifications.defaultTimeout = 6000;
    };

    testScript = ''
      import re

      # The driver keys the single machine global by HOSTNAME — mkNode forces it to
      # the variant ("desktop"), not the nodes.notifynode key. Bind from the
      # machines list (single-node test => element 0), the floor-lock.nix lesson.
      notifynode = machines[0]
      notifynode.start()
      notifynode.wait_for_unit("multi-user.target")

      # ── 1. The daemon + both clients are on PATH ──
      with subtest("mako daemon + makoctl + both notify clients are on PATH"):
          notifynode.succeed("command -v mako")
          notifynode.succeed("command -v makoctl")
          notifynode.succeed("command -v notify-send")        # foreign apps (ungated)
          notifynode.succeed("command -v hart-notify-send")   # the AI emitter (gated)

      # ── 2. The graphical-session USER service + the pinned glass config ──
      with subtest("native notification daemon is a graphical-session user service with the glass config"):
          unit = notifynode.succeed("cat /etc/systemd/user/hart-notify.service")
          assert "graphical-session.target" in unit, \
              "hart-notify must bind the wayland (graphical-session) session, got:\n" + unit
          assert "/bin/mako --config " in unit, \
              "hart-notify must start mako with a pinned store --config, got:\n" + unit
          # Upstream-canonical mako readiness: Type=dbus + the Notifications bus
          # name, so the unit is "started" only once mako owns the name (IPC up) and
          # the doNotDisturb ExecStartPost cannot race a Type=simple immediate fork.
          # Regression guard: a revert to Type=simple would silently re-open the race.
          assert "BusName=org.freedesktop.Notifications" in unit, \
              "hart-notify must own org.freedesktop.Notifications (Type=dbus readiness), got:\n" + unit
          assert re.search(r"^Type=dbus$", unit, re.MULTILINE), \
              "hart-notify must use Type=dbus so makoctl ordering is not a fork race, got:\n" + unit
          m = re.search(r"--config (\S+)", unit)
          assert m, "no --config path in the unit ExecStart:\n" + unit
          conf = notifynode.succeed("cat " + m.group(1))
          # The styling + configured knobs + privacy/DnD modes the daemon loads.
          assert "anchor=top-right" in conf, "configured position not wired into mako config"
          assert "default-timeout=6000" in conf, "configured timeout not wired into mako config"
          assert "[mode=do-not-disturb]" in conf and "invisible=1" in conf, \
              "Do-Not-Disturb mode missing from mako config"
          assert "[mode=privacy]" in conf, "privacy mode missing from mako config"

      # ── 3. LiquidUI hosts the AI-senses authority the emitter consults ──
      with subtest("LiquidUI shell binds the pinned /run/hart/ai-sensing.sock authority"):
          notifynode.wait_for_unit("hart-liquid-ui.service", timeout=180)
          notifynode.wait_for_open_port(6800, timeout=60)
          notifynode.wait_until_succeeds("test -S /run/hart/ai-sensing.sock", timeout=60)

      # Drive the human kill-switch over the ONLY writer — POST /api/shell/ai-sensing
      # on :6800 (the canonical _state holder). action=off/on => cut/restore senses.
      def set_screen(disabled):
          action = "off" if disabled else "on"
          notifynode.succeed(
              "curl -fs -X POST http://localhost:6800/api/shell/ai-sensing "
              "-H 'Content-Type: application/json' "
              f"-d '{{\"action\": \"{action}\"}}'")

      # ── 3 (regression / privacy core). Screen CUT ⇒ emitter SUPPRESSES (77) ──
      with subtest("screen cut ⇒ hart-notify-send suppresses the native AI toast fail-closed"):
          set_screen(True)
          # 2>&1: the suppression message goes to STDERR by design, and the
          # test driver's execute() captures STDOUT only — without the
          # redirect the rc==77 assert passed while this one read an empty
          # string against a correctly-suppressing gate (run 30485906966).
          rc, out = notifynode.execute("hart-notify-send 'HART' 'a private message body' 2>&1")
          assert rc == 77, \
              f"AI emitter must refuse with 77 when the human cut 'screen', got rc={rc}: {out}"
          assert "SUPPRESSED" in out, \
              f"the suppression must be observable on stderr, got: {out}"

      # ── 4 (happy). Screen ON ⇒ the gate ALLOWS (cut is the only blocker) ──
      with subtest("screen on ⇒ the gate allows (no false lockout; not the 77 refusal)"):
          set_screen(False)
          # No live compositor ⇒ the subsequent notify-send has no daemon and exits
          # non-zero, but it must NOT be the 77 gate-refusal: the gate let it through.
          # `timeout` guards against a D-Bus stall (it would exit 124, still != 77).
          rc, _ = notifynode.execute("timeout 10 hart-notify-send 'HART' 'hello'")
          assert rc != 77, \
              f"gate must ALLOW when 'screen' is on (77 == gate-refused), got rc={rc}"

      # ── 5 (boundary: offline/error). Authority DOWN ⇒ fail-closed (77) ──
      with subtest("authority unreachable ⇒ emitter fail-closes (never paints on doubt)"):
          notifynode.succeed("systemctl stop hart-liquid-ui.service")
          # The authority server is gone; query_authority can no longer reach it and
          # fail-closes, so the emitter refuses regardless of the last sense state.
          rc, out = notifynode.execute("hart-notify-send 'HART' 'x'")
          assert rc == 77, \
              f"emitter must fail-closed with 77 when the authority is down, got rc={rc}: {out}"
    '';
  };
}
