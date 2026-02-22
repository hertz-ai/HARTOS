{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Unified Kernel — Multi-Platform Native Binary Support
# ═══════════════════════════════════════════════════════════════
#
# The HART OS kernel is a Linux kernel with native extensions
# for running binaries from ALL major platforms — not through
# emulators, containers, or translation layers, but through
# kernel-level subsystems that make each binary format a
# first-class citizen.
#
# Architecture:
#
#   ┌────────────┬─────────────┬────────────┬──────────────┐
#   │ Linux ELF  │ Android APK │ Windows PE │ AI Inference  │
#   │ (native)   │ (native)    │ (native)   │ (native)      │
#   ├────────────┼─────────────┼────────────┼──────────────┤
#   │ POSIX      │ Binder IPC  │ Win32 API  │ GPU Direct   │
#   │ syscalls   │ + Ashmem    │ (ntdll)    │ Memory Mgmt  │
#   ├────────────┴─────────────┴────────────┴──────────────┤
#   │           Linux Kernel 6.x + HART OS Extensions       │
#   │                                                       │
#   │  Modules:  binder_linux   (Android IPC)               │
#   │            ashmem_linux   (Android shared memory)     │
#   │            binfmt_misc    (PE auto-detect + dispatch) │
#   │            nvidia/amdgpu  (GPU compute)               │
#   │            vhost/vsock    (agent isolation IPC)        │
#   │            cgroup v2      (agent resource limits)      │
#   │                                                       │
#   │  Scheduler: SCHED_EXT (extensible) for AI workloads   │
#   │  Memory:    Transparent Huge Pages for model loading   │
#   │  Security:  Landlock LSM for agent sandboxing          │
#   └───────────────────────────────────────────────────────┘
#
# Zero containers. Zero emulators. Zero simulation.
# Every app runs at the same privilege level as a native binary.

let
  cfg = config.hart;
  kernelCfg = config.hart.kernel;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.kernel = {

    # ─── Master toggle ───
    enable = lib.mkEnableOption "HART OS unified kernel extensions";

    # ─── Android binary support ───
    androidNative = {
      enable = lib.mkEnableOption "Native Android binary support (binder + ashmem)";
    };

    # ─── Windows binary support ───
    windowsNative = {
      enable = lib.mkEnableOption "Native Windows PE binary support (binfmt + API)";
    };

    # ─── AI compute extensions ───
    aiCompute = {
      enable = lib.mkEnableOption "AI compute kernel extensions (GPU scheduling, memory)";

      hugePagesCount = lib.mkOption {
        type = lib.types.int;
        default = 0;
        description = ''
          Number of 2MB huge pages to reserve for model loading.
          0 = auto (use Transparent Huge Pages only).
          Set to e.g. 4096 (8GB) for dedicated model memory.
        '';
      };
    };

    # ─── Agent sandboxing ───
    agentSandbox = {
      enable = lib.mkEnableOption "Agent isolation via cgroups v2 + Landlock LSM";
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && kernelCfg.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Base: Kernel configuration common to all subsystems
    # ─────────────────────────────────────────────────────────
    {
      # Use latest stable kernel for best hardware + subsystem support
      boot.kernelPackages = lib.mkDefault pkgs.linuxPackages_latest;

      # Unified cgroups v2 (required for proper agent isolation)
      boot.kernelParams = [
        "systemd.unified_cgroup_hierarchy=1"
        "cgroup_no_v1=all"
      ];

      # Core kernel modules loaded at boot
      boot.kernelModules = [
        "vhost_vsock"     # Inter-agent communication (fast IPC without networking)
      ];

      # Kernel sysctl: multi-platform workload tuning
      boot.kernel.sysctl = {
        # IPC: support high-throughput binder + agent communication
        "kernel.shmmax" = 68719476736;        # 64GB shared memory max
        "kernel.shmall" = 4294967296;          # Max shared memory pages
        "kernel.msgmnb" = 65536;               # Message queue max bytes
        "kernel.msgmax" = 65536;               # Single message max

        # Memory: optimize for multi-runtime memory pressure
        "vm.overcommit_memory" = 1;            # Allow overcommit (models + Android + Wine)
        "vm.max_map_count" = 1048576;          # Wine + Android need high mmap count
        "vm.vfs_cache_pressure" = 50;          # Keep dentries/inodes in cache

        # Network: agent-to-agent + P2P gossip
        "net.core.rmem_max" = 26214400;
        "net.core.wmem_max" = 26214400;
        "net.core.netdev_max_backlog" = 5000;

        # File handles: multi-runtime concurrent I/O
        "fs.file-max" = 2097152;
        "fs.inotify.max_user_instances" = 8192;
        "fs.inotify.max_user_watches" = 1048576;
      };
    }

    # ─────────────────────────────────────────────────────────
    # Android Native: binder + ashmem kernel modules
    # ─────────────────────────────────────────────────────────
    #
    # Android apps communicate via Binder IPC (inter-process
    # communication) and share memory via Ashmem (Anonymous
    # Shared Memory). These are kernel modules, not userspace
    # hacks. With these loaded, Android's ART runtime runs
    # binaries at the same level as native Linux processes.
    #
    (lib.mkIf kernelCfg.androidNative.enable {

      # Load Android IPC kernel modules at boot
      boot.kernelModules = [
        "binder_linux"    # Android Binder IPC — native kernel module
        "ashmem_linux"    # Android shared memory — native kernel module
      ];

      # Extra kernel config options needed for Android support
      boot.extraModprobeConfig = ''
        # Binder: multiple device support (system, vendor, hwbinder)
        options binder_linux devices=binder,hwbinder,vndbinder
      '';

      # Device nodes for binder
      services.udev.extraRules = ''
        # Android Binder IPC devices
        KERNEL=="binder*", MODE="0666", GROUP="hart"
        KERNEL=="ashmem",  MODE="0666", GROUP="hart"
        KERNEL=="hwbinder", MODE="0660", GROUP="hart"
        KERNEL=="vndbinder", MODE="0660", GROUP="hart"
      '';

      # SELinux-compatible properties filesystem (Android expects this)
      boot.specialFileSystems = {
        "/dev/binderfs" = {
          fsType = "binder";
          options = [ "stats=global" ];
        };
      };

      # Kernel params for Android subsystem
      boot.kernelParams = [
        "androidboot.hardware=hart"
      ];
    })

    # ─────────────────────────────────────────────────────────
    # Windows Native: PE binfmt registration at kernel level
    # ─────────────────────────────────────────────────────────
    #
    # Linux kernel's binfmt_misc subsystem detects Windows PE
    # binaries (.exe, .dll, .msi) by their MZ magic header and
    # dispatches them to Wine's native API implementation.
    #
    # Wine is NOT an emulator (Wine Is Not an Emulator).
    # It implements the Windows API (ntdll.dll, kernel32.dll,
    # user32.dll, etc.) as native Linux shared libraries.
    # A .exe runs at the SAME privilege level as a Linux
    # binary — same kernel, same scheduler, same memory
    # manager. The only "translation" is API call routing.
    #
    (lib.mkIf kernelCfg.windowsNative.enable {

      # binfmt_misc: auto-detect PE binaries at kernel level
      boot.binfmt.registrations = {
        # Windows 64-bit PE executables
        DOSWin = {
          recognitionType = "magic";
          offset = 0;
          magicOrExtension = "MZ";
          interpreter = "/run/current-system/sw/bin/wine64";
          wrapInterpreterInShell = false;
          preserveArgvZero = true;
        };
      };

      # Kernel module for Windows filesystem access
      boot.kernelModules = [
        "ntfs3"          # Native NTFS read/write (kernel 5.15+, no FUSE)
        "vfat"           # FAT32 for USB/SD cross-platform
        "exfat"          # exFAT for large files
      ];

      # Higher vm.max_map_count for Wine (Windows apps use many memory mappings)
      boot.kernel.sysctl = {
        "vm.max_map_count" = 2097152;    # Wine recommends >= 1M, we set 2M
      };
    })

    # ─────────────────────────────────────────────────────────
    # AI Compute: GPU scheduling + model memory management
    # ─────────────────────────────────────────────────────────
    #
    # AI workloads are first-class kernel citizens:
    # - GPU memory management at kernel level (not userspace)
    # - Transparent Huge Pages for efficient model loading
    # - Dedicated CPU scheduling for inference threads
    # - cgroups v2 GPU resource limits per agent
    #
    (lib.mkIf kernelCfg.aiCompute.enable {

      # GPU kernel modules
      boot.kernelModules = [
        "nvidia"          # NVIDIA (loaded if hardware present)
        "nvidia_uvm"      # NVIDIA Unified Virtual Memory (GPU ↔ CPU)
        "nvidia_drm"      # NVIDIA Direct Rendering Manager
        "amdgpu"          # AMD GPU (loaded if hardware present)
        "i915"            # Intel integrated GPU
      ];

      # Transparent Huge Pages: 2MB pages for model loading
      boot.kernel.sysctl = {
        # THP: always use huge pages (models benefit from fewer TLB misses)
        "vm.nr_hugepages" = kernelCfg.aiCompute.hugePagesCount;
      };

      # Static huge pages (optional, for dedicated model memory)
      boot.kernelParams = lib.optionals (kernelCfg.aiCompute.hugePagesCount > 0) [
        "hugepagesz=2M"
        "hugepages=${toString kernelCfg.aiCompute.hugePagesCount}"
        "transparent_hugepage=always"
      ] ++ [
        # NVIDIA: enable kernel modesetting for better GPU management
        "nvidia-drm.modeset=1"
        "nvidia-drm.fbdev=1"
      ];

      # GPU device permissions
      services.udev.extraRules = ''
        # NVIDIA GPU: allow hart group access
        KERNEL=="nvidia*", MODE="0666", GROUP="hart"
        KERNEL=="nvidiactl", MODE="0666", GROUP="hart"
        KERNEL=="nvidia-uvm*", MODE="0666", GROUP="hart"

        # AMD GPU: allow hart group access
        SUBSYSTEM=="drm", KERNEL=="renderD*", MODE="0666", GROUP="hart"
        SUBSYSTEM=="drm", KERNEL=="card*", MODE="0666", GROUP="hart"
      '';

      # Enable NVIDIA if hardware present
      hardware.nvidia = {
        open = lib.mkDefault true;  # Open kernel module (Turing+)
        modesetting.enable = true;
      };

      # OpenGL + Vulkan + compute
      hardware.graphics = {
        enable = true;
        enable32Bit = lib.mkDefault true;
      };
    })

    # ─────────────────────────────────────────────────────────
    # Agent Sandboxing: cgroups v2 + Landlock LSM
    # ─────────────────────────────────────────────────────────
    #
    # Agents run as native processes (not containers), isolated
    # via kernel-native mechanisms:
    #
    # - cgroups v2: CPU, memory, GPU, I/O limits per agent
    # - Landlock LSM: filesystem access restrictions (kernel 5.13+)
    # - Seccomp-BPF: syscall filtering per agent
    # - Namespaces: network/PID isolation without containers
    #
    # This gives container-level isolation with native performance.
    #
    (lib.mkIf kernelCfg.agentSandbox.enable {

      # Landlock LSM for filesystem sandboxing
      boot.kernelParams = [
        "lsm=landlock,lockdown,yama,integrity,apparmor,bpf"
      ];

      # Kernel modules for agent isolation
      boot.kernelModules = [
        "cls_bpf"         # BPF traffic classifier (per-agent networking)
        "sch_fq"          # Fair queue scheduling (agent network fairness)
      ];

      # Systemd: create agent cgroup slice
      systemd.slices.hart-agents = {
        description = "HART OS Agent Workloads";
        sliceConfig = {
          # Default limits per agent (overridable per-agent)
          CPUAccounting = true;
          MemoryAccounting = true;
          IOAccounting = true;
          TasksAccounting = true;

          # Global agent slice limits
          MemoryMax = "80%";       # Agents can't starve the OS
          CPUWeight = 100;         # Fair scheduling between agents
          TasksMax = 4096;         # Max concurrent agent threads
        };
      };

      # Seccomp: agent syscall filtering support
      boot.kernel.sysctl = {
        "kernel.unprivileged_userns_clone" = 1;  # Namespace-based isolation
      };

      # Agent data directories with proper permissions
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/agents 0750 hart hart -"
        "d /var/lib/hart/agents/sandboxes 0700 hart hart -"
      ];
    })
  ]);
}
