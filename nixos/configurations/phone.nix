{ config, lib, pkgs, hartSrc, mobile-nixos, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Phone Variant
# ═══════════════════════════════════════════════════════════════
#
# Native multi-platform phone OS:
#   - Linux apps (native, touch-adaptive via Phosh)
#   - Android apps (native ART — runs WhatsApp, banking, maps natively)
#   - AI agent (native, offloads LLM to hive peers)
#   - Nunba as primary management app
#   - Conky dashboard overlay
#
# For: PinePhone, PinePhone Pro, future ARM phones

{
  # ─── HART OS Core Services ───
  hart = {
    enable = true;
    variant = "phone";

    # Backend + discovery + agent (brain of the node)
    agent.enable = true;
    llm.enable = false;      # Offload to peer nodes
    vision.enable = false;

    # Phone UI
    conky.enable = true;
    nunba.enable = true;

    # ── Kernel Extensions ──
    kernel = {
      enable = true;
      androidNative.enable = true;     # binder + ashmem (Android apps)
      windowsNative.enable = false;    # No Windows on phone
      aiCompute.enable = false;        # No local GPU compute
      agentSandbox.enable = true;      # Isolate agents
    };

    # ── Native Subsystems ──
    subsystems = {
      enable = true;

      linux.flatpak = true;            # Adaptive Linux apps from Flathub

      # Android: native ART (the killer feature — run any Android app)
      android = {
        enable = true;
        playStore = true;              # Most phone users need Google Play
      };

      windows.enable = false;          # Not applicable on phone
      web.enable = true;               # PWA for lightweight apps
    };

    # ── AI Runtime (lightweight for phone) ──
    aiRuntime = {
      enable = true;
      gpu.enable = false;
      agents = {
        maxConcurrent = 3;             # Phone has limited resources
        maxMemoryPerAgent = "512M";
      };
      # Semantic: service healing + prefetch (no smartFS — storage limited)
      semantic = {
        enable = true;
        serviceIntelligence = true;
        predictivePrefetch = true;
        smartFS = false;
      };
    };

    # ── AI-Native Everything OS ──
    # Model Bus: Android apps + Linux apps get native AI
    modelBus = {
      enable = true;
      enableAndroidBridge = true;      # Android apps call AI via content provider
    };

    # Compute Mesh: offload heavy inference to desktop/server
    computeMesh = {
      enable = true;
      allowWAN = true;                 # Phone needs WAN to reach desktop
    };

    # LiquidUI: adaptive interface with voice + haptic
    liquidUI = {
      enable = true;
      voiceEnabled = true;
      hapticEnabled = true;
      renderer = "webkit";
    };

    # App Bridge: Android ↔ Linux cross-subsystem (no Windows on phone)
    appBridge = {
      enable = true;
      intentRouter = true;             # Route Android Intents to Linux services
      clipboardSync = true;
    };

    # ── On-Screen Keyboard ──
    osk = {
      enable = true;
      backend = "squeekboard";
      autoShow = true;
      hapticFeedback = true;
    };

    # ── Sandbox ──
    sandbox.enable = true;
  };

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # ─── Phone Packages ───
  environment.systemPackages = with pkgs; [
    (pkgs.callPackage ../packages/hart-cli.nix { inherit hartSrc; })

    # Phone essentials
    squeekboard
    gnome-contacts
    gnome-calls
    chatty
    megapixels
    gnome-clocks
    gnome-calculator
    firefox
    epiphany
    gnome-files
  ];

  # ─── Phosh (GNOME Mobile Shell) ───
  services.xserver.enable = false;   # Wayland only

  services.greetd = {
    enable = true;
    settings.default_session = {
      command = "${pkgs.phosh}/bin/phosh";
      user = "hart-admin";
    };
  };

  programs.phosh = {
    enable = true;
    phocConfig.output."DSI-1".scale = 2;
  };

  # ─── Cellular ───
  services.modemManager.enable = true;

  networking = {
    networkmanager = {
      enable = true;
      wifi.powersave = true;
    };
    wireless.enable = false;
    firewall = {
      allowedTCPPorts = [ config.hart.ports.backend 22 ];
      allowedUDPPorts = [ config.hart.ports.discovery ];
    };
  };

  # ─── Power ───
  services.upower.enable = true;
  services.tlp = {
    enable = true;
    settings = {
      CPU_SCALING_GOVERNOR_ON_BAT = "powersave";
      CPU_SCALING_GOVERNOR_ON_AC = "performance";
      WIFI_PWR_ON_BAT = "on";
    };
  };

  # ─── Audio ───
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
  };

  # ─── Peripherals ───
  hardware.bluetooth = { enable = true; powerOnBoot = true; };
  hardware.sensor.iio.enable = true;
  services.geoclue2.enable = true;

  # ─── Display ───
  services.displayManager.autoLogin = { enable = true; user = "hart-admin"; };

  # ─── Phone Tuning ───
  boot.kernel.sysctl = {
    "vm.laptop_mode" = 5;
    "vm.dirty_writeback_centisecs" = 6000;
  };

  services.journald.extraConfig = ''
    SystemMaxUse=50M
    MaxRetentionSec=3days
  '';
}
