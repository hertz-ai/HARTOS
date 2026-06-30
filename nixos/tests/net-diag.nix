# ═══════════════════════════════════════════════════════════════
# HART OS - LAN-path diagnostics (hart.netDiag) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the steward's "log to the network path journalctl instead of in pendrive"
# loop AND the #148 hardening: a desktop node enables hart.netDiag, and the
# read-only HTTP diag endpoint returns the live journal over the network when (and
# ONLY when) the FIRST-BOOT-GENERATED token is presented.
#
# This test is BEHAVIOURAL (not grep-on-source): it BOOTS a real VM, lets the
# first-boot token generator + the token-gated HTTP service come up, and drives
# them with a REAL curl:
#   - the GENERATED token (read from the 0600 tmpfs file) -> 200 + real sections
#   - a WRONG token  -> 403 (fail-closed)
#   - NO token       -> 403 (fail-closed)
#
# #148 hardening asserted here:
#   (a) TOKEN is GENERATED-not-default: /run/hart/netdiag-token exists, is 0600,
#       is non-empty, and is NOT the retired "hart-lan-diag" default; the HTTP
#       unit Environment carries NO token (no `systemctl show` leak).
#   (b) LAN-BOUND: the firewall opens the port ONLY from RFC1918 + link-local
#       SOURCE ranges (never a bare global accept).
#   (c) READ-ONLY: only GET /diag is served; a POST is rejected (no write path).
#   (d) SECRET EXCLUSION: a planted PRIVATE_KEY / PEM line is REDACTED out of the
#       bundle (and the diag token never re-leaks into the served journal).
#
# WHY [VM]-gated: a live systemd service + a real kernel rfkill subsystem + a real
# HTTP round-trip cannot run on the Windows dev box. The real "the dev box curls
# the live-OS box across the home LAN" still needs two physical machines; THIS test
# proves every link short of the second box.
#
# #70 discipline preserved: built from `hartModules` alone via the shared `mkNode`
# (./lib.nix), NO ../configurations/X.nix installer-CD overlay. The netDiag enables
# are set IN-TEST (the same way desktop.nix sets them in production).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
  port  = 6699;
