# HART OS — CUPS Print Management
#
# Printer discovery (mDNS/Avahi), driverless printing (IPP Everywhere),
# driver packages (HP, Brother, Gutenprint), print-to-PDF.
#
# CLI: hart-print status|list|test|add <uri> <name>

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.printing;
in
{
  options.hart.printing = {
    enable = lib.mkEnableOption "HART OS print management (CUPS)";

    browsing = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Auto-discover network printers via mDNS/Avahi.";
    };

    defaultDriver = lib.mkOption {
      type = lib.types.str;
      default = "everywhere";
      description = "Default print driver (IPP Everywhere driverless).";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── CUPS daemon ──
    services.printing = {
      enable = true;
      browsing = cfg.browsing;
      drivers = with pkgs; [
        gutenprint          # 700+ printer models
        hplip               # HP printers
        brlaser             # Brother laser printers
      ];
    };

    # ── mDNS printer discovery via Avahi ──
    services.avahi = lib.mkIf cfg.browsing {
      enable = true;
      nssmdns4 = true;
      openFirewall = true;
      publish = {
        enable = true;
        userServices = true;
      };
    };

    # ── Firewall: CUPS web UI ──
    networking.firewall.allowedTCPPorts = [ 631 ];

    # ── Print-to-PDF + CLI tool ──
    environment.systemPackages = [
      (pkgs.cups-pdf-to-pdf or pkgs.cups)
      (pkgs.writeShellScriptBin "hart-print" ''
        case "''${1:-status}" in
          status)
            echo "=== CUPS Print Service ==="
            ${pkgs.cups}/bin/lpstat -r 2>/dev/null || echo "CUPS not running"
            echo ""
            ${pkgs.cups}/bin/lpstat -p -d 2>/dev/null || echo "No printers configured"
            ;;
          list)
            ${pkgs.cups}/bin/lpstat -p -d
            ;;
          test)
            printer="''${2:-$(${pkgs.cups}/bin/lpstat -d | awk '{print $NF}')}"
            echo "Printing test page to $printer..."
            ${pkgs.cups}/bin/lp -d "$printer" /usr/share/cups/data/testprint.ps 2>/dev/null \
              && echo "Test page sent." || echo "Failed — check CUPS web UI at http://localhost:631"
            ;;
          add)
            uri="$2"
            name="$3"
            if [ -z "$uri" ] || [ -z "$name" ]; then
              echo "Usage: hart-print add <uri> <name>"
              echo "Example: hart-print add ipp://192.168.1.100/ipp/print Office-HP"
              exit 1
            fi
            ${pkgs.cups}/bin/lpadmin -p "$name" -E -v "$uri" -m ${cfg.defaultDriver}
            echo "Printer '$name' added."
            ;;
          *)
            echo "Usage: hart-print {status|list|test [printer]|add <uri> <name>}"
            ;;
        esac
      '')
    ];
  };
}
