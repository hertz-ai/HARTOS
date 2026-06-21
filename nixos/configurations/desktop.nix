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

let
  # Rasterize the HART logo (SVG source in nixos/branding/) → PNG for Plymouth,
  # which needs raster.  GNOME renders the SVG wallpaper directly (dconf below).
  hartLogoPng = pkgs.runCommand "hart-logo-png" { nativeBuildInputs = [ pkgs.librsvg ]; } ''
    mkdir -p $out
    rsvg-convert -w 320 -h 320 ${../branding/hart-logo.svg} -o $out/logo.png
  '';

  # ─── Premium boot splash (opt-in) ──────────────────────────────────────
  # DEFAULT FALSE — boot stays on NixOS's stock Plymouth theme with the HART
  # logo swapped in (boot.plymouth.logo below), the combination proven on the
  # #99-103 boot path. Flip to true for the custom HART theme (dark gradient +
  # centered logo + LUKS message/password support) AFTER verifying it paints on
  # a real ISO boot. The theme uses Plymouth's `script` plugin (framebuffer/KMS,
  # no GL — safe on the same broken-GPU path the glass shell hardens for), so a
  # bad GPU can't text-downgrade it. Off = byte-identical to the current boot.
  useCustomBootSplash = false;

  hartPlymouth = pkgs.runCommand "hart-plymouth-theme" { } ''
    d=$out/share/plymouth/themes/hart
    mkdir -p "$d"
    cp ${hartLogoPng}/logo.png "$d/logo.png"
    cp ${../branding/plymouth/hart.script} "$d/hart.script"
    {
      echo "[Plymouth Theme]"
      echo "Name=HART OS"
      echo "Description=HART OS boot splash"
      echo "ModuleName=script"
      echo ""
      echo "[script]"
      echo "ImageDir=$d"
      echo "ScriptFile=$d/hart.script"
    } > "$d/hart.plymouth"
  '';
