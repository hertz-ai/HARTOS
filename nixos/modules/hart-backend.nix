{ config, lib, pkgs, ... }:

# HART OS Backend Module
# Flask/Waitress API server on port 6777
# Ported from deploy/linux/systemd/hart-backend.service

let
  cfg = config.hart;
  hartApp = config.hart.package;
in
{
  config = lib.mkIf cfg.enable {

    systemd.services.hart-backend = {
      description = "HART OS Backend (Flask/Waitress)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      after = [ "network-online.target" "hart-first-boot.service" ];
      wants = [ "network-online.target" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      environment = {
        # Recipe/prompts data must land in the service's WRITABLE StateDirectory
        # (cfg.dataDir), not the /nix/store package dir (read-only) nor the
        # /etc/hartos-release default /var/lib/hartos (outside this sandbox ->
        # EROFS). This is get_data_dir()'s priority-2 signal, so get_recipe_
        # prompts_dir() -> cfg.dataDir/data/prompts. Fixes the boot crash
        # OSError [Errno 30] Read-only file system: '.../prompts'.
        HARTOS_DATA_DIR = cfg.dataDir;
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        HARTOS_BACKEND_PORT = toString cfg.ports.backend;
        HART_DISCOVERY_PORT = toString cfg.ports.discovery;
        HART_LLM_PORT = toString cfg.ports.llm;
        HART_VISION_PORT = toString cfg.ports.vision;
        HART_VERSION = cfg.version;
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        WorkingDirectory = hartApp;
        # Thread count scales by variant: edge=4, server=50, desktop=24
        ExecStart = let
          threads = if cfg.variant == "edge" then "4"
                    else if cfg.variant == "desktop" then "24"
                    else "50";
        in "${hartApp.python}/bin/python -m waitress --port=${toString cfg.ports.backend} --threads=${threads} hart_intelligence_entry:app";

        # Environment file for API keys (optional, user-provided)
        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 5;
        # No WatchdogSec: waitress never sends sd_notify(WATCHDOG=1), so a watchdog
        # timer would SIGABRT the backend every 120s once it is actually serving.
        # Restart=on-failure still covers real crashes.
        TimeoutStartSec = 30;
        TimeoutStopSec = 15;

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [
          cfg.dataDir
          cfg.logDir
          "${cfg.dataDir}/agent_data"
        ];
        PrivateTmp = true;
        ProtectClock = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        SystemCallFilter = [ "@system-service" ];
        MemoryDenyWriteExecute = false;
        LockPersonality = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;

        # Resource limits — scale by variant
        MemoryMax = if cfg.variant == "edge" then "384M"
                    else if cfg.variant == "desktop" then "1G"
                    else "2G";
        MemoryHigh = if cfg.variant == "edge" then "256M"
                     else if cfg.variant == "desktop" then "768M"
                     else "1536M";
        CPUWeight = if cfg.variant == "edge" then 50 else 100;
        TasksMax = if cfg.variant == "edge" then 32 else 256;
        IOWeight = if cfg.variant == "edge" then 50 else 100;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-backend";
      };
    };
  };
}
