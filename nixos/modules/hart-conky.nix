{ config, lib, pkgs, ... }:

# HART OS Conky Dashboard Module
# Always-visible lightweight node status overlay
# Shows: Node ID, Tier, Peers, Agents, LLM status, CPU/GPU, goals

let
  cfg = config.hart;
  conkyCfg = config.hart.conky;

  # Conky with Lua + Cairo support for styled dashboard
  conkyPkg = pkgs.conky.override {
    lua5_4 = pkgs.lua5_4;
    luaSupport = true;
    curlSupport = true;
  };

  # Deploy our custom Conky config, Lua helper, and theme presets
  conkyConfigDir = pkgs.runCommand "hart-conky-config" {} ''
    mkdir -p $out/share/hart
    cp ${../assets/hart.conkyrc} $out/share/hart/hart.conkyrc
    cp ${../assets/hart-conky.lua} $out/share/hart/hart-conky.lua
    mkdir -p $out/share/hart/conky-themes
    for f in ${../assets/conky-themes}/*.json; do
      cp "$f" $out/share/hart/conky-themes/
    done
  '';
in
{
  # ─── Options ──────────────────────────────────────────────
  options.hart.conky = {
    enable = lib.mkEnableOption "HART OS Conky dashboard overlay";

    position = lib.mkOption {
      type = lib.types.enum [ "top_right" "top_left" "bottom_right" "bottom_left" ];
      default = "top_right";
      description = "Screen position for the Conky overlay";
    };

    updateInterval = lib.mkOption {
      type = lib.types.int;
      default = 5;
      description = "Update interval in seconds";
    };

    theme = lib.mkOption {
      type = lib.types.str;
      default = "hart-default";
      description = "Default theme preset ID (from conky-themes/*.json)";
    };
  };

  # ─── Configuration ────────────────────────────────────────
  config = lib.mkIf (cfg.enable && conkyCfg.enable) {

    # Install Conky + config files
    environment.systemPackages = [
      conkyPkg
      conkyConfigDir
      pkgs.lua54Packages.luasocket  # socket.http for Lua API calls
      pkgs.curl   # For Lua HTTP calls to backend API
      pkgs.jq     # Fallback JSON parsing
    ];

    # Systemd user service: auto-start Conky on desktop login
    systemd.user.services.hart-conky = {
      description = "HART OS Conky Dashboard";
      after = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];

      serviceConfig = {
        ExecStartPre = "${pkgs.coreutils}/bin/sleep 3";  # Wait for desktop to settle
        ExecStart = "${conkyPkg}/bin/conky -c ${conkyConfigDir}/share/hart/hart.conkyrc";
        Restart = "on-failure";
        RestartSec = 5;
      };

      # Resource limits — Conky should be invisible on the scheduler
      serviceConfig.MemoryMax = "48M";
      serviceConfig.CPUWeight = 5;
      serviceConfig.Nice = 19;
      serviceConfig.IOWeight = 5;

      environment = {
        DISPLAY = ":0";
        HARTOS_BACKEND_PORT = toString cfg.ports.backend;
        HART_DATA_DIR = toString cfg.dataDir;
        HEVOLVE_DATA_DIR = toString cfg.dataDir;
        HART_THEME_DIR = "${conkyConfigDir}/share/hart/conky-themes";
        HART_CONKY_POSITION = conkyCfg.position;
        HART_CONKY_INTERVAL = toString conkyCfg.updateInterval;
        # Lua path: ensure luasocket is findable by Conky's embedded Lua
        LUA_PATH = "${pkgs.lua54Packages.luasocket}/share/lua/5.4/?.lua;${pkgs.lua54Packages.luasocket}/share/lua/5.4/?/init.lua;;";
        LUA_CPATH = "${pkgs.lua54Packages.luasocket}/lib/lua/5.4/?.so;;";
      };
    };

    # Write initial active_theme.json on first boot if not present
    system.activationScripts.hart-conky-theme = ''
      THEME_FILE="${cfg.dataDir}/active_theme.json"
      if [ ! -f "$THEME_FILE" ]; then
        PRESET="${conkyConfigDir}/share/hart/conky-themes/${conkyCfg.theme}.json"
        if [ -f "$PRESET" ]; then
          mkdir -p "$(dirname "$THEME_FILE")"
          cp "$PRESET" "$THEME_FILE"
          chown hart:hart "$THEME_FILE" 2>/dev/null || true
        fi
      fi
    '';
  };
}