in
{
  imports = [
    "${modulesPath}/installer/cd-dvd/installation-cd-graphical-gnome.nix"
  ];

  # ─── Disable ZFS (broken in nixpkgs 24.11 for kernel 6.15) ───
  boot.supportedFilesystems.zfs = lib.mkForce false;
  # nixpkgs.config.allowBroken now set once at the flake level (#70)

  # Note: do NOT override `glibcLocales` with a custom `locales`
  # allow-list. Changing its derivation hash invalidates the
  # cache.nixos.org binary for glibcLocales AND for every package
  # that depends on it — cascading hundreds of from-source rebuilds
  # that blow the 180-min GHA build cap (run 24639371107 hit
  # 3h0m16s and was still going). The original ENOSPC in full
  # locale-gen (seen in 24623184098 etc.) only reproduced under
  # magic-nix-cache; with that dropped and substituters pinned to
  # cache.nixos.org, the prebuilt glibcLocales binary is served
  # directly and no from-source locale-gen runs. `i18n.supportedLocales`
  # below still trims the runtime locale-archive to 18 locales.

  # ─── Workaround: systemd-hwdb update fails on CI/WSL2 build hosts ───
  # Replace the hwdb.bin derivation with a minimal stub.
  # The real hwdb.bin will be regenerated on first boot by udev.
  environment.etc."udev/hwdb.bin".source = lib.mkForce (
    pkgs.runCommand "hwdb-stub" {} ''
      # Create minimal valid hwdb binary (KSLP magic + empty index)
      printf 'KSLP\x00\x00\x00\x00' > $out
    ''
  );

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

    # ── Supervisor-managed compositor TIER LADDER (the never-blank boot) ──
    # The out-of-process session tier-drop supervisor (greetd) OWNS the boot
    # session: it starts at the BEST tier and falls back on failure —
    #   Tier-1 hart-comp (Smithay/Rust, --backend drm)
    #     → Tier-2 sway (the hart-glass-gtk4 layer-shell session)
    #       → Tier-3 cage (hart-shell, the audited never-fail paint floor).
    # A crash OR a shell-paint timeout drops + LATCHES one tier down; cage is the
    # floor the supervisor can never drop below. This REPLACES the crude fixed
    # cage-pin (68ce3c3 `defaultSession = "hart-shell"`) with the real tiered
    # design — see the session block lower in this file.
    sessionSupervisor = {
      enable = true;
      # Fresh/un-latched boots start at Tier-1 (hart-comp). The supervisor owns
      # the never-blank guarantee, so an unavailable/crashing/hung Tier-1 falls
      # RE-ARMED to hart-comp: both deferral blockers are fixed —
      #   (1) the GTK4 glass-host paint hang — GSK's GL renderer on a real GPU +
      #       an undefined _on_load_changed that never fired the shell-ready marker
      #       — fixed in 75ba78d (GSK_RENDERER=cairo + the marker handler), and
      #   (2) the iso-desktop build hang — the Release build-iso cores=2 throttle on
      #       the from-source Rust compile — fixed in 48b73d6 (warm the Rust closure
      #       at full cores before the throttled ISO step).
      # The boot now tries Tier-1 first; the shell-paint watchdog still drops to
      # sway then the cage floor if Tier-1 fails on real HW (safe to re-arm).
      startTier = "hart-comp";
    };

    # Tier-1: HART-comp, the AI-native Smithay/Rust compositor (--backend drm).
    # Enabling it puts the hart-comp package + the `hart-comp-session` launcher in
    # the desktop closure and arms the supervisor's Tier-1 rung (compCommand via
    # mkDefault in hart-comp.nix). hart-comp reuses the SAME GTK4 layer-shell glass
    # host as Tier-2 sway, so it satisfies the same shell-paint watchdog marker.
    # RE-ARMED (both deferral preconditions met): (a) the GTK4 glass-host paint
    # hang is fixed (75ba78d — GSK cairo renderer + the shell-ready marker handler),
    # and (b) the iso-desktop build no longer hangs (48b73d6 — the Release build-iso
    # job warms the hart.comp Rust closure at full cores BEFORE the throttled ISO
    # step, so it is reused, not recompiled under the cores=2 cap). The shell-paint
    # watchdog still falls back to sway then cage if Tier-1 fails on real HW.
    comp.enable = true;
    rustPrecedent.enable = true;

    # Tier-2: sway running the canonical glass shell + the swaymsg WM shim the
    # brain drives when HART-comp is absent. Registers the sway session + the
    # supervisor's Tier-2 rung. The supervisor's swayCommand is repointed to the
    # GTK4 layer-shell host session (the `hart-glass-gtk4` session) lower in this
    # file so Tier-2 is a TRUE layer-shell desktop, not bare sway.
    swayTier1.enable = true;

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
    # Autonomous central-controlled OTA: the node polls CENTRAL on boot (and on
    # `hart-ota check`) and receives CENTRAL pushes at any time over the existing
    # fleet/gossip fabric — NO periodic interval poll. autoApply=true makes the
    # apply hands-off (the `completed` branch switches via `nixos-rebuild switch
    # --flake` with `|| nixos-rebuild switch --rollback`), so a steward publish
    # lands on every node with no per-node USB/flash. The master-key SIGN gate +
    # canary + auto-rollback still run before DEPLOY — central only chooses WHICH
    # commit; the node never force-applies past canary, and the master key is
    # never touched on the node. This replaces the user's last manual flash.
    ota = {
      enable = true;
      channel = "stable";
      autoApply = true;                # hands-off: central publish → auto-apply
    };

    # ── Persistent boot-diagnostic log partition ──
    # The live ISO's journal lives in tmpfs (RAM) — wiped on reboot, never on the
    # stick, unreadable from the Windows host. With this ON, IF a FAT32 partition
    # labelled HARTLOG is present (the flasher creates it in the stick's free
    # space after a successful flash), HART OS writes the full current-boot
    # journal + the session-supervisor tier latch/decisions + the shell-ready
    # paint marker + the GTK4/GSK/GDK/EGL/GBM/WebKit GL diagnostics to
    # /hart-boot-latest.log on it — EARLY in boot, on a ~20s periodic timer (so a
    # HUNG Tier-1 pointer-only boot STILL leaves the journal-so-far), and at
    # shutdown, fsync'ing each write. So the loop becomes: flash → boot (even if
    # Tier-1 hangs) → plug the stick into Windows → read the journal. A pure
    # NO-OP when no HARTLOG partition is present, so an old stick still boots
    # fine and the capture never blocks/slows/fails boot.
    bootLog.enable = true;

    # ── Live-OS self-creation of the HARTLOG partition ──
    # The HARTLOG partition (read by bootLog above) is now created BY THE LIVE OS
    # on first USB boot, NOT by the Windows flasher. The flasher's diskpart path
    # was doubly broken — it HUNG on a wedged Windows VDS, and a half-completed
    # `diskpart create partition` CORRUPTED a freshly-flashed stick's EFI/GPT
    # (boot failed with start_image returned 0x8000000000000001 = EFI_LOAD_ERROR).
    # With this ON, the first boot from the USB carves a FAT32 HARTLOG partition
    # into ONLY the stick's trailing free space (sgdisk --largest-new + mkfs.vfat),
    # ordered BEFORE the bootLog capture so the very first boot's journal lands on
    # it. NEVER touches the in-use ISO/EFI/boot partitions; a pure NO-OP when not
    # USB-booted, when no free space exists, when HARTLOG already exists, or on any
    # error — it can never block or fail boot. The label defaults to bootLog.label
    # so the create-side and read-side stay in lockstep.
    hartlogCreate.enable = true;

    # ── Boot continuity (return to HART OS on a Live-OS-initiated restart) ──
    # When the user restarts FROM the Live OS, set a ONE-SHOT efibootmgr BootNext
    # to the USB's OWN EFI boot entry so the next boot returns to HART OS without
    # mashing F12. It does NOT change the permanent BootOrder, so the user's
    # Windows still boots normally when chosen — only a Live-OS restart returns
    # here. A no-op if efibootmgr is missing, not UEFI-booted, or the USB entry
    # can't be matched. Intentionally BootNext (one-shot), never BootOrder, so it
    # can never strand the user's Windows boot.
    bootContinuity.enable = true;
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
    fastfetch                   # System info (neofetch successor)
    file unzip p7zip            # File tools
    wget curl                   # Network tools
    ripgrep fd bat              # Modern CLI tools (better grep/find/cat)
    tree jq                     # Directory tree / JSON processor
    mpv                         # Media backend (used by celluloid, also standalone)

    # ── GNOME Shell Extensions ──
    gnomeExtensions.dash-to-dock       # Taskbar (dock) at bottom
    gnomeExtensions.appindicator       # System tray support
    jetbrains-mono                     # Default monospace font
  ]
  # ── Remote Desktop (open-source TeamViewer equivalent) ──
  # RustDesk: ID-based P2P remote control + file transfer. gnome-connections
  # above is only an RDP/VNC *viewer*; RustDesk is the TeamViewer-style remote-
  # control client+server the OS was missing. Attr-guarded so a nixpkgs rev that
  # names it differently (rustdesk vs rustdesk-flutter) or lacks it cannot break
  # evaluation; CI's Nix Build Matrix validates the package itself builds.
  ++ lib.optional (pkgs ? rustdesk) pkgs.rustdesk
  ++ lib.optional (pkgs ? "rustdesk-flutter") pkgs."rustdesk-flutter";

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

  # ─── Touchpad: libinput tap-to-click (session-agnostic) ───
  # The dconf "org/gnome/desktop/peripherals/touchpad" tap-to-click below ONLY
  # applies to a GNOME Shell session. The shipped defaultSession is the cage
  # glass shell (services.displayManager.defaultSession = "hart-shell"),
  # which reads its pointer config straight from libinput at the seat level — so
  # tapping the touch SURFACE did nothing on the live OS while the physical
  # button still clicked (pointer + button work; Tapping was simply never
  # enabled for non-GNOME sessions). Enabling services.libinput.touchpad here
  # turns tap-to-click on for EVERY session (cage glass shell + GTK4 host + GNOME
  # fallback), not just GNOME's dconf path.
  services.libinput = {
    enable = true;
    touchpad = {
      tapping = true;            # single-finger tap = left click (THE fix)
      tappingDragLock = true;    # tap-drag stays engaged across a lift
      naturalScrolling = true;   # match the GNOME dconf natural-scroll above
      clickMethod = "clickfinger";  # 2-finger tap = right, 3 = middle
      disableWhileTyping = true; # ignore palm/stray taps while typing
    };
  };

  # GNOME Shell extensions + theming
  environment.gnome.excludePackages = with pkgs; [
    gnome-tour  # Disable first-run tour (HART has its own onboarding)
  ];
  # ─── GDM greeter branding ───
  # The login screen (first thing after Plymouth) was stock GNOME. Brand it: HART
  # logo (raster PNG — the greeter doesn't render SVG reliably) + a banner + dark
  # scheme. GDM reads its OWN dconf profile, separate from the user one below.
  # disable-user-list is intentionally NOT set: the installer 'nixos' user is
  # already hidden via uid<1000, and forcing the list off would also hide
  # hart-admin. Additive — does not touch autologin or the kiosk session.
  programs.dconf.profiles.gdm.databases = [{
    settings = {
      "org/gnome/login-screen" = {
        logo = "${hartLogoPng}/logo.png";
        banner-message-enable = true;
        banner-message-text = "HART OS — Humans are always in control";
      };
      "org/gnome/desktop/interface" = {
        color-scheme = "prefer-dark";
        gtk-theme = "Adwaita-dark";
      };
    };
  }];

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
        picture-uri = "file:///etc/hart/branding/wallpaper.svg";
        picture-uri-dark = "file:///etc/hart/branding/wallpaper.svg";
        primary-color = "#080808";
      };
      "org/gnome/desktop/screensaver" = {
        picture-uri = "file:///etc/hart/branding/lock-screen.svg";
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
        dash-max-icon-size = lib.gvariant.mkInt32 48;
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

  # GNOME Shell Extensions merged into main systemPackages list above

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
      material-icons               # Material Icons (offline icons for LiquidUI shell)
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
      # hi_IN / vi_VN have no `.UTF-8` variant in nixpkgs glibcLocales
      "hi_IN/UTF-8" "ar_SA.UTF-8/UTF-8" "ru_RU.UTF-8/UTF-8"
      "tr_TR.UTF-8/UTF-8" "th_TH.UTF-8/UTF-8" "vi_VN/UTF-8"
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
    user = lib.mkForce "hart-admin";
  };

  # ─── Hide the NixOS live-installer user (NixOS must be invisible) ───
  # installation-cd-graphical-gnome.nix (imported above) injects a NORMAL
  # `nixos` user (uid 1000) plus its own auto-login. We auto-login to
  # hart-admin (above); here we demote `nixos` to a hidden SYSTEM account
  # (uid < 1000) so GDM never lists it in the greeter, and we drop the TTY
  # auto-login so a Ctrl+Alt+F-key never lands on "nixos" either. Android
  # hides Linux from its users; HART OS hides NixOS the same way.
  users.users.nixos = lib.mkForce {
    isSystemUser = true;
    group = "nixos";
  };
  users.groups.nixos = lib.mkForce {};
  services.getty.autologinUser = lib.mkForce null;

  # ─── Recovery consoles: Ctrl+Alt+F2..F6 ALWAYS reach a TTY ───────────────────
  # The "only a mouse pointer, no desktop, and Ctrl+Alt+F2 does nothing" boot
  # regression had no recovery path: the graphical session held VT1 with a hung
  # shell host and the user could not reach a console. This block guarantees a
  # login console is ALWAYS reachable, independent of the graphical session's
  # health, so a stuck compositor can never trap the machine.
  #
  # 1. Keep getty ENABLED (NixOS default). We only nulled the TTY AUTOLOGIN above
  #    so a Ctrl+Alt+F-key never lands on the hidden `nixos` user — getty itself
  #    still runs on tty1..tty6. We assert the default `console` framework stays on
  #    so a future kiosk tweak can't silently disable the virtual terminals.
  #    `quiet`/`splash` in boot.kernelParams do NOT affect VT switching.
  console.enable = lib.mkDefault true;
  #
  # 2. VT switching is a kernel + systemd-logind seat function: Ctrl+Alt+Fn asks
  #    logind to activate the target VT (logind spawns autovt@ttyN on demand).
  #    We rely on the stock NAutoVTs=6 (NixOS/logind default) so the seat can
  #    switch to tty2..tty6 — we do NOT override it (writing the same default via
  #    extraConfig/settings only risks an option-name mismatch for zero gain). The
  #    hung GRAPHICAL session cannot veto a kernel VT switch — logind owns the
  #    seat, not the compositor — so this is the reliable escape when tier-2 hangs.
  #    Nothing in this config sets a logind option that would refuse VT switching.
  #
  # 3. Belt-and-suspenders: pre-spawn a getty on tty2 from boot so a recovery
  #    console is ALREADY alive (not summoned lazily) the instant the user
  #    switches to it — recovery never depends on logind's on-demand autovt spawn
  #    working while the graphical session is wedged. `autovt@tty2` is the exact
  #    unit logind would itself start on a switch to VT2 (NixOS aliases autovt@ to
  #    the getty@ template), so pinning THIS one instance to multi-user.target
  #    cannot collide with logind's seat management — it is the same unit logind
  #    uses, merely started eagerly. (tty3..tty6 stay on-demand via NAutoVTs.)
  systemd.services."autovt@tty2".wantedBy = [ "multi-user.target" ];

  # ─── Boot session = the SUPERVISOR-MANAGED TIER LADDER (not a fixed default) ───
  # The crude fixed cage-pin (68ce3c3: `services.displayManager.defaultSession =
  # lib.mkForce "hart-shell"`) is REMOVED in favour of the real tiered design the
  # architecture mandates. With hart.sessionSupervisor.enable = true (set in the
  # hart block above), greetd REPLACES GDM and runs the tier-drop SELECTOR as the
  # boot session — so the supervisor, not a fixed defaultSession, owns which tier
  # boots:
  #
  #   Tier-1 hart-comp (Smithay/Rust, --backend drm; the START tier)
  #     → Tier-2 sway   (the hart-glass-gtk4 layer-shell session, wired below)
  #       → Tier-3 cage (hart-shell, the audited never-fail paint floor — the
  #                       supervisor can NEVER drop below it).
  #
  # The ladder tries the BEST tier first and DROPS one rung on a real failure —
  # a crash OR the shell-paint watchdog firing (compositor up but no first frame
  # within shellPaintTimeoutSeconds; the "only-a-pointer" hang ff02e48 exposed).
  # A drop LATCHES across boot; `hartctl session reset-tier` re-arms Tier-1. The
  # supervisor's config sets `defaultSession = "hart-shell"` for the floor, which
  # is moot under greetd's command model — greetd runs the selector, not a named
  # session. So we do NOT (and must not) ALSO mkForce defaultSession here (two
  # equal-priority mkForces would collide); the supervisor block owns it.
  #
  # HONEST HW CAVEAT: the GTK4 glass-shell host that Tier-1 + Tier-2 share still
  # HANGS on real hardware (the pointer-only first-paint bug). Until that is
  # fixed, a real boot will TRY Tier-1 (hart-comp), likely hang ~shellPaint
  # seconds, DROP to Tier-2 (sway, same host, hang again), then DROP to Tier-3
  # cage (the GTK3 host that paints). The ladder degrades safely to the proven
  # floor — it is never a blank screen — but Tier-1/Tier-2 only become the live
  # session once the GTK4 host's on-HW paint is fixed (needs the on-HW journal,
  # now reachable via the recovery TTY added in b97f1ae).
  hart.layerShellHost.enable = true;
  # Tier-2 = the GTK4 layer-shell glass host UNDER sway (the `hart-glass-gtk4`
  # session), not bare sway. The layer-shell-host module repoints the supervisor's
  # swayCommand to its `hart-glass-shell-gtk4-session` launcher (mkOverride, so it
  # wins over both the bare-sway option default and swayTier1's mkDefault). This
  # gives Tier-2 a TRUE layer-shell desktop running the same glass host as Tier-1.

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

  # ─── HART OS Branding (real assets, not placeholders) ───
  # SVG sources live in nixos/branding/.  GNOME renders SVG wallpapers directly
  # (dconf above points here); Plymouth gets the rasterized logo (hartLogoPng).
  # The mark: a geometric heart with circuit-board traces — human compassion +
  # machine intelligence — in #00D4AA (HART teal) on dark #080808.
  environment.etc = {
    "hart/branding/wallpaper.svg".source = ../branding/hart-wallpaper.svg;
    "hart/branding/lock-screen.svg".source = ../branding/hart-wallpaper.svg;
    "hart/branding/logo.svg".source = ../branding/hart-logo.svg;
  };

  # ─── Boot splash: HART logo, not the NixOS lizard ───
  # Uses NixOS's default Plymouth theme but swaps in our logo via
  # boot.plymouth.logo (low-risk — no custom theme module).  quiet+splash hide
  # the kernel/systemd text boot behind the graphical splash.
  boot.plymouth = {
    enable = lib.mkForce true;  # base installer-CD profile may also set this
    logo = lib.mkForce "${hartLogoPng}/logo.png";
  } // lib.optionalAttrs useCustomBootSplash {
    # Opt-in only (useCustomBootSplash above). Off ⇒ {} ⇒ this merge is a no-op
    # and boot.plymouth is byte-identical to the proven stock-theme-plus-logo.
    themePackages = [ hartPlymouth ];
    theme = lib.mkForce "hart";
  };
  boot.kernelParams = [ "quiet" "splash" ];
}
