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
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      after = [ "hart-backend.service" ];
      bindsTo = [ "hart-backend.service" ];
      partOf = [ "hart.target" ];
      # wantedBy hart-backend TOO, not only hart.target: bindsTo takes discovery
      # DOWN when the backend dies, but nothing brought it back UP when the
      # backend AUTO-restarted (Restart=on-failure) — partOf(hart.target) does
      # not propagate a backend auto-restart, so after a real backend crash
      # discovery stayed dead until next boot (VM run 31347690532: hart-edge-boot
      # "critical unit RECOVERS" found hart-discovery inactive after the backend
      # kill+restart cascade). Making the backend WANT discovery means every
      # backend (re)start re-pulls discovery; combined with `after` + `bindsTo`
      # discovery now follows the backend's FULL lifecycle. No cycle: the Want is
      # soft and one-way (backend never blocks on discovery).
      wantedBy = [ "hart.target" "hart-backend.service" ];

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
        ExecStart = "${hartApp.python}/bin/python -c \"from integrations.social.peer_discovery import get_auto_discovery, get_peer_discovery; get_peer_discovery().start(); get_auto_discovery().start(); import time; time.sleep(999999)\"";

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

        # Resource limits — discovery is lightweight.
        #
        # EDGE MemoryHigh 32M -> 48M, MEASURED not estimated (task #19,
        # 2026-08-03). hart-edge-boot's cap check ran on a booted edge node and
        # reported:
        #     hart-discovery.service  peak=32.0M  high=32.0M  max=48.0M
        # i.e. the service's peak sat EXACTLY on its own MemoryHigh. MemoryHigh
        # is a throttle, not a kill, so nothing crashed and restarts=0 — which
        # is why this never showed up as a failure. It just means the cgroup
        # was applying reclaim pressure on essentially every allocation, on the
        # variant with the least CPU to spare for it.
        #
        # 48M is the observed peak + 50%. MemoryMax stays 64M so the hard
        # ceiling still exists and still sits above MemoryHigh — a MemoryHigh
        # equal to MemoryMax would make the throttle meaningless.
        #
        # Worth recording because the estimate was WRONG BY 6x in the other
        # direction: a dev-box import measurement suggested ~188 MB, which does
        # not transfer at all — that box's venv carries transformers/torch and
        # hart-app.nix ships neither. Real environment, real number.
        MemoryMax = if cfg.variant == "edge" then "64M" else "128M";
        MemoryHigh = if cfg.variant == "edge" then "48M" else "96M";
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
