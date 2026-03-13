{ config, lib, pkgs, ... }:

# HART OS Agent Daemon Module
# Goal engine: dispatches goals, manages ledger, runs tools
# Ported from deploy/linux/systemd/hart-agent-daemon.service
# Only enabled on STANDARD tier and above

let
  cfg = config.hart;
  hartApp = config.hart.package;
in
{
  options.hart.agent = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = cfg.variant != "edge";  # Disabled on edge (observer only)
      description = "Enable the agent daemon (goal engine)";
    };
  };

  config = lib.mkIf (cfg.enable && config.hart.agent.enable) {

    systemd.services.hart-agent-daemon = {
      description = "HART OS Agent Daemon (Goal Engine)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      after = [ "hart-backend.service" ];
      bindsTo = [ "hart-backend.service" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      environment = {
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        WorkingDirectory = hartApp;
        ExecStart = "${hartApp.python}/bin/python -c \"from integrations.agent_engine.agent_daemon import AgentDaemon; d = AgentDaemon(); d.run_forever()\"";

        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 30;
        TimeoutStartSec = 30;

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
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_VSOCK" ];
        SystemCallFilter = [ "@system-service" ];
        MemoryDenyWriteExecute = false;
        LockPersonality = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;

        # Resource limits — scale by variant
        MemoryMax = if cfg.variant == "edge" then "128M"
                    else if cfg.variant == "desktop" then "512M"
                    else "1G";
        MemoryHigh = if cfg.variant == "edge" then "96M"
                     else if cfg.variant == "desktop" then "384M"
                     else "768M";
        CPUWeight = if cfg.variant == "edge" then 30 else 80;
        TasksMax = if cfg.variant == "edge" then 16 else 128;
        IOWeight = if cfg.variant == "edge" then 30 else 80;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-agent-daemon";
      };
    };
  };
}
