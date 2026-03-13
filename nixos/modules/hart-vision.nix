{ config, lib, pkgs, ... }:

# HART OS Vision Service Module
# MiniCPM sidecar for scene description + embodied AI learning
# Ported from deploy/linux/systemd/hart-vision.service
# Only starts when the MiniCPM model directory exists

let
  cfg = config.hart;
  hartApp = config.hart.package;
in
{
  options.hart.vision = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = false;  # Only enabled on PERFORMANCE+ tiers with model present
      description = "Enable vision service (MiniCPM sidecar)";
    };

    modelDir = lib.mkOption {
      type = lib.types.str;
      default = "${cfg.dataDir}/models/minicpm";
      description = "Path to MiniCPM model directory";
    };

    device = lib.mkOption {
      type = lib.types.str;
      default = "auto";
      description = "Device for vision inference (auto, cuda, cpu)";
    };
  };

  config = lib.mkIf (cfg.enable && config.hart.vision.enable) {

    systemd.services.hart-vision = {
      description = "HART OS Vision Service (MiniCPM)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      after = [ "hart-backend.service" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      unitConfig = {
        ConditionPathIsDirectory = config.hart.vision.modelDir;
      };

      environment = {
        HART_VISION_PORT = toString cfg.ports.vision;
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        WorkingDirectory = hartApp;
        ExecStart = "${hartApp.python}/bin/python integrations/vision/minicpm_server.py --model_dir ${config.hart.vision.modelDir} --port ${toString cfg.ports.vision} --device ${config.hart.vision.device}";

        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 15;
        TimeoutStartSec = 120;

        # GPU access for vision inference
        SupplementaryGroups = [ "video" "render" ];

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [
          cfg.dataDir
          "${cfg.dataDir}/models"
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

        # Resource limits — vision is GPU-bound
        MemoryMax = "4G";
        CPUWeight = 60;
        TasksMax = 32;
        IOWeight = 50;
        Nice = 10;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-vision";
      };
    };
  };
}
