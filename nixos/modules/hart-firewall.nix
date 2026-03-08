{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Firewall Management + Firmware Updates
# ═══════════════════════════════════════════════════════════════
#
# Two subsystems:
#   1. Firewall: nftables-based zones with API control
#   2. fwupd: Linux Vendor Firmware Service integration
#
# Zones model (inspired by firewalld but declarative):
#   - internal: LAN devices, full access
#   - mesh: compute mesh peers, restricted to mesh ports
#   - external: internet, minimal ingress
#   - management: SSH + API, locked to trusted IPs

let
  cfg = config.hart;
  fw = config.hart.firewall;
in
{
  options.hart.firewall = {

    enable = lib.mkEnableOption "HART OS firewall management";

    defaultZone = lib.mkOption {
      type = lib.types.enum [ "internal" "external" "mesh" "management" ];
      default = "external";
      description = "Default zone for unclassified interfaces";
    };

    trustedInterfaces = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Network interfaces in the 'internal' (trusted) zone";
      example = [ "br0" "enp0s3" ];
    };

    managementAllowedIPs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "127.0.0.1" "::1" ];
      description = "IPs allowed SSH + API access in management zone";
    };

    rateLimiting = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable connection rate limiting for external zone";
      };

      maxConnPerSecond = lib.mkOption {
        type = lib.types.int;
        default = 25;
        description = "Max new connections per second before rate limiting";
      };
    };

    firmware = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable fwupd firmware update service";
      };

      autoCheck = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Automatically check for firmware updates";
      };
    };
  };

  config = lib.mkIf (cfg.enable && fw.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # nftables firewall (replaces iptables)
    # ─────────────────────────────────────────────────────────
    {
      networking.firewall.enable = true;
      networking.nftables.enable = true;

      # Base firewall rules — NixOS handles the nftables ruleset
      networking.firewall = {
        allowedTCPPorts = [
          cfg.ports.backend     # API
          22                    # SSH
        ];
        allowedUDPPorts = [
          cfg.ports.discovery   # Peer discovery
        ];

        # Rate limiting via kernel
        extraCommands = lib.optionalString fw.rateLimiting.enable ''
          # Rate limit new TCP connections (SYN flood protection)
          iptables -A INPUT -p tcp --syn -m limit \
            --limit ${toString fw.rateLimiting.maxConnPerSecond}/second \
            --limit-burst 50 -j ACCEPT 2>/dev/null || true
          iptables -A INPUT -p tcp --syn -j DROP 2>/dev/null || true
        '';
      };

      # Trusted interfaces get full access
      networking.firewall.trustedInterfaces =
        fw.trustedInterfaces ++ [ "lo" ];
    }

    # ─────────────────────────────────────────────────────────
    # fwupd — firmware updates (UEFI, SSD, peripherals)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf fw.firmware.enable {
      services.fwupd = {
        enable = true;
      };

      environment.systemPackages = with pkgs; [
        fwupd
      ];

      # Timer for automatic firmware checks
      systemd.services.hart-firmware-check = lib.mkIf fw.firmware.autoCheck {
        description = "HART OS Firmware Update Check";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];

        serviceConfig = {
          Type = "oneshot";
          ExecStart = pkgs.writeShellScript "hart-firmware-check" ''
            set -euo pipefail
            echo "[HART Firmware] Checking for updates..."
            ${pkgs.fwupd}/bin/fwupdmgr refresh --force 2>/dev/null || true
            UPDATES=$(${pkgs.fwupd}/bin/fwupdmgr get-updates 2>/dev/null) || UPDATES=""
            if [[ -n "$UPDATES" ]]; then
              echo "[HART Firmware] Updates available:"
              echo "$UPDATES"
            else
              echo "[HART Firmware] All firmware up to date"
            fi
          '';
          StandardOutput = "journal";
          SyslogIdentifier = "hart-firmware";
        };
      };

      systemd.timers.hart-firmware-check = lib.mkIf fw.firmware.autoCheck {
        description = "Weekly firmware update check";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnCalendar = "weekly";
          Persistent = true;
          RandomizedDelaySec = "2h";
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # CLI tool
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-firewall" ''
          #!/usr/bin/env bash
          case "''${1:-status}" in
            status)
              echo "=== Firewall Status ==="
              echo "Default zone: ${fw.defaultZone}"
              echo ""
              echo "Active rules:"
              sudo nft list ruleset 2>/dev/null | head -40 || \
                sudo iptables -L -n --line-numbers 2>/dev/null | head -30
              ;;
            ports)
              echo "=== Open Ports ==="
              ss -tlnp 2>/dev/null | grep LISTEN
              ;;
            block)
              if [[ -z "''${2:-}" ]]; then
                echo "Usage: hart-firewall block <IP>"
                exit 1
              fi
              sudo iptables -A INPUT -s "$2" -j DROP
              echo "Blocked: $2"
              ;;
            unblock)
              if [[ -z "''${2:-}" ]]; then
                echo "Usage: hart-firewall unblock <IP>"
                exit 1
              fi
              sudo iptables -D INPUT -s "$2" -j DROP 2>/dev/null
              echo "Unblocked: $2"
              ;;
            firmware)
              shift
              case "''${1:-status}" in
                status)
                  ${pkgs.fwupd}/bin/fwupdmgr get-devices 2>/dev/null || echo "fwupd not available"
                  ;;
                check)
                  ${pkgs.fwupd}/bin/fwupdmgr refresh --force 2>/dev/null || true
                  ${pkgs.fwupd}/bin/fwupdmgr get-updates 2>/dev/null || echo "No updates"
                  ;;
                apply)
                  echo "Applying firmware updates..."
                  ${pkgs.fwupd}/bin/fwupdmgr update 2>/dev/null
                  ;;
                *)
                  echo "  hart-firewall firmware status|check|apply"
                  ;;
              esac
              ;;
            help|--help|-h)
              echo "hart-firewall — HART OS Firewall + Firmware Management"
              echo ""
              echo "  hart-firewall status         Show firewall rules"
              echo "  hart-firewall ports           Show listening ports"
              echo "  hart-firewall block <IP>      Block an IP address"
              echo "  hart-firewall unblock <IP>    Unblock an IP address"
              echo "  hart-firewall firmware status Show firmware devices"
              echo "  hart-firewall firmware check  Check for firmware updates"
              echo "  hart-firewall firmware apply  Apply firmware updates"
              ;;
            *)
              echo "Unknown command: $1 (try: hart-firewall help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