in
{
  net-diag = pkgs.testers.runNixOSTest {
    name = "hart-net-diag";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME - same false "Name not defined" as the
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
      # Turn the module ON exactly as desktop.nix does: http only, NO static token
      # (so the first-boot generator mints a random one), bindAddress "auto" + the
      # RFC1918 firewall scoping (the module defaults). netconsole/push stay OFF.
      hart.netDiag = {
        enable = true;
        http = {
          enable = true;
          inherit port;
          # token = "" -> generated at first boot.
          # bindAddress = "auto" + RFC1918 firewall scope (module defaults).
        };
        wifiUnblock.enable = true;
        usbEthernet.enable = true;
      };
      # desktop.nix runs the nftables backend (hart.firewall.enable=true); mkNode
      # does NOT pull desktop.nix, so enable nftables here to exercise the SAME
      # RFC1918 source-scoping path production uses.
      networking.nftables.enable = true;
      # curl for the round-trip.
      environment.systemPackages = [ pkgs.curl ];
    };

    testScript = ''
      # The driver keys the single machine global by HOSTNAME - mkNode forces it to
      # the variant ("desktop"), NOT the nodes.nd key. Bind from machines[0].
      nd = machines[0]
      nd.start()
      nd.wait_for_unit("multi-user.target")

      PORT = "${toString port}"

      # ── 1. The first-boot token generator ran; the token is GENERATED, 0600,
      #       not the retired default; the HTTP unit has NO token in Environment ──
      with subtest("#148a: the token is generated-not-default, 0600, no systemctl-show leak"):
          nd.succeed("systemctl cat hart-net-diag-token.service")
          nd.wait_for_unit("hart-net-diag-token.service")
          # The 0600 tmpfs token file exists.
          nd.succeed("test -f /run/hart/netdiag-token")
          mode = nd.succeed("stat -c %a /run/hart/netdiag-token").strip()
          assert mode == "600", f"token file must be 0600, got {mode!r}"
          TOKEN = nd.succeed("cat /run/hart/netdiag-token").strip()
          assert TOKEN, "the first-boot token must be non-empty"
          assert TOKEN != "hart-lan-diag", \
              "the retired hardcoded default token must NOT be in use"
          assert len(TOKEN) >= 16, f"generated token implausibly short: {TOKEN!r}"
          # The token must NOT be carried in the HTTP unit Environment (would leak
          # via `systemctl show`). Only the token FILE path may appear.
          env = nd.succeed("systemctl show hart-net-diag-http.service -p Environment --value")
          assert "HART_NETDIAG_TOKEN=" not in env, \
              "the raw token must NOT be in the unit Environment (systemctl-show leak)"
          assert TOKEN not in env, "the token value must NOT appear in the unit Environment"
          assert "HART_NETDIAG_TOKEN_FILE=" in env, "the unit must point at the token FILE"

      # ── 2. The HTTP service is up; serve address derived from the live socket ──
      with subtest("the HTTP diag service is up"):
          nd.wait_for_unit("hart-net-diag-http.service")
          # bindAddress="auto" binds the private LAN IP when a route exists, else
          # falls back SAFE to 0.0.0.0 (the firewall still scopes the port) - derive
          # whatever it actually bound from the live socket and curl THAT.
          laddr = ""
          for col in nd.succeed("ss -ltnH 2>/dev/null | awk '{print $4}'").split():
              if col.endswith(":" + PORT):
                  laddr = col
                  break
          host = laddr.rsplit(":", 1)[0] if laddr else "127.0.0.1"
          # Never a public bind: it is either a private LAN IP or the firewall-scoped
          # 0.0.0.0 fallback - NEVER a globally-routable address.
          assert host in ("0.0.0.0", "127.0.0.1", "::", "*", "[::]") \
              or host.startswith(("10.", "192.168.", "169.254.")) \
              or any(host.startswith("172.%d." % o) for o in range(16, 32)), \
              f"diag endpoint bound a non-LAN address: {host!r}"
          curl_host = "127.0.0.1" if host in ("0.0.0.0", "::", "*", "[::]") else host
          BASE = "http://" + curl_host + ":" + PORT
          nd.wait_until_succeeds(
              "curl -s -o /dev/null -m 5 " + BASE + "/diag?t=" + TOKEN, timeout=30)
          nd.succeed("command -v hart-net-diag-collect")

      # ── 3. The GENERATED token returns 200 + the REAL diagnostic sections ──
      with subtest("the generated token returns 200 + the live diagnostic bundle"):
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
          assert len(body) > 500, f"diag bundle implausibly small ({len(body)} bytes)"
          # The diag token must NEVER re-leak into the served journal (it is echoed
          # to the journal by the generator, then redacted on the way out).
          assert TOKEN not in body, "the diag token leaked into the served bundle (redaction failed)"

      # ── 4. FAIL-CLOSED + REQUIRED: a wrong token AND a missing token both 403 ──
      with subtest("#148: a wrong/absent token returns 403 (fail-closed, required)"):
          code = nd.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -m 10 "
              + BASE + "/diag?t=not-the-token").strip()
          assert code == "403", f"wrong-token request must be 403, got {code!r}"
          code = nd.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -m 10 "
              + BASE + "/diag").strip()
          assert code == "403", f"no-token request must be 403, got {code!r}"
          code2 = nd.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -m 10 "
              + BASE + "/etc/passwd").strip()
          assert code2 == "403", f"non-diag path must be 403, got {code2!r}"

      # ── 5. READ-ONLY: only GET /diag is served; a POST has no handler (no write) ──
      with subtest("#148c: the endpoint is read-only (POST is not implemented)"):
          code = nd.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST "
              + BASE + "/diag?t=" + TOKEN).strip()
          # BaseHTTPRequestHandler answers an unimplemented method with 501.
          assert code == "501", f"POST must be unimplemented (501), got {code!r}"

      # ── 6. LAN-BOUND: the firewall scopes the port to RFC1918 sources (not global) ──
      with subtest("#148b: the firewall opens the diag port LAN-only (RFC1918 scoped)"):
          rules = nd.succeed("nft list ruleset 2>/dev/null || true")
          assert PORT in rules, f"diag port {PORT} not present in the nftables ruleset"
          # Every accept rule that mentions the port must ALSO be source-scoped to a
          # LAN range - i.e. carry `saddr` on the same rule. A bare global accept
          # (`tcp dport 6699 accept` with no saddr) would be a leak.
          port_lines = [ln for ln in rules.splitlines()
                        if ("dport " + PORT) in ln and "accept" in ln]
          assert port_lines, f"no accept rule for dport {PORT} found"
          for ln in port_lines:
              assert "saddr" in ln, \
                  f"diag port accept is NOT source-scoped to the LAN (global leak): {ln!r}"
          assert "hart-netdiag LAN-only" in rules, \
              "the LAN-only scoping rule/comment is missing from the ruleset"

      # ── 7. SECRET EXCLUSION: a planted key/secret is REDACTED out of the bundle ──
      with subtest("#148d: the bundle redacts master-key / private-key / secret material"):
          # Plant fake secrets into the journal, then read the bundle via the CLI
          # (deterministic, no HTTP/routing) and prove they are gone + redacted.
          nd.succeed("logger 'HEVOLVE_MASTER_PRIVATE_KEY=DEADBEEFMASTERLEAK0001'")
          nd.succeed("logger 'some_api_token = SECRETTOKENLEAK0002'")
          nd.succeed("logger -- '-----BEGIN OPENSSH PRIVATE KEY----- PEMBODYLEAK0003'")
          # Give journald a moment to flush, then capture the bundle.
          nd.succeed("journalctl --sync 2>/dev/null || true")
          out = nd.succeed("hart-net-diag-collect")
          for leak in [
              "DEADBEEFMASTERLEAK0001",
              "SECRETTOKENLEAK0002",
              "PEMBODYLEAK0003",
          ]:
              assert leak not in out, f"SECRET LEAKED through the diag bundle: {leak!r}"
          assert "<redacted" in out, \
              "the bundle never redacted anything - the redaction filter is not wired"

      # ── 8. The boot rfkill-unblock oneshot ran and succeeded ──
      with subtest("the rfkill-unblock oneshot ran (exit 0)"):
          nd.wait_for_unit("hart-net-diag-rfkill-unblock.service")
          res = nd.succeed(
              "systemctl show hart-net-diag-rfkill-unblock.service "
              "-p Result --value").strip()
          assert res == "success", f"rfkill-unblock Result must be success, got {res!r}"

      # ── 9. netconsole + push units are ABSENT (no target configured) ──
      with subtest("netconsole + push units are absent when no target is set"):
          nd.fail("systemctl cat hart-net-diag-netconsole.service")
          nd.fail("systemctl cat hart-net-diag-push.timer")

      # ── 10. The diag CLI is read-only + self-contained (runs without the HTTP) ──
      with subtest("the diag CLI prints a bundle directly (read-only)"):
          out = nd.succeed("hart-net-diag-collect 2>&1; echo RC=$?")
          assert "RC=0" in out, f"diag CLI must exit 0, got tail: {out[-200:]!r}"
          assert "HART OS LAN diagnostic bundle" in out, \
              "diag CLI did not print the bundle header"
    '';
  };
}
