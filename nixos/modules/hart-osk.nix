# HART OS — On-Screen Keyboard
#
# Virtual keyboard for touch devices (PinePhone, tablets, convertibles).
# Uses squeekboard (Wayland-native) or onboard (X11 fallback).
# Critical for phone variant — no hardware keyboard available.
#
# CLI: hart-osk {status|toggle|help}

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.osk;
in
{
  options.hart.osk = {
    enable = lib.mkEnableOption "HART OS on-screen keyboard";

    backend = lib.mkOption {
      type = lib.types.enum [ "squeekboard" "onboard" "auto" ];
      default = "auto";
      description = ''
        On-screen keyboard backend.
        "auto" selects squeekboard on Wayland, onboard on X11.
      '';
    };

    autoShow = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Automatically show keyboard when text input is focused.";
    };

    hapticFeedback = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable haptic (vibration) feedback on keypress.";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    # Squeekboard (Wayland — default for phone/tablet)
    (lib.mkIf (cfg.backend == "squeekboard" || cfg.backend == "auto") {
      environment.systemPackages = [ pkgs.squeekboard ];

      # Auto-start with GNOME/Phosh session
      systemd.user.services.squeekboard = {
        description = "HART OS On-Screen Keyboard (squeekboard)";
        partOf = [ "graphical-session.target" ];
        wantedBy = [ "graphical-session.target" ];
        serviceConfig = {
          ExecStart = "${pkgs.squeekboard}/bin/squeekboard";
          Restart = "on-failure";
          RestartSec = 3;
        };
        environment = {
          SQUEEKBOARD_HAPTIC = if cfg.hapticFeedback then "1" else "0";
        };
      };
    })

    # Onboard (X11 fallback)
    (lib.mkIf (cfg.backend == "onboard") {
      environment.systemPackages = with pkgs; [ onboard ];
    })

    # CLI tool
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-osk" ''
          case "''${1:-status}" in
            status)
              echo "=== HART OS On-Screen Keyboard ==="
              echo "Backend: ${cfg.backend}"
              echo "Auto-show: ${if cfg.autoShow then "enabled" else "disabled"}"
              echo "Haptic: ${if cfg.hapticFeedback then "enabled" else "disabled"}"
              systemctl --user is-active squeekboard 2>/dev/null && echo "Status: running" || echo "Status: stopped"
              ;;
            toggle)
              if systemctl --user is-active squeekboard 2>/dev/null; then
                systemctl --user stop squeekboard
                echo "Keyboard hidden"
              else
                systemctl --user start squeekboard
                echo "Keyboard shown"
              fi
              ;;
            help|--help|-h)
              echo "hart-osk {status|toggle|help}"
              ;;
            *) echo "Unknown: $1 (try: hart-osk help)"; exit 1 ;;
          esac
        '')
      ];
    }
  ]);
}
