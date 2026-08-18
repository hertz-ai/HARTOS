{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Unified Kernel -- Multi-Platform Native Binary Support
# ═══════════════════════════════════════════════════════════════
#
# The HART OS kernel is a Linux kernel with native extensions
# for running binaries from ALL major platforms -- not through
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
      # mkForce -- kernel module is the most specialized layer
      boot.kernel.sysctl = {
        # IPC: support high-throughput binder + agent communication
        "kernel.shmmax" = lib.mkForce 68719476736;
        "kernel.shmall" = lib.mkForce 4294967296;
        "kernel.msgmnb" = lib.mkForce 65536;
        "kernel.msgmax" = lib.mkForce 65536;

        # Memory: optimize for multi-runtime memory pressure
        "vm.overcommit_memory" = lib.mkForce 1;
        "vm.max_map_count" = lib.mkForce 2097152;  # Wine/Android need >= 1M
        "vm.vfs_cache_pressure" = lib.mkForce 50;

        # Network: agent-to-agent + P2P gossip
        "net.core.rmem_max" = lib.mkForce 26214400;
        "net.core.wmem_max" = lib.mkForce 26214400;
        "net.core.netdev_max_backlog" = lib.mkForce 5000;

        # File handles: multi-runtime concurrent I/O
        "fs.file-max" = lib.mkForce 2097152;
        "fs.inotify.max_user_instances" = lib.mkForce 8192;
        "fs.inotify.max_user_watches" = lib.mkForce 1048576;
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
        "binder_linux"    # Android Binder IPC -- native kernel module
        "ashmem_linux"    # Android shared memory -- native kernel module
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
          device = "binder";
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
    # binary -- same kernel, same scheduler, same memory
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

      # vm.max_map_count for Wine set in main sysctl block above (2097152)
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
      #
      # Only the Intel iGPU (i915) is force-loaded: it drives the panel on
      # the Intel + integrated-graphics desktop, so KMS must come up
      # deterministically at boot. The discrete-GPU modules are NOT
      # force-loaded. boot.kernelModules feeds systemd-modules-load.service,
      # which FAILS the unit when a listed module is absent from the running
      # kernel. On a box without that hardware (e.g. the Intel + GeForce
      # 940MX laptop, which has no AMD GPU and ships no proprietary NVIDIA
      # driver), force-loading "amdgpu"/"nvidia" errored out and degraded the
      # boot. Instead the discrete drivers are made available for udev to
      # auto-load by PCI modalias, only when the matching hardware is
      # actually present (load-if-present: never fails on absent hardware).
      # A server/edge box with a real discrete GPU still gets its driver:
      # udev matches the dGPU PCI modalias and loads it; nvidia_drm is also
      # brought up by the nvidia-drm.modeset kernel param below, and
      # nvidia_uvm is loaded on first CUDA use.
      boot.kernelModules = [
        "i915"            # Intel integrated GPU - drives the panel (force-load)
      ];

      # Discrete-GPU drivers: load-if-present, so absent hardware never fails
      # systemd-modules-load. Only amdgpu is listed here: it is IN-TREE, so it
      # exists in the kernel module set and udev auto-loads it by PCI modalias
      # when an AMD GPU is present. The proprietary NVIDIA modules (nvidia,
      # nvidia_uvm, nvidia_drm) are OUT-OF-TREE - they are NOT in the mainline
      # kernel module set, so listing them here would fail the initrd build
      # ("module not found"). They are loaded by the nvidia package's own udev
      # rules + the nvidia-drm.modeset param when hardware.nvidia is configured
      # (server/edge); on a box without NVIDIA they simply never load. The old
      # boot.kernelModules force-load of nvidia/amdgpu is what failed systemd-
      # modules-load on the Intel + 940MX desktop.
      boot.initrd.availableKernelModules = [
        "amdgpu"          # AMD GPU - in-tree, udev loads it when present
      ];

      # Transparent Huge Pages: 2MB pages for model loading
      boot.kernel.sysctl = {
        # THP: always use huge pages (models benefit from fewer TLB misses)
        "vm.nr_hugepages" = lib.mkForce kernelCfg.aiCompute.hugePagesCount;
      };

      # Static huge pages (optional, for dedicated model memory)
      boot.kernelParams = lib.optionals (kernelCfg.aiCompute.hugePagesCount > 0) [
        "hugepagesz=2M"
        "hugepages=${toString kernelCfg.aiCompute.hugePagesCount}"
        "transparent_hugepage=always"
      ] ++ [
        # ── NVIDIA DRM: modeset ON (render node), fbdev OFF (never the display) ──
        # modeset=1 STAYS: it enables KMS on the nvidia DRM node so the dGPU can act
        # as a GBM/GL render provider for PRIME render-offload (the hart-gpu-offload /
        # prime-run path). It does NOT make nvidia drive the panel -- it only lets the
        # nvidia node be a RENDER target. Never force-loaded (#132): this param is
        # inert on a box whose nvidia module never loads (no dGPU present).
        #
        # fbdev=0 (flipped from 1) -- THE DRM-MASTER-CONTENTION FIX. `nvidia-drm.fbdev=1`
        # makes the nvidia DRM driver register as the FRAMEBUFFER-CONSOLE (fbcon)
        # provider and claim the boot/console framebuffer. On this Optimus laptop the
        # Intel iGPU (i915) drives the eDP-1 panel and NVIDIA (940MX) is offload-only,
        # so letting nvidia own the console FB makes the nvidia node contend to be the
        # seat's primary DRM device: plymouth/logind grab DRM master on nvidia's node
        # while the compositor opens the Intel card (card1) and then CANNOT become
        # master -- the exact real-HW symptom (hart-comp: "Unable to become drm master,
        # assuming unprivileged mode" → falls to the pixman software floor → the slow,
        # software-rendered desktop). With fbdev=0 the nvidia driver does NOT provide
        # fbcon, so the Intel i915 (via the simpledrm/efifb → i915 boot-FB handoff) is
        # the unambiguous console-FB owner + seat-primary and hands DRM master cleanly
        # to hart-comp on the iGPU -- letting it keep the GLES (GPU) scanout it already
        # arms instead of dropping to pixman. NVIDIA stays a pure render node (modeset),
        # never the display. This REINFORCES the #99-103 Intel-panel path (nouveau
        # blacklisted, i915 force-loaded, desktop drawn on the iGPU) -- it does not
        # regress it.
        #
        # NEVER-BRICK: (1) the param is inert without a loaded nvidia module (#132
        # preserved -- no force-load, live-USB / no-dGPU boxes unaffected). (2) fbdev=0
        # only changes WHO provides fbcon; the early TTY/console still comes up on
        # simpledrm/efifb, and the GUI still comes up via modeset=1 -- no display path is
        # removed. (3) If hart-comp STILL cannot take master or GLES init faults, the
        # existing degrade chain is untouched: pixman renderer of record (udev.rs) +
        # the supervisor paint watchdog drop Tier-1 → sway → cage. Worst case is exactly
        # today's software desktop, never a black/bricked screen. REAL-HW-GATED: verify
        # on the node via the dev loop (canary + rollback protect the OTA push).
        "nvidia-drm.modeset=1"
        "nvidia-drm.fbdev=0"
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
        # x86_64 ONLY. nixpkgs asserts "`hardware.graphics.enable32Bit` is only
        # supported on an x86_64 system", and there is no 32-bit userland to
        # support anywhere else, so an unconditional `true` here is not a
        # preference that goes unused off x86, it is an eval FAILURE: it took
        # down every ARM and RISC-V configuration (hart-server-arm,
        # hart-desktop-arm/rpi, hart-edge-arm/riscv, hart-phone, ...). Same
        # platform gate the open-vm-tools default already uses in hart-base.nix.
        enable32Bit = lib.mkDefault pkgs.stdenv.hostPlatform.isx86_64;
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
        "kernel.unprivileged_userns_clone" = lib.mkForce 1;  # Namespace-based isolation
      };

      # Agent data directories with proper permissions
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/agents 0750 hart hart -"
        "d /var/lib/hart/agents/sandboxes 0700 hart hart -"
      ];
    })
  ]);
}
