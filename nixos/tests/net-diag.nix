# ═══════════════════════════════════════════════════════════════
# HART OS — LAN-path diagnostics (hart.netDiag) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the steward's "log to the network path journalctl instead of in pendrive"
# loop: a desktop node enables hart.netDiag, and the read-only HTTP diag endpoint
# returns the live journal over the network when (and ONLY when) a valid token is
# presented.
#
# This test is BEHAVIOURAL (not grep-on-source): it BOOTS a real VM, lets the
# token-gated HTTP service come up, and drives it with a REAL curl over loopback:
#   - a VALID token  -> 200 + the actual diagnostic sections (journalctl/ip/rfkill)
#   - a WRONG token  -> 403 (fail-closed)
#   - NO token       -> 403 (fail-closed)
# It then proves the network-up + plumbing pieces: the firewall opens the port, the
# boot rfkill-unblock oneshot ran (exit 0), and the read-only diag CLI is on PATH.
#
# WHY [VM]-gated: a live systemd service + a real kernel rfkill subsystem + a real
# loopback HTTP round-trip cannot run on the Windows dev box. The real "the dev box
# curls the live-OS box across the home LAN" still needs two physical machines;
# THIS test proves every link short of the second box.
#
# #70 discipline preserved: built from `hartModules` alone via the shared `mkNode`
# (./lib.nix), NO ../configurations/X.nix installer-CD overlay. The netDiag enables
# are set IN-TEST (the same way desktop.nix sets them in production).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
  token = "test-diag-token-9f3a";
  port  = 6699;
in
{
  net-diag = pkgs.testers.runNixOSTest {
    name = "hart-net-diag";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME — same false "Name not defined" as the
    # boot-log / network-wifi tests. Skip both static passes; the VM still boots
    # and the assertions still run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.nd = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
      # Turn the module ON with a known token + the default port. http only —
      # netconsole/push need a dev-box target, exercised by the unit-level option
      # plumbing, not a live UDP/POST round-trip here.
      hart.netDiag = {
        enable = true;
        http = {
          enable = true;
          inherit port;
          inherit token;
        };
        wifiUnblock.enable = true;
        usbEthernet.enable = true;
        # netconsole + push stay OFF (no target) — their units must be absent.
      };
      # curl for the loopback round-trip.
      environment.systemPackages = [ pkgs.curl ];
    };

    testScript = ''
      # The driver keys the single machine global by HOSTNAME — mkNode forces it to
      # the variant ("desktop"), NOT the nodes.nd key. Bind from machines[0].
      nd = machines[0]
      nd.start()
      nd.wait_for_unit("multi-user.target")

      PORT = "${toString port}"
      TOKEN = "${token}"
      BASE = "http://127.0.0.1:" + PORT

      # ── 1. The HTTP diag service + the rfkill-unblock oneshot are in the closure ──
      with subtest("the netDiag units exist and the HTTP service is up"):
          nd.succeed("systemctl cat hart-net-diag-http.service")
          nd.succeed("systemctl cat hart-net-diag-rfkill-unblock.service")
          nd.wait_for_unit("hart-net-diag-http.service")
          # The endpoint is listening on the configured port.
          nd.wait_until_succeeds(
              "curl -s -o /dev/null -m 5 " + BASE + "/diag?t=" + TOKEN, timeout=30)
          # The read-only diag CLI is on PATH for a manual recovery run.
          nd.succeed("command -v hart-net-diag-collect")

      # ── 2. A VALID token returns 200 + the REAL diagnostic sections ──
      with subtest("a valid token returns 200 + the live diagnostic bundle"):
          code = nd.succeed(
              "curl -s -o /tmp/diag.txt -w '%{http_code}' -m 25 "
              + BASE + "/diag?t=" + TOKEN).strip()
          assert code == "200", f"valid-token request must be 200, got {code!r}"
          body = nd.succeed("cat /tmp/diag.txt")
          for needle in [
              "HART OS LAN diagnostic bundle",
              "ip -br a",
              "rfkill",
              "FULL current-boot journal",
              "end of bundle",
          ]:
              assert needle in body, f"diag bundle missing section: {needle!r}"
          # The journal section actually carries lines (not an empty bundle).
          assert len(body) > 500, f"diag bundle implausibly small ({len(body)} bytes)"

      # ── 3. FAIL-CLOSED: a wrong token AND a missing token both 403 ──
      with subtest("a wrong token returns 403 (fail-closed)"):
          code = nd.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -m 10 "
              + BASE + "/diag?t=not-the-token").strip()
          assert code == "403", f"wrong-token request must be 403, got {code!r}"

      with subtest("a missing token returns 403 (fail-closed)"):
          code = nd.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -m 10 "
              + BASE + "/diag").strip()
          assert code == "403", f"no-token request must be 403, got {code!r}"
          # A non-/diag path is also denied.
          code2 = nd.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -m 10 "
              + BASE + "/etc/passwd").strip()
          assert code2 == "403", f"non-diag path must be 403, got {code2!r}"

      # ── 4. The firewall opens the diag port ──
      with subtest("the firewall opens the HTTP diag port"):
          # nftables (hart-firewall) or iptables — assert the port appears in the
          # active ruleset. The exact backend varies; check both.
          rules = nd.succeed(
              "{ nft list ruleset 2>/dev/null; iptables-save 2>/dev/null; } || true")
          assert PORT in rules, \
              f"diag port {PORT} not found in the active firewall ruleset"

      # ── 5. The boot rfkill-unblock oneshot ran and succeeded ──
      with subtest("the rfkill-unblock oneshot ran (exit 0)"):
          # RemainAfterExit oneshot -> stays 'active' after a successful run.
          nd.wait_for_unit("hart-net-diag-rfkill-unblock.service")
          res = nd.succeed(
              "systemctl show hart-net-diag-rfkill-unblock.service "
              "-p Result --value").strip()
          assert res == "success", f"rfkill-unblock Result must be success, got {res!r}"

      # ── 6. netconsole + push units are ABSENT (no target configured) ──
      with subtest("netconsole + push units are absent when no target is set"):
          nd.fail("systemctl cat hart-net-diag-netconsole.service")
          nd.fail("systemctl cat hart-net-diag-push.timer")

      # ── 7. The CLI bundle is read-only + self-contained (runs without the HTTP) ──
      with subtest("the diag CLI prints a bundle directly (read-only)"):
          out = nd.succeed("hart-net-diag-collect 2>&1; echo RC=$?")
          assert "RC=0" in out, f"diag CLI must exit 0, got tail: {out[-200:]!r}"
          assert "HART OS LAN diagnostic bundle" in out, \
              "diag CLI did not print the bundle header"
    '';
  };
}
