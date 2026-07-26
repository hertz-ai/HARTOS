# ═══════════════════════════════════════════════════════════════
# HART OS - Endpoint security (hart.security) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves, BEHAVIOURALLY (real booted VM, not grep-on-source), the three layers of
# hart-security.nix AND the load-bearing invariant that the hardening preserves the
# shell + SSH + LAN-diag ports:
#
#   (1) ClamAV antivirus - the clamd daemon unit + the freshclam updater unit are
#       GENERATED and the ClamAV CLIs are in the closure. (The daemon does NOT come
#       up in the offline test sandbox - it has no signature database yet and never
#       blocks boot - so we assert the unit EXISTS, not that it is active. That is
#       the honest degrade-not-die contract: the AV is configured, and is never on
#       the boot-critical path.)
#
#   (2) Firewall hardening - the defense-in-depth sysctls actually took effect,
#       read LIVE from /proc on the running kernel (syncookies on, redirects off,
#       source-route off, ptrace restricted).
#
#   (3) Ports PRESERVED - the shell/backend port (6777), SSH (22), and the LAN
#       netdiag port (6699) all survive the hardening in the live nftables ruleset.
#       This is the "firewall hardening must not break netdiag/shell ports" ask,
#       proven against the real ruleset rather than asserted in Nix.
#
#   Plus: the `hart-security` status CLI runs read-only, exits 0, and names the
#   OTA security-fix delivery path (the module's documented contract that OS + app
#   patches arrive over-the-air, not via a second auto-patcher).
#
# WHY [VM]-gated: a live systemd unit graph, a real running kernel's sysctls, and a
# real nftables ruleset cannot be exercised on the Windows dev box. The portable
# decision logic (option shape, no master-key touch) is covered by the unit test
# tests/unit/test_nixos_security.py; THIS test proves the booted system.
#
# #70 discipline preserved: built from `hartModules` alone via the shared `mkNode`
# (./lib.nix), NO ../configurations/X.nix installer-CD overlay. The hart.security +
# hart.netDiag + hart.firewall enables are set IN-TEST exactly as desktop.nix does.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
  shellPort = 6777;   # hart.ports.backend (the Liquid-UI shell / backend API)
  diagPort  = 6699;   # hart.netDiag.http.port (the LAN read-only diag endpoint)
