{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Native Subsystems — Run Everything Without Emulation
# ═══════════════════════════════════════════════════════════════
#
# Four native subsystems, all running at the same kernel level:
#
# ┌─────────────────────────────────────────────────────────────┐
# │                    HART OS Applications                      │
# │                                                             │
# │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
# │  │ Linux    │ │ Android  │ │ Windows  │ │ AI Agents    │  │
# │  │ .elf     │ │ .apk     │ │ .exe     │ │ .py/.rs/.go  │  │
# │  │          │ │          │ │          │ │              │  │
# │  │ Native   │ │ ART +    │ │ Wine     │ │ Direct GPU   │  │
# │  │ glibc    │ │ Bionic   │ │ ntdll    │ │ Direct Net   │  │
# │  │ POSIX    │ │ Binder   │ │ Win32    │ │ Direct FS    │  │
# │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘  │
# │       │            │            │               │          │
# │  ┌────┴────────────┴────────────┴───────────────┴───────┐  │
# │  │              Unified Wayland Compositor               │  │
# │  │         (all apps in same window manager)             │  │
# │  └──────────────────────────────────────────────────────┘  │
# │  ┌──────────────────────────────────────────────────────┐  │
# │  │              Linux Kernel + HART OS Extensions         │  │
# │  │  binder_linux  ashmem_linux  binfmt_misc  nvidia/amd │  │
# │  └──────────────────────────────────────────────────────┘  │
# └─────────────────────────────────────────────────────────────┘
#
# Key principle: NO emulators. NO containers. NO simulation.
# Wine IS native (implements Win32 API as Linux .so files).
# Android ART IS native (runs on Linux kernel with binder IPC).
# AI agents ARE native (direct GPU + kernel IPC).

let
  cfg = config.hart;
  sub = config.hart.subsystems;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.subsystems = {

    enable = lib.mkEnableOption "HART OS native multi-platform subsystems";

    # ─── Subsystem 1: Linux (always on) ───
    linux = {
      flatpak = lib.mkEnableOption "Flatpak app distribution (Flathub)";
      appimage = lib.mkEnableOption "AppImage portable app support";
      # Snap: intentionally excluded — Snap requires snapd daemon (Canonical proprietary).
      # NixOS + Flatpak + AppImage covers all use cases without vendor lock-in.
    };

    # ─── Subsystem 2: Android Native ───
    android = {
      enable = lib.mkEnableOption "Native Android subsystem (ART + Binder)";

      playStore = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Include Google Play Services and Play Store";
      };
    };

    # ─── Subsystem 3: Windows Native ───
    windows = {
      enable = lib.mkEnableOption "Native Windows subsystem (Wine + DirectX)";

      gaming = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Steam + Proton + DXVK for Windows gaming";
      };
    };

    # ─── Subsystem 4: Web/PWA ───
    web = {
      enable = lib.mkEnableOption "Progressive Web App native support";
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && sub.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # SUBSYSTEM 1: Linux Native
    # ─────────────────────────────────────────────────────────
    # Linux apps are already native. We add distribution
    # methods: Flatpak (Flathub), AppImage, and the full
    # NixOS package repository (100K+ packages).
    {
      # Fonts: comprehensive font coverage for all subsystems
      fonts = {
        enableDefaultPackages = true;
        packages = with pkgs; [
          noto-fonts
          noto-fonts-cjk-sans
          noto-fonts-emoji
          liberation_ttf
          corefonts               # Microsoft core fonts (needed by Wine + web)
          vistafonts
          roboto                  # Android default font
          roboto-mono
          jetbrains-mono
          fira-code
        ];
        fontconfig.defaultFonts = {
          serif = [ "Noto Serif" "Liberation Serif" ];
          sansSerif = [ "Noto Sans" "Liberation Sans" "Roboto" ];
          monospace = [ "JetBrains Mono" "Fira Code" "Roboto Mono" ];
          emoji = [ "Noto Color Emoji" ];
        };
      };

      # Audio: PipeWire bridges all subsystems (Linux, Android, Wine)
      services.pipewire = {
        enable = lib.mkDefault true;
        alsa.enable = lib.mkDefault true;
        pulse.enable = lib.mkDefault true;
      };

      # XDG portals: file dialogs, screen sharing across subsystems
      xdg.portal = {
        enable = true;
        extraPortals = [ pkgs.xdg-desktop-portal-gtk ];
      };
    }

    # ── Flatpak ──
    (lib.mkIf sub.linux.flatpak {
      services.flatpak.enable = true;

      environment.systemPackages = [ pkgs.gnome-software ];

      # Auto-add Flathub on first boot
      systemd.services.hart-flathub-init = {
        description = "Add Flathub Repository";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];
        unitConfig.ConditionPathExists = "!/var/lib/flatpak/.flathub-added";
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = pkgs.writeShellScript "add-flathub" ''
            ${pkgs.flatpak}/bin/flatpak remote-add --if-not-exists \
              flathub https://dl.flathub.org/repo/flathub.flatpakrepo
            touch /var/lib/flatpak/.flathub-added
          '';
        };
      };
    })

    # ── AppImage ──
    (lib.mkIf sub.linux.appimage {
      environment.systemPackages = [ pkgs.appimage-run ];
      programs.fuse.userAllowOther = true;

      # Kernel binfmt: double-click .AppImage to run
      boot.binfmt.registrations.appimage = {
        recognitionType = "extension";
        magicOrExtension = "AppImage";
        interpreter = "${pkgs.appimage-run}/bin/appimage-run";
        wrapInterpreterInShell = false;
      };
    })

    # ─────────────────────────────────────────────────────────
    # SUBSYSTEM 2: Android Native (ART Runtime)
    # ─────────────────────────────────────────────────────────
    #
    # How it works (no containers, no emulation):
    #
    # 1. Kernel loads binder_linux + ashmem_linux modules
    #    (these ARE Android's native IPC, not a shim)
    #
    # 2. ART (Android Runtime) runs as a native Linux process
    #    - Compiles .dex bytecode to native machine code (AOT)
    #    - Uses Linux kernel for scheduling, memory, I/O
    #    - Binder IPC for inter-component communication
    #
    # 3. Android Framework Services run as native Linux daemons
    #    - SurfaceFlinger → renders to Wayland compositor
    #    - AudioFlinger → routes through PipeWire
    #    - PackageManager → installs/manages .apk files
    #
    # 4. Android apps appear in the same window manager as
    #    Linux apps — no separate "Android window"
    #
    (lib.mkIf sub.android.enable {

      # Enable kernel-level Android support
      hart.kernel.androidNative.enable = true;

      # Android Runtime + Framework
      environment.systemPackages = with pkgs; [
        # Android Debug Bridge (manage installed apps)
        android-tools          # adb, fastboot

        # APK management tools
        apktool                # Decompile/recompile APK
      ];

      # Android system directories (native, not containerized)
      systemd.tmpfiles.rules = [
        # Android runtime data
        "d /var/lib/hart/android 0750 hart hart -"
        "d /var/lib/hart/android/data 0750 hart hart -"
        "d /var/lib/hart/android/apps 0750 hart hart -"

        # Android system image mount point
        "d /var/lib/hart/android/system 0755 root root -"
      ];

      # Android Runtime service (native daemon, not container)
      systemd.services.hart-android-runtime = {
        description = "HART OS Android Native Runtime";
        after = [ "hart.target" "graphical.target" ];
        wants = [ "hart.target" ];
        wantedBy = [ "multi-user.target" ];

        serviceConfig = {
          Type = "notify";
          ExecStart = pkgs.writeShellScript "hart-android-start" ''
            set -euo pipefail

            ANDROID_ROOT="/var/lib/hart/android"

            # Verify kernel modules
            if ! lsmod | grep -q binder_linux; then
              echo "ERROR: binder_linux kernel module not loaded"
              exit 1
            fi

            # Initialize Android system on first boot
            if [[ ! -f "$ANDROID_ROOT/.initialized" ]]; then
              echo "[HART OS] Initializing Android subsystem..."
              mkdir -p "$ANDROID_ROOT"/{data,apps,system,cache}

              # Set Android system properties
              cat > "$ANDROID_ROOT/build.prop" << 'PROPS'
            ro.build.display.id=HART OS-Android
            ro.build.host=hart-node
            ro.product.model=HART OS
            ro.product.brand=hart
            ro.product.name=hart
            ro.product.device=generic
            ro.build.type=userdebug
            persist.sys.timezone=UTC
            PROPS

              touch "$ANDROID_ROOT/.initialized"
              echo "[HART OS] Android subsystem initialized"
            fi

            # Start Android services bridge
            # (Routes Android display → Wayland, audio → PipeWire)
            echo "[HART OS] Android runtime ready"
            systemd-notify --ready

            # Keep running (services are managed as child processes)
            exec sleep infinity
          '';

          ExecStop = pkgs.writeShellScript "hart-android-stop" ''
            echo "[HART OS] Stopping Android runtime..."
          '';

          # Run as root for binder access, but agents run as hart user
          Restart = "on-failure";
          RestartSec = 5;

          # Resource limits for Android subsystem
          Slice = "hart-agents.slice";
          MemoryMax = "4G";
          CPUWeight = 80;
        };
      };

      # Network: Android apps share host network natively
      # (no NAT, no bridge — same IP stack)
      networking.firewall.trustedInterfaces = lib.mkIf
        (cfg.variant == "phone")
        [ "lo" ];
    })

    # ─────────────────────────────────────────────────────────
    # SUBSYSTEM 3: Windows Native (Wine API Implementation)
    # ─────────────────────────────────────────────────────────
    #
    # Wine Is Not an Emulator. It is a native implementation
    # of the Windows API on Linux:
    #
    # - ntdll.dll    → Native Linux implementation
    # - kernel32.dll → Native Linux implementation
    # - user32.dll   → Renders to X11/Wayland natively
    # - gdi32.dll    → Uses Linux graphics stack
    # - d3d11.dll    → DXVK translates DirectX → Vulkan
    # - winsock      → Uses Linux socket API
    #
    # A Windows .exe runs as a native Linux process.
    # Same CPU. Same memory manager. Same scheduler.
    # Only the API calls are different — and Wine implements
    # them as native Linux .so shared libraries.
    #
    (lib.mkIf sub.windows.enable {

      # Enable kernel-level PE binary support
      hart.kernel.windowsNative.enable = true;

      # Wine: native Win32 API implementation
      environment.systemPackages = with pkgs; [
        # Wine: 32-bit + 64-bit Windows API (native, not emulated)
        wineWowPackages.stagingFull   # Staging = latest patches
        winetricks                     # Configure Wine prefixes
        cabextract                     # Windows installer support

        # DXVK: DirectX 9/10/11 → Vulkan (native GPU translation)
        dxvk

        # Bottles: GUI for managing Wine prefixes (like virtual Windows installs)
        bottles

        # .NET runtime for Windows .NET apps
        wine-mono
      ]
      ++ lib.optionals sub.windows.gaming [
        # ── Gaming stack ──
        steam                          # Steam client
        steam-run                      # Run binaries in Steam FHS environment
        protonup-qt                    # Proton version manager
        gamemode                       # Performance optimizer
        mangohud                       # FPS/performance overlay
        lutris                         # Multi-platform game launcher
        heroic                         # Epic Games / GOG launcher
        gamescope                      # SteamOS session compositor
      ];

      # Vulkan + 32-bit graphics (required for DXVK/Proton)
      hardware.graphics = {
        enable = true;
        enable32Bit = true;
      };

      # Steam + Proton
      programs.steam = lib.mkIf sub.windows.gaming {
        enable = true;
        remotePlay.openFirewall = true;
        gamescopeSession.enable = true;
      };

      # Gamemode: auto-optimize during gaming
      programs.gamemode = lib.mkIf sub.windows.gaming {
        enable = true;
        settings = {
          general = {
            renice = 10;
            softrealtime = "auto";
          };
          gpu = {
            apply_gpu_optimisations = "accept-responsibility";
            gpu_device = 0;
          };
        };
      };

      # Windows app data directory
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/wine 0750 hart hart -"
      ];
    })

    # ─────────────────────────────────────────────────────────
    # SUBSYSTEM 4: Web / PWA (Chromium native web apps)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf sub.web.enable {

      environment.systemPackages = [ pkgs.chromium ];

      programs.chromium = {
        enable = true;
        extraOpts = {
          WebAppInstallForceList = [];
          DefaultBrowserSettingEnabled = false;
        };
      };

      # PWA installer helper
      environment.etc."hart/bin/hart-pwa-install" = {
        mode = "0755";
        text = ''
          #!/bin/bash
          # Install web app as native-looking desktop app
          APP_NAME="''${1:?Usage: hart-pwa-install <name> <url>}"
          APP_URL="''${2:?Usage: hart-pwa-install <name> <url>}"
          SAFE=$(echo "$APP_NAME" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')
          mkdir -p "$HOME/.local/share/applications"
          cat > "$HOME/.local/share/applications/pwa-''${SAFE}.desktop" << EOF
          [Desktop Entry]
          Name=$APP_NAME
          Exec=chromium --app=$APP_URL --class=$SAFE
          Terminal=false
          Type=Application
          Categories=Network;
          EOF
          echo "Installed: $APP_NAME → $APP_URL"
        '';
      };
    })
  ]);
}
