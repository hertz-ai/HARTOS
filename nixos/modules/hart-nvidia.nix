{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS NVIDIA Proprietary Driver Support
# ═══════════════════════════════════════════════════════════════
#
# Handles the NVIDIA driver lifecycle:
#   - Auto-detection of NVIDIA hardware
#   - Driver channel selection (production, new-feature, open-kernel)
#   - CUDA toolkit + cuDNN for AI inference
#   - Power management (dynamic boost, persistence mode)
#   - Container toolkit for GPU-accelerated containers
#
# hart-kernel.nix loads kernel modules + sets udev rules.
# This module handles the userspace driver + toolkit stack.

let
  cfg = config.hart;
  nv = config.hart.nvidia;
in
{
  options.hart.nvidia = {

    enable = lib.mkEnableOption "NVIDIA proprietary driver management";

    driverChannel = lib.mkOption {
      type = lib.types.enum [ "production" "new-feature" "open" ];
      default = "production";
      description = ''
        Driver channel:
          production  — Stable, tested (recommended for servers)
          new-feature — Latest features (recommended for desktop)
          open        — Open-source kernel module (Turing+, Ampere+)
      '';
    };

    cuda = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Install CUDA toolkit for GPU compute (llama.cpp, etc.)";
      };
    };

    persistenceMode = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Keep GPU initialized between workloads.
        Eliminates cold-start latency for inference.
      '';
    };

    powerManagement = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable NVIDIA power management (suspend/resume, dynamic boost)";
      };

      dynamicBoost = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Enable Dynamic Boost (laptop GPU+CPU power sharing)";
      };
    };
  };

  config = lib.mkIf (cfg.enable && nv.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Driver selection
    # ─────────────────────────────────────────────────────────
    {
      # Use open kernel module for Turing+ when selected
      hardware.nvidia = {
        open = (nv.driverChannel == "open");
        modesetting.enable = true;
        nvidiaSettings = true;

        # Power management
        powerManagement = {
          enable = nv.powerManagement.enable;
          finegrained = nv.powerManagement.dynamicBoost;
        };

        # Package selection based on channel
        package = let
          nvPkgs = config.boot.kernelPackages.nvidiaPackages;
        in
          if nv.driverChannel == "new-feature" then nvPkgs.beta
          else nvPkgs.stable;
      };

      # OpenGL + Vulkan
      hardware.graphics = {
        enable = true;
        enable32Bit = true;
      };
    }

    # ─────────────────────────────────────────────────────────
    # CUDA toolkit
    # ─────────────────────────────────────────────────────────
    (lib.mkIf nv.cuda.enable {
      environment.systemPackages = with pkgs; [
        cudatoolkit
        cudaPackages.cudnn
      ];

      # CUDA environment for all users
      environment.variables = {
        CUDA_PATH = "${pkgs.cudatoolkit}";
      };
    })

    # ─────────────────────────────────────────────────────────
    # Persistence daemon — keep GPU warm between inferences
    # ─────────────────────────────────────────────────────────
    (lib.mkIf nv.persistenceMode {
      systemd.services.nvidia-persistence = {
        description = "NVIDIA Persistence Daemon";
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          Type = "forking";
          ExecStart = "${config.hardware.nvidia.package.bin}/bin/nvidia-persistenced --user hart --persistence-mode";
          ExecStopPost = "${config.hardware.nvidia.package.bin}/bin/nvidia-persistenced --user hart --no-persistence-mode";
          Restart = "on-failure";
          RestartSec = 5;
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # Monitoring CLI
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-gpu" ''
          #!/usr/bin/env bash
          case "''${1:-status}" in
            status)
              nvidia-smi 2>/dev/null || echo "No NVIDIA GPU detected"
              ;;
            temp)
              nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null || echo "N/A"
              ;;
            power)
              nvidia-smi --query-gpu=power.draw,power.limit --format=csv,noheader 2>/dev/null || echo "N/A"
              ;;
            memory)
              nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "N/A"
              ;;
            help|--help|-h)
              echo "hart-gpu — NVIDIA GPU Management"
              echo ""
              echo "  hart-gpu status   Full nvidia-smi output"
              echo "  hart-gpu temp     GPU temperature"
              echo "  hart-gpu power    Power draw/limit"
              echo "  hart-gpu memory   VRAM usage"
              ;;
            *)
              echo "Unknown command: $1 (try: hart-gpu help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
