{ config, lib, pkgs, ... }:

# HART OS Local LLM Module
# llama.cpp server for local inference with GPU support
# Ported from deploy/linux/systemd/hart-llm.service
# Only starts when a model file exists

let
  cfg = config.hart;

  # GPU-accelerated llama.cpp. Vulkan is the UNIVERSAL GPU backend -- Intel iGPU (mesa
  # ANV), AMD (RADV), NVIDIA (its Vulkan ICD) -- so ONE build offloads on any GPU and
  # cleanly falls back to CPU. The old flake `.default` was CPU-only, so EVERY layer ran
  # on the CPU = the "assistant keeps thinking" slowness. The Vulkan RUNTIME is already
  # present (desktop.nix `hardware.graphics.enable` -> mesa ICDs in /run/opengl-driver).
  # This is the NixOS analogue of the Nunba companion's "CPU prebundled, GPU when present"
  # bootstrap: one universal binary, GPU-or-CPU decided at launch, no runtime download.
  llama-server = pkgs.llama-cpp.override { vulkanSupport = true; };

  # GPU-aware launcher. A Vulkan-built llama-server with --n-gpu-layers>0 but NO Vulkan
  # device ERRORS out at load (and would crash-loop under Restart=on-failure), so gate
  # -ngl on a real render node + a Vulkan ICD actually being present; otherwise pure-CPU.
  llamaLauncher = pkgs.writeShellScriptBin "hart-llm-server" ''
    set -eu
    # CPU-thread budget. 0 = AUTO = leave one core for the rest of the OS (nproc - 1, min
    # 1) so CPU-FALLBACK inference never saturates every core and starves the interactive
    # desktop/shell. (The systemd CPUWeight + Nice below keep it a background citizen on the
    # SHARED cores too -- the desktop always wins under contention, spare CPU is still used
    # when it is idle.)
    THREADS="${toString config.hart.llm.threads}"
    if [ "$THREADS" -le 0 ]; then
      THREADS=$(( $(${pkgs.coreutils}/bin/nproc) - 1 ))
      [ "$THREADS" -lt 1 ] && THREADS=1
    fi
    NGL=""
    if [ -e /dev/dri/renderD128 ] && ls /run/opengl-driver/share/vulkan/icd.d/*.json >/dev/null 2>&1; then
      NGL="--n-gpu-layers ${toString config.hart.llm.gpuLayers}"
      echo "hart-llm: Vulkan GPU present -> offloading ${toString config.hart.llm.gpuLayers} layers ($THREADS CPU threads)" >&2
    else
      echo "hart-llm: no Vulkan GPU -> CPU inference ($THREADS threads, one core left for the OS)" >&2
    fi
    exec ${llama-server}/bin/llama-server \
      --model "${config.hart.llm.modelPath}" \
      --port "${toString cfg.ports.llm}" \
      --ctx-size "${toString config.hart.llm.contextSize}" \
      --threads "$THREADS" \
      $NGL
  '';
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
      default = 0;
      description =
        "CPU threads for inference. 0 = AUTO: leave one core for the rest of the OS "
        + "(nproc - 1, min 1) so CPU-fallback inference never starves the interactive "
        + "desktop/shell. Set a positive value to pin it.";
    };

    gpuLayers = lib.mkOption {
      type = lib.types.int;
      default = 999;
      description =
        "Layers to offload to the (Vulkan) GPU when one is present. 999 = offload ALL "
        + "(llama.cpp clamps to the model's real layer count). The launcher only passes "
        + "this when a Vulkan device actually exists, else it runs pure-CPU.";
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
        # The GPU-aware launcher (see `llamaLauncher` in the `let` above): offloads to the
        # Vulkan GPU when one is present, pure-CPU otherwise. Replaces the old bare
        # llama-server call that passed NO --n-gpu-layers, so every layer ran on the CPU.
        ExecStart = "${llamaLauncher}/bin/hart-llm-server";

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

        # Resource limits. The LLM is the heaviest service, but it must NOT starve the
        # interactive desktop/shell when it falls back to CPU-only inference. So it is a
        # deliberate BACKGROUND citizen: CPUWeight 50 (BELOW the default 100 the UI services
        # run at, so they win the shared cores under contention) + Nice 10, paired with the
        # launcher's "leave one core for the OS" thread budget. Spare CPU is still used when
        # the desktop is idle; the desktop just always wins when it needs it. (Was
        # CPUWeight 150 -- ABOVE the UI -- which would let CPU-fallback inference stall the
        # whole desktop, the very thing the steward flagged.)
        MemoryMax = "8G";
        CPUWeight = 50;
        TasksMax = 64;
        IOWeight = 50;
        Nice = 10;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hart-llm";
      };
    };
  };
}
