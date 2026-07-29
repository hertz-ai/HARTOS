# ═══════════════════════════════════════════════════════════════
# HART OS — Central-Controlled Autonomous OTA nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the NODE-side autonomous OTA wiring (hart-ota.nix): an installed
# node auto-polls CENTRAL (not github) for the approved {flake_ref, commit}
# of its channel, stages it through the EXISTING UpgradeOrchestrator pipeline,
# and — with autoApply=true — applies via the EXISTING `nixos-rebuild switch
# --flake` path with the EXISTING `|| nixos-rebuild switch --rollback`
# auto-rollback, ALL with zero manual approval.
#
# What it asserts (the task's four required behaviours):
#   1. The check service HITS hart.ota.centralEndpoint and parses the approved
#      revision for its channel (central is the source of truth, not github).
#   2. A new commit STAGES — pending_update.json carries the central commit as
#      `revision` AND the central flake_ref as `switch_flake`, and the
#      orchestrator pipeline left IDLE→BUILDING (start_upgrade fired).
#   3. autoApply APPLIES — when the pipeline reaches `completed`, the check
#      service runs `nixos-rebuild switch --flake <CENTRAL flake>#hart-<variant>`
#      with NO manual step (timer/oneshot-driven).
#   4. A failed apply ROLLS BACK — when the switch exits non-zero, the service
#      runs `nixos-rebuild switch --rollback`.
#
# REUSE discipline: this drives the SAME hart-ota-check service + the SAME
# UpgradeOrchestrator (start_upgrade/get_status) the production module uses —
# no second updater, no forked pipeline. Only the privileged `nixos-rebuild`
# and `sudo` are shadowed by recording stubs (a real switch would rebuild the
# whole VM and is untestable); the central server is a localhost mock standing
# in for etime's /api/ota/latest. The master-key SIGN gate and the canary
# stage are NOT bypassed by central — central only chooses WHICH commit; the
# orchestrator's local SIGN/CANARY gates still run before DEPLOY (asserted
# indirectly: start_upgrade enters BUILDING, the front of the same gated
# pipeline, never jumps straight to the switch).
#
# `[VM]` — boots a real QEMU node; gates in CI (`nix flake check`) / local
# QEMU, CANNOT run on the Windows dev box (mirrors floor-lock.nix: a pure
# systemd-script-wiring concern whose behavioural assertion IS the nixosTest).
# The REUSED orchestrator contract this leans on (start_upgrade enters BUILDING,
# refuses while a pipeline is active, rollback works) is unit-tested on the dev
# box in tests/unit/test_upgrade_pipeline.py.
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix), NO ../configurations/X.nix installer-CD overlay.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

  # The SAME hart app package the node builds (mkNode does exactly this from
  # specialArgs.hartSrc). Reused here only so the push subtest can drive the
  # node-side listener (ota_push_listener) with the node's own interpreter —
  # no second python, no copy of the app.
  hartApp = pkgs.callPackage ../packages/hart-app.nix {
    hartSrc = specialArgs.hartSrc;
  };

  # The revision + flake CENTRAL approves for the node's channel. The node must
  # switch to EXACTLY this flake_ref (central can pin an exact commit), not the
  # configured github channel HEAD.
  approvedCommit = "deadbeefcafe0000deadbeefcafe0000deadbeef";
  approvedFlake = "github:hertz-ai/HARTOS/${approvedCommit}";
  centralPort = 9099;

  # Mock CENTRAL /api/ota/latest. Returns the approved {flake_ref, commit,
  # channel} for the requested channel (the queen-bee authority's decision).
  # Stands in for etime.hertzai.com:6777/api/ota/latest. Bound to localhost on
  # the node so the check service's curl reaches it without external network.
  # Written via writeText + exec (no heredoc) so Nix indentation-stripping can
  # never corrupt the Python body — the floor-lock/supervisor no-heredoc rule.
  mockCentralPy = pkgs.writeText "mock-central-ota.py" ''
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    BODY = {
        "flake_ref": "${approvedFlake}",
        "commit": "${approvedCommit}",
        "channel": "stable",
    }

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            # Echo the requested channel so the test proves the node passes it.
            payload = dict(BODY)
            if "channel" in q:
                payload["channel"] = q["channel"][0]
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    HTTPServer(("127.0.0.1", ${toString centralPort}), H).serve_forever()
  '';
  mockCentral = pkgs.writeShellScript "mock-central-ota" ''
    exec ${pkgs.python3}/bin/python3 ${mockCentralPy}
  '';

  # Recording stubs for the privileged switch path. `.path` prepends these so
  # they win over the system `sudo`/`nixos-rebuild`. Each records its full argv
  # to ota/rebuild.log; `nixos-rebuild switch --flake …` exits non-zero IFF the
  # FAIL_SWITCH sentinel exists (drives the rollback subtest) — `--rollback`
  # always succeeds so the recovery path completes.
  rebuildStubs = pkgs.writeShellScriptBin "nixos-rebuild" ''
    echo "nixos-rebuild $*" >> /var/lib/hart/ota/rebuild.log
    if [[ "$*" == *"switch --flake"* ]]; then
      if [[ -e /var/lib/hart/ota/FAIL_SWITCH ]]; then
        echo "[stub] simulated switch FAILURE" >&2
        exit 1
      fi
    fi
    exit 0
  '';

  # `sudo` stub: record + exec the rest (so `sudo nixos-rebuild …` hits the
  # nixos-rebuild stub above). Keeps the module's exact `sudo nixos-rebuild`
  # call shape — we shadow privilege, not the command structure.
  sudoStub = pkgs.writeShellScriptBin "sudo" ''
    # Skip any leading sudo flags, then exec the target command.
    while [[ "''${1:-}" == -* ]]; do shift; done
    exec "$@"
  '';

  # `nixos-version` stub so the check service's `CURRENT=$(nixos-version)` is
  # deterministic under `set -euo pipefail` on the minimal node.
  nixosVersionStub = pkgs.writeShellScriptBin "nixos-version" ''
    echo "hart-os-test"
  '';

  # `systemctl` recording stub for the PUSH path. ota_push_listener kicks the
  # apply with `systemctl start hart-ota-check.service`; this records that argv
  # to ota/push-kick.log so the push subtest can prove a verified central push
  # converges on the SAME apply unit the boot poll uses. Non-`start` invocations
  # (none expected from the listener) just succeed.
  systemctlStub = pkgs.writeShellScriptBin "systemctl" ''
    echo "systemctl $*" >> /var/lib/hart/ota/push-kick.log
    exit 0
  '';

  # Test driver for the PUSH path. Imports the REAL node-side listener
  # (ota_push_listener.handle_push) and feeds it one simulated central push —
  # a `firmware_update` FleetCommand exactly as it arrives on the existing
  # 'fleet.command' fabric. We monkeypatch ONLY the signature-authority check
  # (verify_command_signature) so the test deterministically exercises both a
  # verified and a forged push without standing up the full key chain (that
  # check is unit-tested in tests/unit/test_ota_push_listener.py). With the
  # recording `systemctl` stub on PATH, a verified push must record a `start
  # hart-ota-check.service` kick; a forged push must record NOTHING. This proves
  # requirement (3): a central push triggers the SAME apply, over the existing
  # transport, gated by the same authority — no new updater, no new transport.
  #
  # $1 = "verified" | "forged"  (toggles the patched verifier's return)
  pushDriverPy = pkgs.writeText "ota-push-driver.py" ''
    import os, sys
    sys.path.insert(0, "${hartApp}")

    mode = sys.argv[1] if len(sys.argv) > 1 else "verified"

    # Patch the SAME authority gate the fleet bus uses. handle_push calls
    # FleetCommandService.verify_command_signature; force it per-mode.
    from integrations.social import fleet_command as fc
    fc.FleetCommandService.verify_command_signature = staticmethod(
        lambda cmd: (mode == "verified"))

    from integrations.agent_engine import ota_push_listener as L

    # A simulated central push as published on 'fleet.command'. Untargeted
    # (no target_node_id) = broadcast to this node; carries the central flake +
    # commit the same way POST /api/ota/publish (api_fleet_update) fans out via
    # FleetCommandService.push_broadcast('firmware_update', ...).
    push = {
        "cmd_type": "firmware_update",
        "issued_by": "central00central0",
        "signature": "sig",
        "params": {
            "update_url": "${approvedFlake}",
            "release_hash": "${approvedCommit}",
            "channel": "stable",
        },
    }
    kicked = L.handle_push(push, self_node_id="thisnode00000000")
    print("PUSH_HANDLED kicked=%s mode=%s" % (kicked, mode))
  '';
  pushDriver = pkgs.writeShellScriptBin "ota-push-driver" ''
    export HART_OTA_CHECK_UNIT="hart-ota-check.service"
    exec ${hartApp.python}/bin/python ${pushDriverPy} "$@"
  '';

