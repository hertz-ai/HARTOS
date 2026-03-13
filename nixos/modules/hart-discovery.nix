{ config, lib, pkgs, ... }:

# HART OS Peer Discovery Module
# UDP beacon on port 6780 for zero-config LAN peer discovery
# Ported from deploy/linux/systemd/hart-discovery.service

let
  cfg = config.hart;
  hartApp = config.hart.package;
in
{
  config = lib.mkIf cfg.enable {

    systemd.services.hart-discovery = {
      description = "HART OS Peer Discovery (UDP Beacon)";
      documentation = [ "https://github.com/hevolve-ai/hart" ];
      after = [ "hart-backend.service" ];
      bindsTo = [ "hart-backend.service" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      environment = {
        HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        HART_DISCOVERY_PORT = toString cfg.ports.discovery;
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        WorkingDirectory = hartApp;
        ExecStart = "${hartApp.python}/bin/python -c \"from integrations.social.peer_discovery import AutoDiscovery; d = AutoDiscovery(); d.start(); import time; time.sleep(999999)\"";

        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 10;

        # UDP broadcast capability
        AmbientCapabilities = [ "CAP_NET_BROADCAST" ];

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.dataDir ];
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

        # Resource limits — discovery is lightweight
        MemoryMax = if cfg.variant == "edge" then "48M" else "128M";
        MemoryHigh = if cfg.variant == "edge" then "32M" else "96M";
        CPUWeight = 20;
        TasksMax = 16;
        IOWeight = 20;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-discovery";
      };
    };
  };
}