in
{
  security = pkgs.testers.runNixOSTest {
    name = "hart-security";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME - the same false "Name not defined" as
    # the boot-log / net-diag tests. Skip both static passes; the VM still boots
    # and the assertions still run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sec = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
      };
      # Turn the module ON exactly as desktop.nix does: AV daemon + freshclam
      # updater + the firewall hardening sysctls.
      hart.security = {
        enable = true;
        antivirus = {
          enable = true;
          updates.enable = true;   # the freshclam unit is generated
        };
        firewallHardening.enable = true;
      };
      # The LAN netdiag endpoint - so we can prove the hardening leaves ITS port
      # open (the explicit "keep netdiag ports" requirement). http only, generated
      # token, module-default scoping.
      hart.netDiag = {
        enable = true;
        http = {
          enable = true;
          port = diagPort;
        };
      };
      # Run the production firewall too, to prove hart-security's sysctls + the
      # netdiag RFC1918 scoping + hart-firewall's SYN-flood rule all MERGE cleanly
      # (no eval conflict) and the shell/SSH ports survive end to end.
      hart.firewall.enable = true;
      # desktop.nix runs the nftables backend; mkNode does not pull desktop.nix, so
      # enable it here to exercise the SAME ruleset path production uses.
      networking.nftables.enable = true;
      environment.systemPackages = [ pkgs.curl ];
    };

    testScript = ''
      # The driver keys the single machine global by HOSTNAME - mkNode forces it to
      # the variant ("desktop"), NOT the nodes.sec key. Bind from machines[0].
      sec = machines[0]
      sec.start()
      sec.wait_for_unit("multi-user.target")

      SHELL_PORT = "${toString shellPort}"
      DIAG_PORT  = "${toString diagPort}"

      # ── 1. ClamAV: the daemon + freshclam units are GENERATED + CLIs in closure ──
      with subtest("(1) ClamAV clamd + freshclam units exist and the CLIs are present"):
          # The unit files are generated regardless of network (the daemon itself
          # cannot become active offline - no DB - and is never boot-critical, so we
          # assert the unit EXISTS, not that it is active).
          sec.succeed("systemctl cat clamav-daemon.service")
          sec.succeed("systemctl cat clamav-freshclam.service")
          # The ClamAV CLIs are on PATH (the clamav package is in the closure).
          sec.succeed("command -v clamscan")
          sec.succeed("command -v freshclam")
          sec.succeed("command -v clamdscan")
          # The good-citizen resource limits we set on the daemon are present.
          nice = sec.succeed(
              "systemctl show clamav-daemon.service -p Nice --value").strip()
          assert nice == "10", f"clamd Nice must be 10 (low prio), got {nice!r}"
          oom = sec.succeed(
              "systemctl show clamav-daemon.service -p OOMScoreAdjust --value").strip()
          assert oom == "500", f"clamd OOMScoreAdjust must be 500, got {oom!r}"

      # ── 2. Firewall hardening: the sysctls took effect on the LIVE kernel ──
      with subtest("(2) the hardening sysctls are applied (read live from /proc)"):
          syncookies = sec.succeed("sysctl -n net.ipv4.tcp_syncookies").strip()
          assert syncookies == "1", f"tcp_syncookies must be 1, got {syncookies!r}"
          send_redir = sec.succeed(
              "sysctl -n net.ipv4.conf.all.send_redirects").strip()
          assert send_redir == "0", f"send_redirects must be 0, got {send_redir!r}"
          src_route = sec.succeed(
              "sysctl -n net.ipv4.conf.all.accept_source_route").strip()
          assert src_route == "0", f"accept_source_route must be 0, got {src_route!r}"
          ptrace = sec.succeed("sysctl -n kernel.yama.ptrace_scope").strip()
          assert ptrace == "1", f"ptrace_scope must be 1 (restricted), got {ptrace!r}"

      # ── 3. Ports PRESERVED: shell + SSH + netdiag all survive in the ruleset ──
      with subtest("(3) hardening keeps the shell/SSH/netdiag ports open"):
          rules = sec.succeed("nft list ruleset 2>/dev/null || true")
          # The shell/backend port and SSH are opened by hart-base (allowedTCPPorts)
          # -> they must be present in the rendered nftables ruleset.
          assert SHELL_PORT in rules, \
              f"the shell/backend port {SHELL_PORT} was stripped from the ruleset"
          assert "dport 22" in rules or "dport ssh" in rules or " 22 " in rules, \
              "SSH (22) was stripped from the firewall ruleset"
          # The LAN netdiag port is opened by hart-net-diag (RFC1918-scoped) -> it
          # must still be reachable on the LAN, source-scoped (carries saddr).
          diag_lines = [ln for ln in rules.splitlines()
                        if ("dport " + DIAG_PORT) in ln and "accept" in ln]
          assert diag_lines, \
              f"the netdiag port {DIAG_PORT} was stripped by the hardening"
          for ln in diag_lines:
              assert "saddr" in ln, \
                  f"netdiag port accept is not LAN-source-scoped any more: {ln!r}"

      # ── 4. The status CLI runs read-only, exits 0, and names the OTA path ──
      with subtest("(4) hart-security status reports AV + firewall + OTA"):
          sec.succeed("command -v hart-security")
          out = sec.succeed("hart-security status; echo RC=$?")
          assert "RC=0" in out, f"hart-security status must exit 0, got: {out[-200:]!r}"
          assert "ClamAV" in out, "status did not report the ClamAV layer"
          assert "Firewall hardening" in out, "status did not report the firewall layer"
          # The module's documented contract: OS/app patches arrive over-the-air.
          assert "over-the-air" in out or "hart-ota" in out, \
              "status did not name the OTA security-fix delivery path"
          assert "tcp_syncookies" in out, "status did not surface the live sysctls"
    '';
  };
}
