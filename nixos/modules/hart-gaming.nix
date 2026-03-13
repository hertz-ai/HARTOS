# HART OS — Gaming Optimizations
#
# Low-latency kernel tuning, network optimization, PipeWire low-latency audio,
# hardware game capture (OBS, NVENC, VAAPI). Complements hart-subsystems.nix
# which provides game launchers (Steam, Proton, DXVK, Wine, Gamemode).
#
# CLI: hart-gaming status|benchmark|fps|anticheat|help

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.gaming;
in
{
  options.hart.gaming = {
    enable = lib.mkEnableOption "HART OS gaming optimizations";

    kernel = {
      preemptRT = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Use PREEMPT_RT kernel (lowest latency, trades throughput).";
      };
      cpuIsolation = lib.mkOption {
        type = lib.types.listOf lib.types.int;
        default = [];
        description = "CPU cores to isolate for game threads.";
      };
    };

    network = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Low-latency network tuning for multiplayer.";
      };
    };

    capture = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Hardware game capture (OBS, NVENC, VAAPI).";
      };
    };

    audio = {
      lowLatency = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "PipeWire low-latency (256 samples / ~5ms at 48kHz).";
      };
      sampleRate = lib.mkOption {
        type = lib.types.int;
        default = 48000;
        description = "Audio sample rate.";
      };
      bufferSize = lib.mkOption {
        type = lib.types.int;
        default = 256;
        description = "Audio buffer size in samples.";
      };
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    # Kernel tuning (always when gaming enabled)
    {
      boot.kernel.sysctl = {
        "kernel.sched_min_granularity_ns" = 500000;
        "kernel.sched_wakeup_granularity_ns" = 250000;
        "kernel.sched_latency_ns" = 2000000;
        "kernel.sched_migration_cost_ns" = 500000;
        "kernel.nmi_watchdog" = 0;
        "vm.compaction_proactiveness" = 0;
      };
    }

    # CPU isolation
    (lib.mkIf (cfg.kernel.cpuIsolation != []) {
      boot.kernelParams = [
        "isolcpus=${lib.concatMapStringsSep "," toString cfg.kernel.cpuIsolation}"
        "nohz_full=${lib.concatMapStringsSep "," toString cfg.kernel.cpuIsolation}"
      ];
    })

    # Network tuning
    (lib.mkIf cfg.network.enable {
      boot.kernelModules = [ "tcp_bbr" ];
      boot.kernel.sysctl = {
        "net.ipv4.tcp_low_latency" = 1;
        "net.core.rmem_default" = 1048576;
        "net.core.wmem_default" = 1048576;
        "net.core.rmem_max" = 26214400;
        "net.core.wmem_max" = 26214400;
        "net.core.default_qdisc" = "fq";
        "net.ipv4.tcp_congestion_control" = "bbr";
        "net.core.netdev_max_backlog" = 16384;
        "net.ipv4.tcp_fin_timeout" = 10;
      };
    })

    # Game capture
    (lib.mkIf cfg.capture.enable {
      environment.systemPackages = with pkgs; [
        obs-studio
        ffmpeg-full
      ];
      boot.kernelModules = [ "v4l2loopback" ];
    })

    # Low-latency audio
    (lib.mkIf cfg.audio.lowLatency {
      environment.etc."pipewire/pipewire.conf.d/99-hart-gaming.conf".text = builtins.toJSON {
        "context.properties" = {
          "default.clock.rate" = cfg.audio.sampleRate;
          "default.clock.quantum" = cfg.audio.bufferSize;
          "default.clock.min-quantum" = cfg.audio.bufferSize;
        };
      };
      security.rtkit.enable = true;
      security.pam.loginLimits = [
        { domain = "@audio"; item = "memlock"; type = "-"; value = "unlimited"; }
        { domain = "@audio"; item = "rtprio"; type = "-"; value = "95"; }
      ];
    })

    # CLI tool + benchmark packages
    {
      environment.systemPackages = with pkgs; [
        vulkan-tools
        glxinfo
        (writeShellScriptBin "hart-gaming" ''
          case "''${1:-status}" in
            status)
              echo "=== HART OS Gaming Status ==="
              echo "Network tuning: ${if cfg.network.enable then "enabled" else "disabled"}"
              echo "Audio latency: ${if cfg.audio.lowLatency then "${toString cfg.audio.bufferSize} samples" else "default"}"
              echo "Game capture: ${if cfg.capture.enable then "enabled" else "disabled"}"
              echo ""
              nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu --format=csv,noheader 2>/dev/null || echo "No NVIDIA GPU info"
              ;;
            benchmark)
              echo "=== Quick GL/Vulkan Benchmark ==="
              glxgears 2>/dev/null &
              sleep 5 && kill %1 2>/dev/null
              ;;
            fps)
              echo "Use MangoHUD overlay:"
              echo "  mangohud %command%     (Steam launch options)"
              echo "  mangohud ./game        (CLI)"
              ;;
            anticheat)
              echo "=== Anti-Cheat Compatibility ==="
              echo "EAC:      Supported via Proton (game must opt in)"
              echo "BattlEye: Supported via Proton (game must opt in)"
              echo "Vanguard: NOT supported (requires Windows kernel)"
              echo "Check: https://areweanticheatyet.com"
              ;;
            help|--help|-h)
              echo "hart-gaming {status|benchmark|fps|anticheat|help}"
              ;;
            *) echo "Unknown: $1 (try: hart-gaming help)"; exit 1 ;;
          esac
        '')
      ];
    }
  ]);
}
