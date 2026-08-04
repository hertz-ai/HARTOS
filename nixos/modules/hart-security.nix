{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS - Endpoint security (ClamAV antivirus + firewall hardening)
# ═══════════════════════════════════════════════════════════════
#
# THREE layers of protection, each LOCAL-first:
#
#   (1) ClamAV antivirus  - the clamd scanning daemon + freshclam signature
#       updater. The daemon scans LOCALLY (no data ever leaves the box). The
#       ONLY network use is freshclam pulling signature updates, which is gated
#       behind hart.security.antivirus.updates.enable - exactly the way fwupd's
#       weekly firmware check (hart-firewall.nix) and the OTA pull (hart-ota.nix)
#       reach out: on by default, but a single switch turns the egress off.
#
#   (2) Firewall hardening - defense-in-depth kernel sysctls that COMPLEMENT (do
#       NOT replace) the existing nftables firewall. hart-base.nix + hart-firewall
#       .nix already open the ports + SYN-flood-rate-limit; this module ADDS the
#       network-stack hardening they do not set (no redirects, no source routing,
#       log martians, SYN cookies, restricted ptrace). It is purely ADDITIVE: it
#       opens NO new port and CLOSES nothing - the HART shell/backend port, SSH,
#       and the LAN netdiag port all stay exactly as the other modules left them.
#       An eval-time assertion makes "never strip the shell/SSH port" structural.
#
#   (3) OTA security fixes - the OS itself and the HART application are patched
#       OVER-THE-AIR via hart-ota.nix (the signed, canary, auto-rollback pipeline).
#       ClamAV + the firewall protect the RUNNING system from external threats;
#       OTA delivers the CODE fixes (kernel CVEs, app patches) as a new signed
#       NixOS generation. The three are complementary: AV = files, firewall =
#       network, OTA = code. There is intentionally NO bespoke auto-patcher here -
#       duplicating hart-ota would be a parallel update path (CLAUDE.md Gate 4).
#
# PRIVACY / DECENTRALIZATION: every protection runs LOCALLY by default. The lone
#   network egress (signature downloads) is a toggle, and it needs no central HART
#   authority - freshclam pulls from the public ClamAV mirrors, peer-to-peer-style,
#   matching the decentralization-first lens (a feature must still work with the
#   HART central OFF). This module never reads, writes, or touches any master-key /
#   guardrail / node-key material - it is pure system hardening.
#
# BOOT-SAFE / DEGRADE-NOT-DIE: nothing here is on the boot-critical path. clamav
#   -daemon is NOT a boot dependency - on a first, offline boot it simply has no
#   signature database yet and stays inactive until freshclam runs once online;
#   that failure can never block or slow the desktop. The sysctls are applied by
#   systemd-sysctl (best-effort) and a missing key is ignored. A wrong value here
#   degrades a protection, it never bricks a boot.
#
# VM-gated: tests/security.nix BOOTS a desktop node, enables this module + the LAN
#   netdiag endpoint, and proves BEHAVIOURALLY that the clamd + freshclam units are
#   generated, the ClamAV CLIs are in the closure, the hardening sysctls took
#   effect (read live from /proc), and - the load-bearing invariant - the shell
#   port (6777), SSH (22), and the netdiag port (6699) all SURVIVE the hardening.

let
  cfg = config.hart;
  sec = config.hart.security;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.security = {
    enable = lib.mkEnableOption ''
      HART OS endpoint security: the ClamAV antivirus daemon + signature updater
      and defense-in-depth firewall/kernel hardening. LOCAL-first (the scanner and
      every sysctl run on the box); the only network egress is the signature
      updater, which is its own toggle. Additive to the existing nftables firewall
      (opens no new port, closes none). OS + app security fixes arrive separately
      over-the-air via hart-ota. OFF unless enabled -> a pure no-op for every
      variant; the desktop config turns it on'';

    antivirus = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Run the ClamAV clamd scanning daemon (LOCAL on-demand + scriptable virus
          scanning, e.g. `hart-security scan`). On by default (privacy-first: a
          purely local protection ships enabled). The daemon never sends anything
          off the box. Turn OFF to ship without an AV daemon (the firewall
          hardening + OTA still apply).
        '';
      };

      updates = {
        enable = lib.mkOption {
          type = lib.types.bool;
          default = true;
          description = ''
            Let freshclam pull ClamAV signature updates from the public mirrors.
            This is the module's ONLY network egress. Gated exactly like the fwupd
            firmware check and the OTA pull (on by default, one switch to stop the
            egress). Turn OFF for a fully air-gapped node - the daemon then scans
            with whatever database it already has (or none, until updated by hand).
          '';
        };

        frequency = lib.mkOption {
          type = lib.types.ints.positive;
          default = 12;
          description = ''
            How many times per day freshclam checks the mirrors for new signatures
            (ClamAV's own `Checks` setting). 12 = roughly every two hours. Only
            meaningful when antivirus.updates.enable is true.
          '';
        };
      };
    };

    firewallHardening = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Apply defense-in-depth network/kernel hardening sysctls that COMPLEMENT
          the existing nftables firewall (no redirects, no source routing, log
          martians, SYN cookies, restricted ptrace). Purely ADDITIVE: opens no new
          port and closes none - the shell/backend port, SSH, and the LAN netdiag
          port are left exactly as hart-base / hart-firewall / hart-net-diag set
          them. An eval-time assertion enforces that the shell + SSH ports survive.
        '';
      };
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when hart.security.enable = false)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && sec.enable) (lib.mkMerge [

    # ── (1) ClamAV antivirus: the clamd daemon + the freshclam updater ──
    (lib.mkIf sec.antivirus.enable {
      services.clamav = {
        # The LOCAL scanning daemon. Loads the signature DB and answers scan
        # requests over its socket (clamdscan / on-demand). No egress.
        daemon.enable = true;

        # freshclam: the ONLY network use. A no-op when updates.enable is false
        # (the air-gapped path) - the daemon then scans with its existing DB.
        updater = lib.mkIf sec.antivirus.updates.enable {
          enable = true;
          frequency = sec.antivirus.updates.frequency;
        };
      };

      # The ClamAV CLIs (clamscan / clamdscan / freshclam / clamd) on PATH.
      environment.systemPackages = [ pkgs.clamav ];

      # Keep the scanner a GOOD CITIZEN on a desktop: low CPU + idle I/O priority
      # so a background scan never steals the interactive desktop, and a high
      # OOM score so that under memory pressure the kernel sacrifices clamd (which
      # can recover) BEFORE the user's session (degrade-not-die). mkDefault so a
      # future explicit override wins; these keys are not set by the upstream unit.
      systemd.services.clamav-daemon.serviceConfig = {
        Nice = lib.mkDefault 10;
        IOSchedulingClass = lib.mkDefault "idle";
        OOMScoreAdjust = lib.mkDefault 500;
      };
    })

    # ── (2) Firewall hardening: defense-in-depth sysctls (additive) ──
    (lib.mkIf sec.firewallHardening.enable {
      # mkDefault throughout: these keys are NOT set by hart-base/hart-kernel.
      # (HISTORY: hart-devtools used to set kernel.yama.ptrace_scope = 0 WITHOUT
      # priority, so merely installing debuggers beat this default. That was
      # written when devtools was an opt-in developer choice; once the desktop
      # PROFILE enabled devtools for every shipped machine it silently became an
      # OS-wide posture change and contradicted the ptrace assertion in
      # nixos/tests/security.nix. It is now gated behind the explicit
      # hart.ideTools.ptraceUnrestricted, which mkForces it — so this hardening
      # holds unless a box deliberately opts out.) Every value here only
      # tightens the network stack; none opens a port or changes routing the LAN
      # needs.
      boot.kernel.sysctl = {
        # SYN-flood resilience (complements hart-firewall's nftables rate limit).
        "net.ipv4.tcp_syncookies" = lib.mkDefault 1;
        # Protect against TIME-WAIT assassination (RFC 1337).
        "net.ipv4.tcp_rfc1337" = lib.mkDefault 1;
        # We are an endpoint, not a router: never SEND ICMP redirects.
        "net.ipv4.conf.all.send_redirects" = lib.mkDefault 0;
        "net.ipv4.conf.default.send_redirects" = lib.mkDefault 0;
        # Do not ACCEPT ICMP redirects (route-hijack hardening). hart-base already
        # zeroes accept_redirects; secure_redirects is the gateway-scoped variant
        # it does not set.
        "net.ipv4.conf.all.secure_redirects" = lib.mkDefault 0;
        "net.ipv4.conf.default.secure_redirects" = lib.mkDefault 0;
        # Reject source-routed packets (spoofing hardening), v4 + v6.
        "net.ipv4.conf.all.accept_source_route" = lib.mkDefault 0;
        "net.ipv4.conf.default.accept_source_route" = lib.mkDefault 0;
        "net.ipv6.conf.all.accept_source_route" = lib.mkDefault 0;
        "net.ipv6.conf.default.accept_source_route" = lib.mkDefault 0;
        # Log impossible (martian) source addresses - visibility for the netdiag
        # bundle without opening anything.
        "net.ipv4.conf.all.log_martians" = lib.mkDefault 1;
        "net.ipv4.conf.default.log_martians" = lib.mkDefault 1;
        # Restrict ptrace to direct children (anti-process-snooping). mkDefault so
        # hart-devtools (which sets it to 0 for debugging) still wins when enabled.
        "kernel.yama.ptrace_scope" = lib.mkDefault 1;
      };

      # ── The load-bearing invariant: hardening must NEVER strip shell/SSH access.
      # A future change that mkForce-empties allowedTCPPorts (or removes the shell
      # port) becomes a BUILD failure here, not a silent "can't reach the desktop
      # API / can't SSH in" brick. The netdiag port (6699) is opened by
      # hart-net-diag via extraInputRules/interfaces (not allowedTCPPorts), so it
      # is verified BEHAVIOURALLY in tests/security.nix rather than asserted here.
      assertions = [
        {
          assertion =
            (lib.elem cfg.ports.backend config.networking.firewall.allowedTCPPorts)
            && (lib.elem 22 config.networking.firewall.allowedTCPPorts);
          message = ''
            hart.security.firewallHardening is additive and must never close the
            HART shell/backend port (${toString cfg.ports.backend}) or SSH (22),
            but one of them is missing from networking.firewall.allowedTCPPorts.
            Restore the port in hart-base/hart-firewall - the hardening layer only
            tightens the kernel network stack, it does not manage the port list.
          '';
        }
      ];
    })

    # ── (3) Always-on (when the module is enabled): the status CLI + OTA note ──
    {
      # `hart-security status` surfaces all three layers at a glance, including the
      # explicit reminder that OS + application security fixes are delivered over
      # the air (NOT by any second auto-patcher in this module). clamdscan/clamscan
      # are referenced by store path ONLY when the AV daemon is enabled, so the CLI
      # never drags clamav into the closure when antivirus.enable = false.
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-security" ''
          set -u
          AVSCAN="${if sec.antivirus.enable then "${pkgs.clamav}/bin/clamdscan" else ""}"
          AVFULL="${if sec.antivirus.enable then "${pkgs.clamav}/bin/clamscan" else ""}"
          cmd="''${1:-status}"
          case "$cmd" in
            status)
              echo "=== HART OS Security ==="
              echo ""
              echo "[Antivirus: ClamAV]"
              if systemctl list-unit-files clamav-daemon.service >/dev/null 2>&1; then
                echo "  daemon:     $(systemctl is-active clamav-daemon.service 2>/dev/null || echo inactive)"
                echo "  updater:    $(systemctl is-active clamav-freshclam.service 2>/dev/null || echo inactive)"
                if [ -f /var/lib/clamav/daily.cvd ] || [ -f /var/lib/clamav/daily.cld ]; then
                  echo "  signatures: present"
                else
                  echo "  signatures: not yet downloaded (freshclam pulls them once online)"
                fi
              else
                echo "  ClamAV daemon not enabled on this node"
              fi
              echo ""
              echo "[Firewall hardening]"
              echo "  tcp_syncookies:    $(cat /proc/sys/net/ipv4/tcp_syncookies 2>/dev/null || echo '?')"
              echo "  send_redirects:    $(cat /proc/sys/net/ipv4/conf/all/send_redirects 2>/dev/null || echo '?')"
              echo "  accept_src_route:  $(cat /proc/sys/net/ipv4/conf/all/accept_source_route 2>/dev/null || echo '?')"
              echo ""
              echo "[OS + application security fixes]"
              echo "  Delivered over-the-air via hart-ota (signed, canary, auto-rollback)."
              echo "  ClamAV + the firewall protect the running system; OTA patches the code."
              ;;
            scan)
              shift
              target="''${1:-$HOME}"
              if [ -z "$AVSCAN" ]; then
                echo "Antivirus is disabled on this node (hart.security.antivirus.enable = false)."
                exit 1
              fi
              echo "Scanning $target (on-demand) ..."
              "$AVSCAN" --fdpass --multiscan "$target" 2>/dev/null || "$AVFULL" -r "$target"
              ;;
            update)
              if [ -z "$AVSCAN" ]; then
                echo "Antivirus is disabled on this node."
                exit 1
              fi
              echo "Refreshing ClamAV signatures ..."
              sudo systemctl start clamav-freshclam.service
              ;;
            help|--help|-h)
              echo "hart-security - HART OS endpoint security"
              echo ""
              echo "  hart-security status    Show antivirus + firewall + OTA status"
              echo "  hart-security scan [P]  On-demand virus scan (default: \$HOME)"
              echo "  hart-security update    Refresh virus signatures now"
              ;;
            *)
              echo "Unknown command: $cmd (try: hart-security help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
