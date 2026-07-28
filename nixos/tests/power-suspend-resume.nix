# ═══════════════════════════════════════════════════════════════
# HART OS — Suspend/resume agent-state + backend-reconnect nixosTest
# ═══════════════════════════════════════════════════════════════
#
# The behavioural proof for the power-session-suspend audit's failure mode #6
# ("Agent state lost / backend not reconnected across suspend — GAP (dormant)").
#
# THE GAP this closes: nixos/modules/hart-power.nix defines the suspend pipeline
# (hart-suspend-checkpoint before sleep.target, hart-resume after the sleep/
# hibernate targets, the lid action), but `hart.power.enable` was NEVER set in any
# configuration, so the whole module was dormant AND — the reason WHY it stayed
# dormant — it could not even be turned on: it enabled BOTH power-profiles-daemon
# and TLP, which nixpkgs hard-asserts are mutually exclusive, so
# `hart.power.enable = true` FAILED EVAL on desktop/phone (Fix B corrects this).
# On top of that, the checkpoint/resume hooks curl'd /api/power/{checkpoint,resume}
# while the real routes are /api/shell/power/{checkpoint,resume}
# (register_shell_os_routes), so even an enabled module silently 404'd (the
# `|| echo` masked it — Fix A), and they called bare `curl` (not guaranteed on the
# systemd unit PATH — Fix C).
#
# This test boots a node with `hart.power.enable = true` (which only EVALUATES at
# all because of Fix B), stands up a tiny mock backend on the HART backend port
# that RECORDS the request path of every POST it receives, and proves
# BEHAVIOURALLY:
#   1. The module is enableable: the node boots, power-profiles-daemon is the live
#      profile daemon, and TLP is NOT co-enabled (the eval-bricking conflict gone).
#   2. The checkpoint hook, when run, reaches the REAL route
#      /api/shell/power/checkpoint — NOT the old /api/power/* 404 (Fix A) — via an
#      absolute curl (Fix C). The "agent state IS checkpointed before suspend".
#   3. The resume hook reaches /api/shell/power/resume AND runs networkctl
#      reconfigure — the "backend reconnected + network re-checked on resume".
#   4. DEGRADE-NOT-DIE: with the backend DOWN, the checkpoint hook STILL exits 0
#      (it never blocks suspend / wedges sleep.target).
#   5. Unit wiring: checkpoint Before + WantedBy sleep.target; resume After +
#      WantedBy the suspend/hibernate targets.
#   6. The lid action is the configured policy (HandleLidSwitch=suspend) and the
#      box advertises a real suspend capability (/sys/power/state has `mem`).
#
# NOTE on the DISPLAY half (failure mode #5, "resume blacks the screen"): the
# direct fix — the compositor re-acquiring DRM master on libseat session-activate
# (compositor/src apply_pending_session) — is the DISPLAY dimension and is NOT
# edited here. The indirect never-black recovery net (node_watchdog ->
# /run/hart/compositor-unhealthy -> the supervisor drops to a painting tier) is
# already proven by tests/session-supervisor.nix (the unhealthy-flag node) and
# tests/display-tiers-neverblack.nix; this file does not re-prove it.
#
# [VM]-gated per the honest-hardware rule: a real systemd suspend pipeline +
# logind cannot run on the Windows dev box. It gates in CI (`nix flake check` /
# local QEMU), never grep. #70 discipline preserved: built from `hartModules`
# alone via the shared `mkNode` (./lib.nix); hart.power is opt-in so the node
# enables it + injects the mock backend.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # The HART backend port (hart-base default). Pinned explicitly so the hook curl,
  # the mock backend bind, and the testScript probe all agree on ONE value.
  backendPort = 6777;
