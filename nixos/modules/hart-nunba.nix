{ config, lib, pkgs, hartSrc, ... }:

# HART OS Nunba Module
# Headless OS component — Flask API daemon serving management APIs.
# React SPA is rendered inside LiquidUI glass panels (no separate PyWebView window).
# Provides: chat, communities, agent goals, settings, intelligence API

let
  cfg = config.hart;
  nunbaCfg = config.hart.nunba;

  nunbaPackage = pkgs.callPackage ../packages/nunba.nix { inherit hartSrc; };
in
{
  # ─── Options ──────────────────────────────────────────────
  options.hart.nunba = {
    enable = lib.mkEnableOption "Nunba headless management daemon";

    port = lib.mkOption {
      type = lib.types.port;
      default = 5000;
      description = "Nunba Flask API server port";
    };

    autostart = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Auto-start Nunba Flask server on boot";
    };

    addToFavorites = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Add Nunba shortcut to GNOME dock (not needed — LiquidUI IS the shell)";
    };
  };

  # ─── Configuration ────────────────────────────────────────
  config = lib.mkIf (cfg.enable && nunbaCfg.enable) {

    # Install Nunba package (no PyWebView GUI deps — LiquidUI handles rendering)
    environment.systemPackages = [
      nunbaPackage
    ];

    # Systemd user service: Nunba Flask API (headless, no GUI)
    systemd.user.services.hart-nunba = lib.mkIf nunbaCfg.autostart {
      description = "Nunba Flask API Daemon";
      after = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];

      serviceConfig = {
        ExecStart = "${nunbaPackage}/bin/nunba --server-only";
        Restart = "on-failure";
        RestartSec = 3;
      };

      environment = {
        NUNBA_PORT = toString nunbaCfg.port;
        NUNBA_BACKEND_URL = "http://localhost:${toString cfg.ports.backend}";
        PYTHONDONTWRITEBYTECODE = "1";
      };
    };

    # GNOME/Phosh dock favorites (LiquidUI is the primary interface now)
    programs.dconf = lib.mkIf nunbaCfg.addToFavorites {
      enable = true;
      profiles.user.databases = [{
        settings = {
          "org/gnome/shell" = {
            favorite-apps = [
              "firefox.desktop"
              "org.gnome.Terminal.desktop"
              "org.gnome.Nautilus.desktop"
            ];
          };
        };
      }];
    };
  };
}