in
{
  hart-ota-central = pkgs.testers.runNixOSTest {
    name = "hart-ota-central";
    # Same runtime-injected-machine-global false positives the floor-lock /
    # session-supervisor tests skip (the node is named by its variant hostname,
    # not the nodes.<key>; bound from machines[0] at runtime).
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.node = mkNode "server" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };

      # ── The autonomous-central OTA configuration under test ──
      hart.ota = {
        enable = true;
        channel = "stable";
        autoApply = true;   # hands-off: the `completed` branch switches with no approval
        centralEndpoint = "http://127.0.0.1:${toString centralPort}/api/ota/latest";
        # flakeRef stays the github default; central's approved flake_ref must
        # SUPERSEDE it as the switch target (asserted below).
      };

      # Shadow the privileged switch + version binaries on the check service's
      # PATH only (`.path` prepends). The production unit calls bare
      # `sudo`/`nixos-rebuild`/`nixos-version`; these recording stubs intercept
      # them so the test observes the switch/rollback argv without a real
      # system rebuild.
      systemd.services.hart-ota-check.path = [
        sudoStub
        rebuildStubs
        nixosVersionStub
      ];

      # The push driver (drives the real ota_push_listener) is available on the
      # node; the push subtest runs it with `systemctlStub` prepended on PATH so
      # the listener's `systemctl start hart-ota-check` is recorded, not real.
      environment.systemPackages = [ pushDriver systemctlStub ];

      # The localhost mock CENTRAL endpoint (stands in for etime's
      # /api/ota/latest). Up before the check service polls it.
      systemd.services.mock-central-ota = {
        description = "Mock CENTRAL OTA pointer endpoint (test only)";
        wantedBy = [ "multi-user.target" ];
        before = [ "hart-ota-check.service" ];
        serviceConfig = {
          ExecStart = mockCentral;
          Restart = "on-failure";
        };
      };
    };

    testScript = ''
      import json

      # Driver keys the single machine global by HOSTNAME (mkNode forces it to
      # the variant "server"), not nodes.node — bind from machines[0].
      node = machines[0]
      node.start()
      node.wait_for_unit("multi-user.target")

      with subtest("Backend + mock CENTRAL are up"):
          node.wait_for_unit("hart-backend.service", timeout=120)
          node.wait_for_unit("mock-central-ota.service", timeout=60)
          node.wait_for_open_port(${toString centralPort}, timeout=30)

      OTA = "/var/lib/hart/ota"
      STATE = "/var/lib/hart/agent_data/upgrade_state.json"
      PENDING = OTA + "/pending_update.json"
      REBUILD_LOG = OTA + "/rebuild.log"

      # The orchestrator must start IDLE for the staging run. tmpfiles already
      # created OTA + agent_data; ensure no stale state from a prior boot.
      node.succeed("rm -f " + STATE + " " + PENDING + " " + REBUILD_LOG)

      # ── 1+2. Check service HITS central, parses the approved rev, STAGES it ──
      with subtest("check service polls CENTRAL and parses the approved {flake_ref, commit}"):
          out = node.succeed("systemctl start hart-ota-check.service; "
                             "journalctl -u hart-ota-check -n 80 --no-pager")
          # It must have polled OUR central endpoint (not github) and surfaced
          # the central-approved revision.
          assert "Polling CENTRAL" in out, f"check service did not poll central:\n{out}"
          assert "CENTRAL approved rev=${approvedCommit}" in out, \
              f"central-approved rev not parsed:\n{out}"

      with subtest("a new commit STAGES — pending_update.json pins the central flake + commit"):
          node.wait_for_file(PENDING, timeout=30)
          pend = json.loads(node.succeed("cat " + PENDING))
          assert pend["revision"] == "${approvedCommit}", \
              f"staged revision is not central's commit: {pend}"
          # The switch target MUST be central's exact flake_ref, NOT the github
          # channel HEAD — this is the whole point of central pinning.
          assert pend["switch_flake"] == "${approvedFlake}", \
              f"switch_flake is not central's approved flake_ref: {pend}"
          assert pend["channel"] == "stable", f"channel mismatch: {pend}"

      with subtest("staging entered the EXISTING gated pipeline (start_upgrade → BUILDING)"):
          # Reuse proof: the central trigger funnels through the SAME
          # UpgradeOrchestrator. It must be at the FRONT of the 7-stage pipeline
          # (BUILDING), never jumped straight to the switch — the SIGN/CANARY
          # gates still sit between here and DEPLOY.
          node.wait_for_file(STATE, timeout=30)
          st = json.loads(node.succeed("cat " + STATE))
          assert st["stage"] == "building", f"pipeline did not enter BUILDING: {st}"
          assert st["version"] == "${approvedCommit}", f"pipeline version mismatch: {st}"
          # The switch must NOT have run yet (we are pre-canary).
          node.succeed("test ! -e " + REBUILD_LOG)

      # ── 3. autoApply APPLIES at `completed` — hands-off switch to central flake ──
      with subtest("autoApply switches to the CENTRAL-approved flake when pipeline completes"):
          # Drive the orchestrator to `completed` directly (the real run would
          # advance through BUILD→…→CANARY over many ticks; the canary stage is
          # exercised by upgrade_orchestrator's own tests). pending_update.json
          # already carries switch_flake from the staging run above.
          node.succeed(
              "install -o hart -g hart -m640 /dev/stdin " + STATE + " <<'EOF'\n"
              + json.dumps({
                  "stage": "completed",
                  "version": "${approvedCommit}",
                  "git_sha": "${approvedCommit}",
                  "started_at": 0,
                  "stage_history": [],
              }) + "\nEOF")
          node.succeed("rm -f " + REBUILD_LOG + " " + OTA + "/FAIL_SWITCH")

          out = node.succeed("systemctl start hart-ota-check.service; "
                             "journalctl -u hart-ota-check -n 60 --no-pager")
          assert "Auto-apply enabled" in out, f"autoApply branch did not run:\n{out}"
          node.wait_for_file(REBUILD_LOG, timeout=30)
          log = node.succeed("cat " + REBUILD_LOG)
          # The switch target is central's flake#hart-<variant>, applied with NO
          # manual step (oneshot/timer-driven) — the autonomy requirement.
          assert "switch --flake ${approvedFlake}#hart-server" in log, \
              f"did not switch to the central-approved flake:\n{log}"
          # A successful switch must NOT roll back.
          assert "switch --rollback" not in log, \
              f"unexpected rollback on a successful switch:\n{log}"

      # ── 4. A failed apply ROLLS BACK (canary/rollback safety must-not-regress) ──
      with subtest("a failed switch triggers `nixos-rebuild switch --rollback`"):
          # Re-arm `completed` and flip the failure sentinel so the stubbed
          # switch exits non-zero — the module's `|| nixos-rebuild switch
          # --rollback` recovery must fire.
          node.succeed(
              "install -o hart -g hart -m640 /dev/stdin " + STATE + " <<'EOF'\n"
              + json.dumps({
                  "stage": "completed",
                  "version": "${approvedCommit}",
                  "git_sha": "${approvedCommit}",
                  "started_at": 0,
                  "stage_history": [],
              }) + "\nEOF")
          node.succeed("rm -f " + REBUILD_LOG)
          node.succeed("touch " + OTA + "/FAIL_SWITCH")

          out = node.succeed("systemctl start hart-ota-check.service; "
                             "journalctl -u hart-ota-check -n 60 --no-pager")
          node.wait_for_file(REBUILD_LOG, timeout=30)
          log = node.succeed("cat " + REBUILD_LOG)
          assert "switch --flake ${approvedFlake}#hart-server" in log, \
              f"failed-switch attempt not recorded:\n{log}"
          assert "switch --rollback" in log, \
              f"auto-rollback did NOT fire after a failed switch:\n{log}"
          assert "rolling back" in out.lower(), \
              f"check service did not log the rollback path:\n{out}"

      # ── 1(timer). The check timer is BOOT-ONLY — no periodic interval poll ──
      with subtest("hart-ota-check timer fires on boot only (no OnUnitActiveSec)"):
          # The trigger model forbids a periodic poll. systemd-analyze/show must
          # report an OnBoot timestamp and NO OnUnitActiveSec entry.
          props = node.succeed(
              "systemctl show hart-ota-check.timer "
              "-p TimersMonotonic -p NextElapseUSecMonotonic")
          # OnBootSec is present (the boot poll trigger)...
          assert "OnBootSec" in props, \
              f"boot-poll trigger missing from timer:\n{props}"
          # ...and OnUnitActiveSec (the interval poll) is NOT — proving we
          # dropped the hourly/checkInterval recurring poll entirely.
          assert "OnUnitActiveSec" not in props, \
              f"interval poll still scheduled (OnUnitActiveSec present):\n{props}"

      # ── 3. A CENTRAL PUSH triggers the SAME apply, over the existing fabric ──
      with subtest("realtime push leg is wired into the backend (existing fabric)"):
          # The realtime push receiver lives IN hart-backend (the process that
          # holds the WAMP session) — local_subscribers subscribes to the
          # existing 'fleet.command' bus topic and routes OTA pushes to the same
          # apply.  Its bootstrap log line proves the OTA leg was wired (no new
          # transport, no separate subscriber process).
          jb = node.succeed("journalctl -u hart-backend --no-pager | "
                            "grep -i 'Local subscribers bootstrapped' | tail -1")
          assert "ota-push" in jb, \
              f"OTA realtime push leg not wired into backend bootstrap:\n{jb}"

      with subtest("durable push-drain oneshot runs on boot (offline-queued sweep)"):
          # The durable leg: a oneshot that drains central pushes queued while
          # the node was offline and kicks the SAME apply.  Runs as a boot
          # oneshot (NOT a recurring poll).
          node.systemctl("start hart-ota-push.service")
          node.wait_until_succeeds(
              "systemctl is-active hart-ota-push.service "
              "|| systemctl show hart-ota-push.service -p Result | grep -q success",
              timeout=60)

      KICK = OTA + "/push-kick.log"
      # Prepend the recording systemctl stub so the listener's kick is captured.
      STUB_PATH = "${systemctlStub}/bin:$PATH"

      with subtest("a verified central push kicks the SAME hart-ota-check apply"):
          node.succeed("rm -f " + KICK)
          out = node.succeed(
              "PATH=" + STUB_PATH + " ota-push-driver verified")
          assert "kicked=True" in out, \
              f"verified push did not kick the apply:\n{out}"
          node.wait_for_file(KICK, timeout=30)
          klog = node.succeed("cat " + KICK)
          # Convergence proof: the push path starts the EXACT unit the boot poll
          # uses — not a second updater.
          assert "start hart-ota-check.service" in klog, \
              f"push did not converge on hart-ota-check apply:\n{klog}"

      with subtest("a forged (unverified) push is REFUSED — no apply kicked"):
          node.succeed("rm -f " + KICK)
          out = node.succeed(
              "PATH=" + STUB_PATH + " ota-push-driver forged")
          assert "kicked=False" in out, \
              f"forged push was NOT refused:\n{out}"
          # The apply unit must NOT have been kicked by an unauthorized push.
          node.succeed("test ! -e " + KICK)

      # ── /etc/hart/src stays in step with what OTA applied (task #20) ──
      # hart-install freezes the repo at /etc/hart/src for offline rebuilds;
      # without the refresh, a user's `nixos-rebuild` after any applied OTA
      # silently REVERTS to install-time HART. Drives the REAL shipped binary
      # (hart-ota-sync-src, the same one both apply sites call) through all
      # three behaviours: resolvable ref syncs, unresolvable ref keeps the
      # previous copy and says so, image systems no-op.
      with subtest("source sync: a resolvable flake ref replaces /etc/hart/src"):
          # An installed-system stand-in: old source with a marker...
          node.succeed(
              "mkdir -p /etc/hart/src && echo OLD-REV > /etc/hart/src/REV")
          # ...and a fake NEW repo shaped like ours (flake at <root>/nixos, so
          # the ?dir=nixos root-derivation logic is exercised too).
          node.succeed(
              "mkdir -p /tmp/newrepo/nixos",
              "echo '{ outputs = _: { }; }' > /tmp/newrepo/nixos/flake.nix",
              "echo NEW-REV > /tmp/newrepo/REV",
          )
          out = node.succeed("hart-ota-sync-src 'path:/tmp/newrepo?dir=nixos' 2>&1")
          assert "synced to" in out, f"sync did not report success: {out}"
          rev = node.succeed("cat /etc/hart/src/REV").strip()
          assert rev == "NEW-REV", f"/etc/hart/src not refreshed (REV={rev!r})"

      with subtest("source sync: an unresolvable ref KEEPS the previous copy, loudly"):
          out = node.succeed(
              "hart-ota-sync-src 'path:/does-not-exist-anywhere' 2>&1")
          assert "kept at previous rev" in out, f"degrade not reported: {out}"
          rev = node.succeed("cat /etc/hart/src/REV").strip()
          assert rev == "NEW-REV", f"a failed sync must not touch the copy (REV={rev!r})"

      with subtest("source sync: image systems (no /etc/hart/src) are a clean no-op"):
          node.succeed("rm -rf /etc/hart/src")
          out = node.succeed("hart-ota-sync-src 'path:/tmp/newrepo?dir=nixos' 2>&1")
          assert "skipped" in out, f"image no-op not reported: {out}"
          node.succeed("test ! -e /etc/hart/src")
    '';
  };
}
