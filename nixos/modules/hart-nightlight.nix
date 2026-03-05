# HART OS — Night Light (Blue Light Filter)
#
# Reduces blue light emission after sunset or on a manual schedule.
# Uses gammastep (Wayland) or redshift (X11) as a systemd user service.
#
# CLI: hart-nightlight status|on|off|temp <kelvin>

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.nightlight;
  tool = if config.hart.base.variant or "desktop" == "desktop"
         then pkgs.gammastep    # Wayland-native
         else pkgs.redshift;    # X11 fallback
  toolName = if tool == pkgs.gammastep then "gammastep" else "redshift";
in
{
  options.hart.nightlight = {
    enable = lib.mkEnableOption "HART OS night light (blue light filter)";

    temperature = lib.mkOption {
      type = lib.types.int;
      default = 4500;
      description = "Night color temperature in Kelvin (1000=warm, 6500=daylight).";
    };

    schedule = {
      mode = lib.mkOption {
        type = lib.types.enum [ "sunset" "manual" "disabled" ];
        default = "sunset";
        description = "Schedule mode: sunset (auto), manual (fixed times), disabled.";
      };

      start = lib.mkOption {
        type = lib.types.str;
        default = "20:00";
        description = "Start time for manual schedule (HH:MM).";
      };

      end = lib.mkOption {
        type = lib.types.str;
        default = "06:00";
        description = "End time for manual schedule (HH:MM).";
      };
    };

    latitude = lib.mkOption {
      type = lib.types.float;
      default = 0.0;
      description = "Latitude for automatic sunset detection.";
    };

    longitude = lib.mkOption {
      type = lib.types.float;
      default = 0.0;
      description = "Longitude for automatic sunset detection.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [
      tool
      (pkgs.writeShellScriptBin "hart-nightlight" ''
        case "''${1:-status}" in
          status)
            echo "=== Night Light ==="
            if ${pkgs.procps}/bin/pgrep -x "${toolName}" >/dev/null 2>&1; then
              echo "Status: ACTIVE"
            else
              echo "Status: inactive"
            fi
            echo "Temperature: ${toString cfg.temperature}K"
            echo "Schedule: ${cfg.schedule.mode}"
            ;;
          on)
            systemctl --user start hart-nightlight 2>/dev/null || \
              ${tool}/bin/${toolName} -O ${toString cfg.temperature} &
            echo "Night light enabled (${toString cfg.temperature}K)"
            ;;
          off)
            systemctl --user stop hart-nightlight 2>/dev/null
            ${pkgs.procps}/bin/pkill -x "${toolName}" 2>/dev/null
            echo "Night light disabled"
            ;;
          temp)
            t="''${2:-${toString cfg.temperature}}"
            ${pkgs.procps}/bin/pkill -x "${toolName}" 2>/dev/null
            ${tool}/bin/${toolName} -O "$t" &
            echo "Temperature set to ''${t}K"
            ;;
          *)
            echo "Usage: hart-nightlight {status|on|off|temp <kelvin>}"
            ;;
        esac
      '')
    ];

    # ── Systemd user service ──
    systemd.user.services.hart-nightlight = {
      description = "HART OS Night Light";
      wantedBy = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      serviceConfig = {
        ExecStart = let
          args = if cfg.schedule.mode == "sunset" && cfg.latitude != 0.0
                 then "-l ${toString cfg.latitude}:${toString cfg.longitude} -t 6500:${toString cfg.temperature}"
                 else if cfg.schedule.mode == "manual"
                 then "-t 6500:${toString cfg.temperature}"
                 else "-O ${toString cfg.temperature}";
        in "${tool}/bin/${toolName} ${args}";
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    # ── Export config for shell API layer ──
    environment.etc."hart/nightlight.json".text = builtins.toJSON {
      enabled = true;
      temperature = cfg.temperature;
      schedule = {
        inherit (cfg.schedule) mode start end;
      };
      latitude = cfg.latitude;
      longitude = cfg.longitude;
    };

  };
}
