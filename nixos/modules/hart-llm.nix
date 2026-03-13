{ config, lib, pkgs, llama-cpp, ... }:

# HART OS Local LLM Module
# llama.cpp server for local inference with GPU support
# Ported from deploy/linux/systemd/hart-llm.service
# Only starts when a model file exists

let
  cfg = config.hart;

  # Use the llama-cpp flake output with CUDA support if available
  llama-server = llama-cpp.packages.${pkgs.system}.default or pkgs.llama-cpp;
in
{
  options.hart.llm = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = cfg.variant != "edge";
      description = "Enable local LLM inference (llama.cpp)";
    };

    modelPath = lib.mkOption {
      type = lib.types.str;
      default = "${cfg.dataDir}/models/default.gguf";
      description = "Path to GGUF model file";
    };

    contextSize = lib.mkOption {
      type = lib.types.int;
      default = 4096;
      description = "Context window size";
    };

    threads = lib.mkOption {
      type = lib.types.int;
      default = 4;
      description = "Number of CPU threads for inference";
    };

    gpuLayers = lib.mkOption {
      type = lib.types.int;
      default = 0;
      description = "Number of layers to offload to GPU (0 = auto-detect at runtime)";
    };
  };

  config = lib.mkIf (cfg.enable && config.hart.llm.enable) {

    # NVIDIA GPU support (declarative — the NixOS way)
    hardware.nvidia = lib.mkIf (builtins.pathExists "/dev/nvidia0") {
      open = true;  # Use open-source kernel modules (Turing+)
    };

    systemd.services.hart-llm = {
      description = "HART OS Local LLM (llama.cpp)";
      documentation = [ "https://github.com/hertz-ai/HARTOS" ];
      after = [ "network.target" "hart-first-boot.service" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];

      unitConfig = {
        # Only start if a model file exists
        ConditionPathExists = config.hart.llm.modelPath;
      };

      environment = {
        HART_LLM_PORT = toString cfg.ports.llm;
      };

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        ExecStart = lib.concatStringsSep " " [
          "${llama-server}/bin/llama-server"
          "--model ${config.hart.llm.modelPath}"
          "--port ${toString cfg.ports.llm}"
          "--ctx-size ${toString config.hart.llm.contextSize}"
          "--threads ${toString config.hart.llm.threads}"
        ];

        EnvironmentFile = lib.mkIf (builtins.pathExists "/etc/hart/hart.env") "/etc/hart/hart.env";

        Restart = "on-failure";
        RestartSec = 10;
        TimeoutStartSec = 60;

        # GPU access
        SupplementaryGroups = [ "video" "render" ];

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "${cfg.dataDir}/models" ];
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

        # Resource limits — LLM is the heaviest service
        MemoryMax = "8G";
        CPUWeight = 150;
        TasksMax = 64;
        IOWeight = 100;
        # Nice value: lower priority than backend
        Nice = 5;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-llm";
      };
    };
  };
}
