# ═══════════════════════════════════════════════════════════════
# HART OS PHONE — the variant FEATURE PROFILE (canonical home)
# ═══════════════════════════════════════════════════════════════
#
# This is "what a phone IS": the hart.* feature block, MOVED VERBATIM out of
# configurations/phone.nix (2026-07-28). It is a pure option-set — deliberately no
# `config`/`lib`/`pkgs` captures (verified 0 non-comment scope refs at move time),
# no media/image concerns (ISO branding, repart sizing, growPartition stay in the
# configuration), no hardware assumptions.
#
# WHY A SEPARATE FILE: three consumers need exactly this block and nothing else,
# and until now it lived entangled with image concerns so only the images got it:
#   - configurations/phone.nix        (the iso/raw images — imports this)
#   - tests/lib.nix mkNode          (the nixosTest VMs; their nodes ran with every
#                                    hart.* feature at default-false, which is why
#                                    25 nixosTests were red — task #15)
#   - the hardware-agnostic installer (task #17: an installed system must be the
#                                    UNION of nixos-generate-config hardware +
#                                    THIS profile — never stock NixOS)
# One canonical home, consumed everywhere; per the steward's rule the union of
# NixOS's hardware layer and HART's OS layer, and never two drifting copies.
#
# hart.package is NOT here on purpose: it captures pkgs+hartSrc, so each consumer
# wires it (configurations use packages/hart-app.nix; mkNode builds its own).

{ config, pkgs, hartSrc, ... }:   # config for hart.ports firewall refs; pkgs+hartSrc for the app set
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

  # ─── Phone experience (parity slice, task #21) ───
  # The ENTIRE phone experience is variant surface — phone.nix carries no
  # live-CD machinery, so everything except hart.package moves here and an
  # installed/sd-image phone composes identically.

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

  # phosh is launched directly by greetd below.  There is NO
  # `programs.phosh` NixOS option (the nix flake-check #70 failure:
  # "The option `programs.phosh' does not exist"); the greetd session
  # command is the supported launch path on this nixpkgs pin.  HiDPI
  # output scaling lives in phoc's own config (phoc.ini), written via
  # environment.etc below, not a non-existent NixOS option.
  services.greetd = {
    enable = true;
    settings.default_session = {
      command = "${pkgs.phosh}/bin/phosh";
      user = "hart-admin";
    };
  };

  # phoc (the phosh Wayland compositor) reads this for per-output config;
  # replaces the invalid programs.phosh.phocConfig.output."DSI-1".scale.
  environment.etc."phosh/phoc.ini".text = ''
    [output:DSI-1]
    scale = 2
  '';

  # ─── Cellular ───
  # ModemManager comes with networking.networkmanager.enable below — there is
  # no standalone services.modemManager option on this nixpkgs pin (#70: the
  # eval error "services.modemManager does not exist", surfaced once the phosh
  # error above was cleared).

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
