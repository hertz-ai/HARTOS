# HART OS — SANE Scanner Support
#
# Scanner hardware support via SANE backends, GUI scanning with
# simple-scan, and optional network scanning via saned.
#
# CLI: hart-scanner status|list|test

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.scanner;
in
{
  options.hart.scanner = {
    enable = lib.mkEnableOption "HART OS scanner support (SANE)";

    networkScanning = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable saned for network-accessible scanning.";
    };

    allowedClients = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "127.0.0.1" "::1" ];
      description = "IP addresses allowed to access the network scanner.";
      example = [ "192.168.1.0/24" ];
    };

    extraBackends = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [];
      description = "Additional SANE backend packages (e.g., vendor drivers).";
      example = lib.literalExpression "[ pkgs.sane-airscan ]";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── SANE scanner backends ──
    hardware.sane = {
      enable = true;
      extraBackends = cfg.extraBackends ++ [ pkgs.sane-airscan ];
    };

    # ── User packages: GUI scanner + CLI tools ──
    environment.systemPackages = with pkgs; [
      simple-scan             # GTK scanner GUI
      sane-backends           # scanimage, scanadf CLI tools

      (writeShellScriptBin "hart-scanner" ''
        case "''${1:-status}" in
          status)
            echo "=== HART OS Scanner Service ==="
            echo "Network scanning: ${if cfg.networkScanning then "enabled" else "disabled"}"
            echo ""
            echo "Detected scanners:"
            ${sane-backends}/bin/scanimage -L 2>/dev/null || echo "No scanners found"
            ;;
          list)
            ${sane-backends}/bin/scanimage -L 2>/dev/null || echo "No scanners detected"
            ;;
          test)
            scanner="''${2:-}"
            echo "Scanning test image..."
            if [ -n "$scanner" ]; then
              ${sane-backends}/bin/scanimage -d "$scanner" --format=png -o /tmp/hart-scan-test.png 2>/dev/null
            else
              ${sane-backends}/bin/scanimage --format=png -o /tmp/hart-scan-test.png 2>/dev/null
            fi
            if [ $? -eq 0 ]; then
              echo "Test scan saved to /tmp/hart-scan-test.png"
            else
              echo "Scan failed — check scanner connection"
            fi
            ;;
          help|--help|-h)
            echo "hart-scanner — HART OS Scanner Management"
            echo ""
            echo "  hart-scanner status            Show scanner status"
            echo "  hart-scanner list               List detected scanners"
            echo "  hart-scanner test [device]      Perform a test scan"
            ;;
          *)
            echo "Unknown command: $1 (try: hart-scanner help)"
            exit 1
            ;;
        esac
      '')
    ];

    # ── Network scanning daemon (saned) ──
    systemd.services.saned = lib.mkIf cfg.networkScanning {
      description = "SANE Network Scanner Daemon";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        ExecStart = "${pkgs.sane-backends}/bin/saned -a";
        User = "scanner";
        Group = "scanner";
        Restart = "on-failure";
        RestartSec = 5;
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
      };
    };

    # ── saned access control ──
    environment.etc."sane.d/saned.conf" = lib.mkIf cfg.networkScanning {
      text = lib.concatStringsSep "\n" cfg.allowedClients + "\n";
    };

    # ── Firewall: saned port ──
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.networkScanning [ 6566 ];

    # ── Scanner user/group ──
    users.groups.scanner = {};
    users.users.scanner = lib.mkIf cfg.networkScanning {
      isSystemUser = true;
      group = "scanner";
      description = "SANE network scanner daemon user";
    };
  };
}