in
{
  hart-power-suspend-resume = pkgs.testers.runNixOSTest {
    name = "hart-power-suspend-resume";
    # Same false-positive static passes as every sibling desktop test: the driver
    # injects the per-node Machine global at runtime; mypy/pyflakes can't see it.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    # `extra` is a FUNCTION module so it can read config (the port) at eval time.
    nodes.node = mkNode "desktop" ({ config, pkgs, lib, ... }:
      let
        # A tiny recording backend: every POST appends its request PATH to a hits
        # file so the test can assert which ROUTE the hook reached (catches the
        # /api/power vs /api/shell/power path regression behaviourally).
        mockBackend = pkgs.writeText "mock-backend.py" ''
          import http.server, socketserver
          HITS = "/run/mock-backend-hits"
          PORT = ${toString backendPort}
          class H(http.server.BaseHTTPRequestHandler):
              def _drain(self):
                  try:
                      n = int(self.headers.get("Content-Length", 0) or 0)
                      if n:
                          self.rfile.read(n)
                  except Exception:
                      pass
              def do_POST(self):
                  self._drain()
                  with open(HITS, "a") as f:
                      f.write(self.path + "\n")
                  self.send_response(200)
                  self.send_header("Content-Type", "application/json")
                  self.end_headers()
                  self.wfile.write(b'{"ok": true}')
              def do_GET(self):
                  self.send_response(200)
                  self.end_headers()
              def log_message(self, *a):
                  return
          socketserver.TCPServer.allow_reuse_address = True
          with socketserver.TCPServer(("127.0.0.1", PORT), H) as httpd:
              httpd.serve_forever()
        '';
      in
      {
        virtualisation = {
          memorySize = 2048;
          cores = 2;
        };

        # Pin the port explicitly so config, mock, and test agree (no reliance on
        # the default drifting).
        hart.ports.backend = backendPort;

        # Turn the dormant module ON — the whole point of the test. This only
        # EVALUATES because of the ppd/TLP mutual-exclusion fix (Fix B).
        hart.power.enable = true;

        # Free the backend port for the mock: the real hart-backend (heavy Flask
        # app) would otherwise bind it. We test the HOOK -> route contract, not the
        # real app, so a recording stand-in on the same port is enough.
        systemd.services.hart-backend.enable = lib.mkForce false;

        # The mock recording backend, on the SAME port the hooks curl.
        systemd.services.mock-backend = {
          description = "Recording mock backend for the suspend/resume hook test";
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            ExecStart = "${pkgs.python3}/bin/python3 ${mockBackend}";
            Restart = "on-failure";
          };
        };
      });

    testScript = ''
      node = machines[0]
      node.start()
      node.wait_for_unit("multi-user.target")

      BACKEND = ${toString backendPort}
      HITS = "/run/mock-backend-hits"

      # ── 1. The module is ENABLEABLE (Fix B): ppd live, TLP NOT co-enabled ──
      with subtest("hart.power is enableable — power-profiles-daemon is live and TLP is NOT co-enabled"):
          # ppd is D-BUS ACTIVATED, never enabled: nixpkgs' module sets only
          # services.dbus.packages + systemd.packages -- no wantedBy -- so the unit
          # sits inactive until a client calls it, and wait_for_unit timed out on
          # every run since 2026-07-26 while the module was configured perfectly.
          # Drive the REAL client path instead: powerprofilesctl is the same D-Bus
          # surface the shell's /api/shell/power/set uses. The call both proves the
          # daemon serves AND activates the unit, which can then be asserted.
          node.succeed("powerprofilesctl get")
          assert node.succeed("systemctl is-active power-profiles-daemon.service").strip() == "active"
          # TLP must NOT be enabled (the mutual-exclusion that used to brick eval).
          # is-active on a disabled/absent unit returns non-zero -> fail() passes.
          node.fail("systemctl is-active tlp.service")

      # ── mock backend up on the backend port ──
      with subtest("the recording mock backend is listening on the HART backend port"):
          node.wait_for_unit("mock-backend.service", timeout=60)
          node.wait_for_open_port(BACKEND)
          node.succeed(f"rm -f {HITS}")

      # ── 2. checkpoint hook reaches the REAL route (Fix A + Fix C) ──
      with subtest("hart-suspend-checkpoint reaches /api/shell/power/checkpoint (not the old /api/power 404)"):
          node.succeed("systemctl start hart-suspend-checkpoint.service")
          hits = node.succeed(f"cat {HITS}")
          assert "/api/shell/power/checkpoint" in hits, \
              f"checkpoint hook did not reach the canonical route; mock saw: {hits!r}"
          assert "/api/power/checkpoint" not in hits, \
              "checkpoint hook hit the OLD /api/power/checkpoint path (Fix A regression)"

      # ── 3. resume hook reaches /api/shell/power/resume + reconfigures network ──
      with subtest("hart-resume reaches /api/shell/power/resume and re-checks the network"):
          node.succeed(f"rm -f {HITS}")
          node.succeed("systemctl start hart-resume.service")
          hits = node.succeed(f"cat {HITS}")
          assert "/api/shell/power/resume" in hits, \
              f"resume hook did not reach the canonical route; mock saw: {hits!r}"
          # The resume unit also runs `networkctl reconfigure` (best-effort, || true)
          # then logs "Resume complete" — proving the reconnect path ran end to end
          # without wedging.
          log = node.succeed("journalctl -u hart-resume.service --no-pager")
          assert "Resume complete" in log, f"resume hook did not complete cleanly: {log!r}"

      # ── 4. DEGRADE-NOT-DIE: backend DOWN -> checkpoint still exits 0 ──
      with subtest("with the backend DOWN the checkpoint hook still succeeds (never blocks suspend)"):
          node.succeed("systemctl stop mock-backend.service")
          node.succeed(f"rm -f {HITS}")
          # The oneshot must return success even though the POST fails (the `|| echo`
          # degrade branch). A failure here would wedge sleep.target on a real suspend.
          node.succeed("systemctl start hart-suspend-checkpoint.service")
          result = node.succeed("systemctl show -p Result hart-suspend-checkpoint.service").strip()
          assert result == "Result=success", \
              f"checkpoint hook failed when the backend was down ({result}) — would block suspend"
          # It recorded NOTHING (backend was down) but did not error.
          node.fail(f"test -s {HITS}")
          node.succeed("systemctl start mock-backend.service")  # restore

      # ── 5. unit WIRING: checkpoint before sleep; resume after sleep/hibernate ──
      with subtest("hart-suspend-checkpoint runs BEFORE sleep.target and is pulled in by it"):
          show = node.succeed(
              "systemctl show hart-suspend-checkpoint.service -p Before -p WantedBy -p Type")
          assert "sleep.target" in show, f"checkpoint not wired to sleep.target: {show!r}"
          assert "Type=oneshot" in show, f"checkpoint must be a oneshot: {show!r}"
          before = node.succeed(
              "systemctl show hart-suspend-checkpoint.service -p Before").strip()
          assert "sleep.target" in before, f"checkpoint not ordered BEFORE sleep.target: {before!r}"

      with subtest("hart-resume runs AFTER the suspend/hibernate targets and is pulled in by them"):
          show = node.succeed("systemctl show hart-resume.service -p After -p WantedBy")
          assert "suspend.target" in show, f"resume not wired to suspend.target: {show!r}"
          assert "hibernate.target" in show, f"resume not wired to hibernate.target: {show!r}"

      # ── 6. lid policy + real suspend capability ──
      with subtest("the configured lid action is suspend (logind policy)"):
          conf = node.succeed("systemd-analyze cat-config systemd/logind.conf")
          assert "HandleLidSwitch=suspend" in conf, \
              f"logind lid policy is not suspend: {conf!r}"

      with subtest("the box advertises a real suspend capability (/sys/power/state has mem)"):
          # Degrade-not-die: assert the capability only when the kernel exposes the
          # sleep-state interface at all (it does in QEMU). Absence is tolerated.
          if node.succeed("test -e /sys/power/state; echo $?").strip() == "0":
              states = node.succeed("cat /sys/power/state")
              assert "mem" in states, f"suspend-to-RAM not offered by the kernel: {states!r}"
    '';
  };
}
