{ config, lib, pkgs, modulesPath, hartSrc, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Desktop Variant
# ═══════════════════════════════════════════════════════════════
#
# Full desktop with ALL native subsystems:
#   - Linux apps (native)
#   - Android apps (native ART + Binder IPC)
#   - Windows apps (native Wine API implementation)
#   - AI agents (native GPU + kernel IPC)
#   - Nunba management app + Conky dashboard overlay
#
# Zero emulators. Zero containers. Zero simulation.
# Every app runs at the same kernel level.
#
# Minimum 8GB RAM.

{
  imports = [
    "${modulesPath}/installer/cd-dvd/installation-cd-graphical-gnome.nix"
  ];

  # ─── Disable ZFS (broken in nixpkgs 24.11 for kernel 6.15) ───
  boot.supportedFilesystems.zfs = lib.mkForce false;

  # ─── HART OS Core Services ───
  hart = {
    enable = true;
    variant = "desktop";

    # AI services
    agent.enable = true;
    llm.enable = true;
    vision.enable = true;

    # Desktop UI
    conky.enable = true;
    nunba.enable = true;

    # ── Unified Kernel Extensions ──
    kernel = {
      enable = true;
      androidNative.enable = true;     # binder + ashmem kernel modules
      windowsNative.enable = true;     # PE binfmt + NTFS + high mmap
      aiCompute = {
        enable = true;                 # GPU scheduling + huge pages
        hugePagesCount = 0;            # Auto (THP); set to 4096 for 8GB dedicated
      };
      agentSandbox.enable = true;      # cgroups v2 + Landlock LSM
    };

    # ── Native Subsystems (no emulation) ──
    subsystems = {
      enable = true;

      # Linux: native + distribution methods
      linux = {
        flatpak = true;                # Flathub app store
        appimage = true;               # Portable apps
      };

      # Android: native ART runtime (not a container)
      android = {
        enable = true;
        playStore = false;             # AOSP + F-Droid; set true for Google Play
      };

      # Windows: native Wine API (not an emulator)
      windows = {
        enable = true;
        gaming = true;                 # Steam + Proton + DXVK
      };

      # Web: PWA as native windows
      web.enable = true;
    };

    # ── AI Runtime ──
    aiRuntime = {
      enable = true;
      gpu.enable = true;
      worldModel.enable = true;
      agents = {
        maxConcurrent = 8;
        maxMemoryPerAgent = "2G";
      };
      # Full semantic intelligence on desktop
      semantic = {
        enable = true;
        serviceIntelligence = true;
        smartFS = true;                # AI-indexed filesystem for desktop users
        predictivePrefetch = true;
      };
    };

    # ── AI-Native Everything OS ──
    # Model Bus: every app (Linux, Android, Windows) gets native AI
    modelBus = {
      enable = true;
      enableAndroidBridge = true;
      enableWineBridge = true;
    };

    # Compute Mesh: aggregate compute across user's devices
    computeMesh = {
      enable = true;
      allowWAN = true;
    };

    # LiquidUI: AI-generated adaptive interface
    liquidUI = {
      enable = true;
      voiceEnabled = true;
      renderer = "webkit";
    };

    # App Bridge: Android ↔ Linux ↔ Windows cross-subsystem routing
    appBridge = {
      enable = true;
      clipboardSync = true;
      dragAndDrop = true;
      intentRouter = true;
    };

    # ── Subsystem Sandbox ──
    sandbox.enable = true;             # `hart sandbox test-all`
  };

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # ─── System Packages ───
  environment.systemPackages = with pkgs; [
    (pkgs.callPackage ../packages/hart-cli.nix { inherit hartSrc; })
    firefox
    gnome-terminal

    # Development: all major languages (native, not sandboxed)
    git gcc gnumake cmake
    python310 nodejs_20 rustup go jdk21

    # System utilities
    htop neofetch file unzip wget
  ];

  # ─── ISO Branding ───
  isoImage = {
    isoName = lib.mkForce "hart-os-${config.hart.version}-desktop-${pkgs.system}.iso";
    volumeID = lib.mkForce "HART_OS";
    appendToMenuLabel = " HART OS Desktop";
  };

  # ─── GNOME Desktop ───
  services.xserver = {
    enable = true;
    displayManager.gdm.enable = true;
    desktopManager.gnome.enable = true;
  };

  # D-Bus policy for HART agent bridge
  services.dbus.packages = lib.mkIf (builtins.pathExists ../dbus/com.hart.Agent.conf) [
    (pkgs.writeTextDir "share/dbus-1/system.d/com.hart.Agent.conf"
      (builtins.readFile ../dbus/com.hart.Agent.conf))
  ];

  # Auto-login
  services.displayManager.autoLogin = {
    enable = true;
    user = "hart-admin";
  };

  # Audio: PipeWire bridges all subsystems (Linux, Android, Wine)
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
    jack.enable = true;
  };

  # Bluetooth
  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
  };

  # GPU: Vulkan + 32-bit (required for DXVK/Proton)
  hardware.graphics = {
    enable = true;
    enable32Bit = true;
  };

  # Printing
  services.printing.enable = true;
}
