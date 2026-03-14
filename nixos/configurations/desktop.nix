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

    # ── Self-Building OS ──
    selfBuild = {
      enable = true;                   # OS can rebuild itself at runtime
      autoRebuild = false;             # Require explicit `hart-ota self-build`
      allowAgentBuilds = false;        # Agents propose, humans approve
      maxBuildsPerDay = 10;
    };

    # ── OTA Updates ──
    ota = {
      enable = true;
      channel = "stable";
      autoApply = false;               # Stage updates, user approves
    };
  };

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # ═══════════════════════════════════════════════════════════════
  # Prebundled Apps — best-in-class from ALL OS ecosystems
  # ═══════════════════════════════════════════════════════════════
  #
  # Philosophy: every app a real OS ships, HART OS ships better.
  # GTK4/libadwaita preferred for native GNOME 50 experience.
  # Users can install Android/Windows apps via subsystems.
  #
  environment.systemPackages = with pkgs; [
    (pkgs.callPackage ../packages/hart-cli.nix { inherit hartSrc; })

    # ── Browser & Web ──
    firefox                     # Primary browser (privacy-first)
    epiphany                    # GNOME Web — lightweight secondary / PWA host

    # ── Terminal ──
    gnome-console               # GNOME Console — GTK4/libadwaita native
    kitty                       # GPU-accelerated power terminal
    # OpenTerminal: gnome-console IS the modern open terminal for GNOME 50
    # (replaces legacy gnome-terminal with native GTK4/Adwaita)

    # ── Text & Code Editors ──
    gnome-text-editor           # Simple text editor (like Notepad/TextEdit)
    helix                       # Modal editor (like Vim, but modern — Rust)

    # ── File Management ──
    nautilus                    # GNOME Files (like Explorer/Finder)
    file-roller                 # Archive manager (ZIP/RAR/7z/tar)
    baobab                      # Disk usage analyzer (like WinDirStat/Storage Sense)

    # ── Image & Photo ──
    loupe                       # GNOME image viewer (like Photos/Preview) — GTK4
    shotwell                    # Photo manager (like Photos/Gallery) — import/organize
    drawing                     # Simple drawing/paint app (like Paint/Markup)

    # ── Video & Music ──
    celluloid                   # Video player (mpv frontend, GTK4 — like Media Player/QuickTime)
    amberol                     # Music player (GTK4/libadwaita — clean, local-first)

    # ── Documents & PDF ──
    papers                      # Document/PDF viewer (like Preview/Edge PDF — GTK4)
    libreoffice                 # Full office suite (like Microsoft 365/iWork)

    # ── Communication ──
    thunderbird                 # Email client (like Mail/Gmail/Outlook)
    gnome-contacts              # Contacts manager
    fractal                     # Matrix chat client (GTK4/libadwaita — federated messaging)

    # ── Productivity ──
    gnome-calculator            # Calculator
    gnome-calendar              # Calendar (CalDAV sync)
    gnome-clocks                # World clock, timer, stopwatch, alarms
    gnome-weather               # Weather (like Weather app on every OS)
    gnome-maps                  # Maps (OpenStreetMap — like Maps on every OS)
    iotas                       # Notes app (GTK4/libadwaita — like Notes/Samsung Notes)

    # ── Camera & Recording ──
    snapshot                    # Camera app (GTK4/libadwaita — like Camera)
    gnome-sound-recorder        # Voice recorder (like Voice Memos/Sound Recorder)
    obs-studio                  # Screen recording & streaming (like Game Bar/screen recorder)

    # ── System Tools ──
    gnome-system-monitor        # Task/process manager (like Task Manager/Activity Monitor)
    gnome-disk-utility          # Disk management (partitioning, formatting, SMART)
    gnome-font-viewer           # Font viewer/installer (like Font Book)
    gnome-connections           # Remote desktop viewer (RDP/VNC)
    dconf-editor                # System configuration editor (advanced)

    # ── Media Creation ──
    pitivi                      # Video editor (like iMovie/Clipchamp — GTK/GStreamer)
    gimp                        # Image editor (like Photoshop — advanced)

    # ── Security ──
    seahorse                    # Password & key manager (like Keychain Access)

    # ── Development (all major languages, native) ──
    git gcc gnumake cmake
    python310 nodejs_20 rustup go jdk21

    # ── System Utilities ──
    htop btop                   # System monitors (CLI)
    neofetch                    # System info
    file unzip p7zip            # File tools
    wget curl                   # Network tools
    ripgrep fd bat              # Modern CLI tools (better grep/find/cat)
    tree jq                     # Directory tree / JSON processor
    mpv                         # Media backend (used by celluloid, also standalone)
  ];

  # ─── ISO Branding ───
  isoImage = {
    isoName = lib.mkForce "hart-os-${config.hart.version}-desktop-${pkgs.system}.iso";
    volumeID = lib.mkForce "HART_OS";
    appendToMenuLabel = " HART OS Desktop";
  };

  # ═══════════════════════════════════════════════════════════════
  # GNOME 50 Desktop — full desktop environment
  # ═══════════════════════════════════════════════════════════════
  services.xserver = {
    enable = true;
    displayManager.gdm.enable = true;
    desktopManager.gnome.enable = true;
    # Keyboard layout — user-selectable via Settings > Keyboard
    xkb = {
      layout = "us";
      options = "ctrl:nocaps";  # Caps Lock → Ctrl (power user default)
    };
  };

  # GNOME Shell extensions + theming
  environment.gnome.excludePackages = with pkgs; [
    gnome-tour  # Disable first-run tour (HART has its own onboarding)
  ];
  programs.dconf.profiles.user.databases = [{
    settings = {
      # ─── HART OS Branding ───
      "org/gnome/desktop/interface" = {
        gtk-theme = "Adwaita-dark";
        color-scheme = "prefer-dark";
        monospace-font-name = "JetBrains Mono 11";
        document-font-name = "Cantarell 11";
      };
      "org/gnome/desktop/background" = {
        picture-uri = "file:///etc/hart/branding/wallpaper.png";
        picture-uri-dark = "file:///etc/hart/branding/wallpaper-dark.png";
        primary-color = "#080808";
      };
      "org/gnome/desktop/screensaver" = {
        picture-uri = "file:///etc/hart/branding/lock-screen.png";
        primary-color = "#080808";
      };
      # ─── Taskbar / Dash / Top Bar customization ───
      "org/gnome/shell" = {
        favorite-apps = [
          "firefox.desktop"
          "org.gnome.Nautilus.desktop"
          "org.gnome.Console.desktop"
          "org.gnome.TextEditor.desktop"
          "org.libreoffice.LibreOffice.Writer.desktop"
          "org.gnome.Calculator.desktop"
          "hart-identity.desktop"
        ];
        # GNOME 50: dynamic workspaces + app grid
        enabled-extensions = [
          "dash-to-dock@micxgx.gmail.com"
          "appindicatorsupport@rgcjonas.gmail.com"
        ];
      };
      "org/gnome/shell/extensions/dash-to-dock" = {
        dock-position = "BOTTOM";
        dash-max-icon-size = 48;
        extend-height = false;
        transparency-mode = "DYNAMIC";
        running-indicator-style = "DOTS";
        show-trash = true;
        show-mounts = false;
      };
      # ─── Keyboard Shortcuts (Windows-style defaults) ───
      # User can switch to Mac profile via keyboard_shortcuts panel
      "org/gnome/desktop/wm/keybindings" = {
        close = ["<Alt>F4"];                    # Win: Alt+F4, Mac: Cmd+W
        minimize = ["<Super>h"];                # Minimize window
        toggle-maximized = ["<Super>Up"];        # Win: Win+Up
        switch-applications = ["<Alt>Tab"];      # App switching
        switch-windows = ["<Alt>grave"];         # Window cycling within app
        move-to-workspace-left = ["<Super><Shift>Left"];
        move-to-workspace-right = ["<Super><Shift>Right"];
        switch-to-workspace-left = ["<Super><Ctrl>Left"];
        switch-to-workspace-right = ["<Super><Ctrl>Right"];
      };
      "org/gnome/shell/keybindings" = {
        toggle-overview = ["<Super>space"];      # Activities / Spotlight
        toggle-application-grid = ["<Super>a"];  # App grid
        screenshot = ["Print"];
        show-screenshot-ui = ["<Shift>Print"];
        screenshot-window = ["<Alt>Print"];
      };
      "org/gnome/settings-daemon/plugins/media-keys" = {
        home = ["<Super>e"];                     # File manager (Win: Win+E)
        terminal = ["<Ctrl><Alt>t"];              # Terminal
        www = ["<Super>b"];                       # Browser
        search = ["<Super>s"];                    # Search
        screensaver = ["<Super>l"];               # Lock screen (Win: Win+L)
        calculator = ["<Super>c"];                # Calculator
      };
      # ─── Multi-monitor & Window snapping ───
      "org/gnome/mutter" = {
        edge-tiling = true;           # Snap windows to edges
        dynamic-workspaces = true;    # Auto create/remove workspaces
        workspaces-only-on-primary = true;
      };
      # ─── Touchpad gestures (3-finger swipe = workspace switch) ───
      "org/gnome/desktop/peripherals/touchpad" = {
        tap-to-click = true;
        two-finger-scrolling-enabled = true;
        natural-scroll = true;
      };
    };
  }];

  # ─── GNOME Shell Extensions (packaged) ───
  # Uses lib.mkAfter to merge with the primary systemPackages list above
  environment.systemPackages = lib.mkAfter (with pkgs; [
    gnomeExtensions.dash-to-dock       # Taskbar (dock) at bottom
    gnomeExtensions.appindicator       # System tray support
    jetbrains-mono                     # Default monospace font
  ]);

  # ─── i18n / Language Support ───
  # Install fonts for ALL major writing systems
  fonts = {
    packages = with pkgs; [
      noto-fonts                   # Latin, Cyrillic, Greek
      noto-fonts-cjk-sans          # Chinese, Japanese, Korean
      noto-fonts-emoji             # Emoji
      noto-fonts-extra             # Arabic, Devanagari, Thai, etc.
      liberation_ttf               # Metric-compatible with Arial/Times/Courier
      jetbrains-mono               # Monospace for code
      fira-code                    # Alternative monospace with ligatures
    ];
    fontconfig.defaultFonts = {
      serif = [ "Noto Serif" "Liberation Serif" ];
      sansSerif = [ "Noto Sans" "Liberation Sans" ];
      monospace = [ "JetBrains Mono" "Fira Code" "Noto Sans Mono" ];
      emoji = [ "Noto Color Emoji" ];
    };
  };

  # Input methods (CJK + multilingual)
  i18n = {
    defaultLocale = "en_US.UTF-8";
    supportedLocales = [
      "en_US.UTF-8/UTF-8" "en_GB.UTF-8/UTF-8"
      "de_DE.UTF-8/UTF-8" "fr_FR.UTF-8/UTF-8" "es_ES.UTF-8/UTF-8"
      "pt_BR.UTF-8/UTF-8" "it_IT.UTF-8/UTF-8" "nl_NL.UTF-8/UTF-8"
      "ja_JP.UTF-8/UTF-8" "ko_KR.UTF-8/UTF-8"
      "zh_CN.UTF-8/UTF-8" "zh_TW.UTF-8/UTF-8"
      "hi_IN.UTF-8/UTF-8" "ar_SA.UTF-8/UTF-8" "ru_RU.UTF-8/UTF-8"
      "tr_TR.UTF-8/UTF-8" "th_TH.UTF-8/UTF-8" "vi_VN.UTF-8/UTF-8"
    ];
    inputMethod = {
      enable = true;
      type = "ibus";
      ibus.engines = with pkgs.ibus-engines; [
        libpinyin       # Chinese (Pinyin)
        anthy           # Japanese
        hangul          # Korean
        m17n            # Multilingual (Hindi, Arabic, Thai, etc.)
      ];
    };
  };

  # ─── Default Apps (XDG MIME associations) ───
  xdg.mime.defaultApplications = {
    "text/html" = "firefox.desktop";
    "x-scheme-handler/http" = "firefox.desktop";
    "x-scheme-handler/https" = "firefox.desktop";
    "text/plain" = "org.gnome.TextEditor.desktop";
    "application/pdf" = "org.gnome.Papers.desktop";
    "image/png" = "org.gnome.Loupe.desktop";
    "image/jpeg" = "org.gnome.Loupe.desktop";
    "image/gif" = "org.gnome.Loupe.desktop";
    "image/webp" = "org.gnome.Loupe.desktop";
    "video/mp4" = "io.github.celluloid_player.Celluloid.desktop";
    "video/webm" = "io.github.celluloid_player.Celluloid.desktop";
    "audio/mpeg" = "io.bassi.Amberol.desktop";
    "audio/flac" = "io.bassi.Amberol.desktop";
    "inode/directory" = "org.gnome.Nautilus.desktop";
    "x-scheme-handler/mailto" = "org.mozilla.Thunderbird.desktop";
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

  # ─── Printing & Scanning ───
  services.printing.enable = true;
  services.avahi = {
    enable = true;
    nssmdns4 = true;  # mDNS for network printer discovery
  };
  hardware.sane = {
    enable = true;    # Scanner support (SANE backends)
    extraBackends = [ pkgs.sane-airscan ];  # eSCL/AirScan wireless scanners
  };

  # ─── Location Services (for weather, timezone auto-detect) ───
  services.geoclue2.enable = true;

  # ─── Accessibility ───
  services.gnome.at-spi2-core.enable = true;  # Screen reader support

  # ─── Power Management ───
  services.upower.enable = true;
  services.thermald.enable = true;

  # ─── HART OS Branding ───
  # Logo and wallpaper files are deployed to /etc/hart/branding/
  # by the hart-branding package (or manually placed there)
  environment.etc = {
    "hart/branding/README" = {
      text = ''
        HART OS Branding Assets
        =======================
        wallpaper.png      — Desktop wallpaper (dark theme, HART logo)
        wallpaper-dark.png — Dark variant
        lock-screen.png    — Lock screen background
        logo.svg           — HART OS logo (scalable)
        logo-64.png        — HART OS logo 64x64
        logo-128.png       — HART OS logo 128x128
        icon.svg           — Application icon

        The HART OS logo features a minimalist geometric heart shape
        with circuit-board traces emanating from within, symbolizing
        the union of human compassion and machine intelligence.
        Color: #00D4AA (HART accent green) on dark (#080808) background.
      '';
    };
  };
}
