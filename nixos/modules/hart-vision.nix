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
        # minicpm_server.py runs as a script (not `-m`), so Python puts the
        # script's own dir on sys.path[0], NOT the app root — `import core` then
        # raises ModuleNotFoundError ("No module named 'core'", the crash that
        # looped hart-vision 88× on the ISO). Put the app root on PYTHONPATH.
        PYTHONPATH = "${hartApp}";
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        WorkingDirectory = hartApp;
        # --log_file MUST be absolute + writable: minicpm_server.py defaults it to
        # the RELATIVE `minicpm_sidecar.log`, which resolves against WorkingDirectory
        # (= hartApp, the RO nix store) → `OSError: Read-only file system` crashed
        # the sidecar on the ISO (real-HW bdd849 journal). cfg.dataDir is already in
        # ReadWritePaths (writable under ProtectSystem=strict), so log there.
        ExecStart = "${hartApp.python}/bin/python integrations/vision/minicpm_server.py --model_dir ${config.hart.vision.modelDir} --port ${toString cfg.ports.vision} --device ${config.hart.vision.device} --log_file ${cfg.dataDir}/minicpm_sidecar.log";

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
