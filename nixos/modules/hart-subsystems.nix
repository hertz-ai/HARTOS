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

    # ─── Subsystem 4: Web/PWA + browser extensions ───
    web = {
      enable = lib.mkEnableOption "Progressive Web App native support";

      # Browser-extension (.crx / .xpi) FORCE-INSTALL via the bundled browser's
      # own enterprise managed-policy engine. This is a REAL install into a real
      # browser (Chromium ExtensionInstallForcelist / Firefox ExtensionSettings),
      # DISTINCT from HART's in-process .hartpkg extension registry. Opt-in so the
      # writable managed-policy dir + Firefox enterprise policy file only exist
      # when the operator wants extension force-install (the AppInstaller's
      # _install_browser_ext writes into them and verifies on disk).
      allowExtensions = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Expose the bundled browser's managed-policy extension force-install
          surface: a writable Chromium managed-policy dir
          (/etc/chromium/policies/managed) and, if a Firefox is shipped, a
          Firefox enterprise policy file (/etc/firefox/policies/policies.json).
          The AppInstaller._install_browser_ext (InstallerPlatform.BROWSER_EXT)
          writes the extension id+update_url into these and confirms by reading
          the on-disk policy back. Default off.
        '';
      };
    };

    # ─── Subsystem 5: macOS (Darling — experimental) ───
    macos = {
      enable = lib.mkEnableOption "Experimental macOS app support (Darling — Mach-O/Darwin translation, the macOS analogue of Wine)";
    };

    # ─── Subsystem 6: Snap — INTENTIONALLY ABSENT (see note) ───
    # There is NO hart.subsystems.snap.enable option, on purpose.
    #
    # Native snapd is INFEASIBLE on this image: snapd hard-codes an FHS /snap +
    # /var/lib/snapd tree, generates AppArmor profiles at runtime, and conflicts
    # with the Nix store model. nixpkgs ships no first-class snapd; the only real
    # path is the third-party `nix-snapd` flake (services.snap.enable + a /snap
    # FHS bind mount), which this flake does NOT bundle — flake.nix pins nixpkgs
    # to a fixed commit (50ab793) with no snapd input and no FHS /snap. Adding it
    # would mean a new, unpinned, supply-chain-unvetted flake input — a steward
    # decision, not a silent module (no-fork / no-fake-module rule).
    #
    # Honest contract: the AppInstaller routes .snap to InstallerPlatform.SNAP
    # and returns success=False with "Snap is not supported on HART OS — install
    # the Flatpak / AppImage / Nix equivalent". /platforms reports snap as
    # available=False so the UI can grey it out honestly. We do NOT ship a fake
    # module that pretends to install snaps. Most Snap apps have a Flathub or
    # nixpkgs equivalent (linux.flatpak / the Nix repo), which is the fallback.
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
          # ── Glass shell typography + icons, bundled LOCALLY ──
          # liquid_ui_service.py renders the shell with Inter (body) and the
          # Material Icons ligature font for EVERY top-bar/tray icon. Those were
          # loaded from fonts.googleapis.com, so a fresh OFFLINE ISO boot showed
          # literal "lock"/"notifications" words instead of icons (and phoned
          # Google on every launch). Bundling them makes the shell render fully
          # offline + private. (A real OS ships its own fonts.)
          inter                   # Shell body font (--ds-font-body)
          material-icons          # Material Icons ligature font (.mi icons)
          material-symbols        # Newer Material Symbols (forward-compat)
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

      # Auto-add Flathub — EVENT-DRIVEN, not polled.
      #
      # 2026-07-24 real-HW bug (nightly-0d7c84f journal): on the potato node,
      # network-online.target fired ~5 min BEFORE DNS was actually reachable, so
      # `remote-add` failed with "Could not resolve hostname". The old script then
      # `touch`ed the success marker UNCONDITIONALLY, so ConditionPathExists
      # permanently blocked any retry -> Flathub was never added and every App
      # Store install failed ("flatpak not installed" / Retry) until the marker was
      # cleared by hand.
      #
      # The unit makes ONE attempt and writes the marker ONLY on success. It does
      # NOT poll: recovery is driven by the actual connectivity EVENT — a
      # NetworkManager dispatcher (below) starts this oneshot each time an
      # interface comes up / gets a lease / connectivity changes. ConditionPathExists
      # makes a re-start a clean no-op once the remote is added, and re-runs
      # remote-add if the boot-time attempt failed because DNS wasn't up yet. No
      # timer, no sleep loop, no arbitrary give-up window.
      systemd.services.hart-flathub-init = {
        description = "Add Flathub Repository";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];
        # Only skip once the remote is ACTUALLY added (marker written on success).
        unitConfig.ConditionPathExists = "!/var/lib/flatpak/.flathub-added";
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = pkgs.writeShellScript "add-flathub" ''
            set -u
            # remote-add fetches the .flatpakrepo (URL + GPG key), so it needs DNS.
            # ONE attempt; the marker is written ONLY on success, so a transient
            # DNS failure records nothing and the connectivity-event dispatcher
            # re-runs this later (and next boot too, marker absent).
            if ${pkgs.flatpak}/bin/flatpak remote-add --if-not-exists \
                 flathub https://dl.flathub.org/repo/flathub.flatpakrepo; then
              mkdir -p /var/lib/flatpak
              touch /var/lib/flatpak/.flathub-added
              echo "[hart-flathub-init] flathub remote added"
              exit 0
            fi
            echo "[hart-flathub-init] remote-add failed: network/DNS not ready; the NetworkManager dispatcher will re-run this on the next connectivity event" >&2
            exit 1
          '';
        };
      };

      # The connectivity EVENT source. NetworkManager runs this on every
      # interface up / DHCP lease / connectivity-state change ($1=iface, $2=action).
      # When the network genuinely arrives (which on this node was ~5 min after
      # network-online.target), it (re)starts hart-flathub-init exactly once per
      # event — the missing "internet is here now" signal that a boot-time oneshot
      # alone never gets. `--no-block` returns immediately; the oneshot's
      # ConditionPathExists guarantees at-most-once real work.
      networking.networkmanager.dispatcherScripts = [{
        type = "basic";
        source = pkgs.writeShellScript "flathub-on-connectivity" ''
          # Fire on the connectivity events, but ONLY act once the internet is
          # ACTUALLY reachable. The bug this fixes: the dispatcher re-ran
          # hart-flathub-init on `up` / dhcp-lease at ~+2 min, while DNS still
          # could not resolve, so remote-add hit "Could not resolve hostname"
          # AGAIN and gave up — event-driven, but on the wrong event (an
          # interface having a lease is not the same as the internet being
          # reachable). NetworkManager's own connectivity check answers the
          # real question: gate on CONNECTIVITY=full so we only attempt
          # remote-add when a captive-portal-free, DNS-resolving path exists.
          case "$2" in
            up|connectivity-change|dhcp4-change|dhcp6-change)
              if [ "$(${pkgs.networkmanager}/bin/nmcli -t -f CONNECTIVITY general status 2>/dev/null)" = "full" ]; then
                ${pkgs.systemd}/bin/systemctl start --no-block hart-flathub-init.service || true
              fi
              ;;
          esac
        '';
      }];
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

      # ── Eval-LOUD prerequisite ─────────────────────────────────────────
      # Waydroid runs a real AOSP container that needs Binder IPC, which lives
      # in the kernel module (hart.kernel.androidNative -> binder_linux/ashmem/
      # binderfs + the /dev/binder* udev rules GROUP=hart). Enabling Android
      # without the kernel bits would produce an INERT runtime (the old lie).
      # Assert it so a misconfig fails eval LOUDLY instead of booting dead.
      assertions = [
        {
          assertion = cfg.kernel.enable && cfg.kernel.androidNative.enable;
          message = ''
            hart.subsystems.android.enable requires the kernel Binder bits:
            set hart.kernel.enable = true AND
            hart.kernel.androidNative.enable = true (binder_linux + ashmem +
            binderfs + /dev/binder* udev rules). Waydroid cannot run without
            Binder IPC. (This module enables androidNative for you when
            hart.kernel.enable is already on; the assertion guards the case
            where the kernel master toggle is off.)
          '';
        }
      ];

      # Enable kernel-level Android support (binder/ashmem/binderfs). Harmless
      # if already set in the variant config; the assertion above still catches
      # hart.kernel.enable = false (which gates the whole kernel module merge).
      hart.kernel.androidNative.enable = true;

      # ── REAL Android runtime: Waydroid (LXC-based AOSP container) ────────
      # NOT the old `exec sleep infinity` stub (which ran NO ART, NO Binder, NO
      # PackageManager and installed nothing while reporting "ready"). Waydroid
      # runs a genuine AOSP image with ART + Bionic + Binder on the HOST kernel;
      # SurfaceFlinger -> Wayland, AudioFlinger -> PipeWire. We hand the
      # container lifecycle to the STOCK nixpkgs `virtualisation.waydroid` module
      # (its own waydroid-container.service owns the LXC) rather than keeping a
      # parallel inert daemon — one runtime, no drift.
      #
      # IMPORTANT (eval-gate lesson #1): Waydroid raises inotify watch pressure,
      # but we do NOT re-declare fs.inotify.max_user_watches here — hart-kernel
      # already forces it to 1048576 with lib.mkForce. A second declaration would
      # collide at eval ("defined multiple times"). Don't touch the sysctl.
      virtualisation.waydroid.enable = true;

      # Android Runtime + Framework tooling (manage installed apps from the host)
      environment.systemPackages = with pkgs; [
        # Android Debug Bridge (inspect / manage the running session)
        android-tools          # adb, fastboot
        # APK management / inspection (the installer parses the package name from
        # the APK manifest; aapt-style inspection is handy for operators too)
        apktool                # Decompile/recompile APK
      ];

      # Android runtime data dir (the installer's honest "where APKs land" note;
      # Waydroid keeps its own state under /var/lib/waydroid). Kept for operator
      # scratch + parity with the other subsystems' /var/lib/hart/<x> dirs.
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/android 0750 hart hart -"
      ];

      # ── First-boot Waydroid image init — NEVER-FAIL boot ordering ───────
      # `waydroid init` downloads the system + vendor images (network-dependent).
      # Per eval-gate lesson #3 (the graphical.target ordering-cycle the old
      # Android runtime learned): this is a plain hart.target child, Type=oneshot
      # + RemainAfterExit, ConditionPathExists guard for idempotency, and it MUST
      # NOT block boot if there is no network / the download fails — every step
      # is `|| true` (mirrors hart-flathub-init's tolerance). A dead init must
      # never wedge the boot; the installer surfaces "session not started"
      # honestly at install time instead.
      systemd.services.hart-waydroid-init = {
        description = "HART OS — Waydroid first-boot image init (non-blocking)";
        # network-online is WANTED (best-effort) not REQUIRED — no-network boots
        # must still complete; the init just no-ops and retries on a later boot.
        after = [ "hart.target" "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "hart.target" ];
        # Idempotent: skip once the images are present.
        unitConfig.ConditionPathExists = "!/var/lib/waydroid/images/system.img";
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          # Downloads can be slow; cap so a hung mirror can't pin the unit
          # forever, and never let a failure fail the boot transaction.
          TimeoutStartSec = "600";
          ExecStart = pkgs.writeShellScript "hart-waydroid-init" ''
            set -uo pipefail
            echo "[HART OS] Waydroid first-boot init (best-effort, non-blocking)..."
            # ${if sub.android.playStore then "GApps" else "vanilla"} system image.
            ${pkgs.waydroid}/bin/waydroid init \
              ${lib.optionalString sub.android.playStore "-s GAPPS"} || \
              echo "[HART OS] waydroid init deferred (no network / mirror down) — will retry next boot"
            exit 0
          '';
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

        # .NET runtime for Windows .NET apps (bundled in Wine staging)
      ]
      ++ lib.optionals sub.windows.gaming [
        # ── Gaming stack ──
        steam                          # Steam client
        steam-run                      # Run binaries in Steam FHS environment
        protonup-qt                    # Proton version manager
        gamemode                       # Performance optimizer
        mangohud                       # FPS/performance overlay
        lutris                         # Multi-platform game launcher
        gamescope                      # SteamOS session compositor

        # heroic (Epic/GOG launcher) is NOT preinstalled — it is in the App Store
        # catalog (hart-app-catalog.json, Games) and installs in one click.
        #
        # It cost 1.4 GiB of every image and every OTA, and most of that was not
        # Heroic. `nix why-depends` on the shipped closure (run 30356487925):
        #
        #   system-path -> heroic -> heroic-bwrap -> heroic-fhsenv-rootfs
        #     -> kdialog -> kinit-dev -> kiconthemes-dev -> qttools-dev
        #       -> clang-18.1.8-lib          (775 MiB)
        #
        # Three -dev outputs in a RUNTIME closure, ending at libclang — which is
        # there because Qt's qdoc links it to build documentation. Nothing on this
        # machine will ever run qdoc. buildFHSEnv is not at fault (it installs only
        # out/lib/bin); kdialog's own out output carries the reference, an upstream
        # KDE packaging bug HART OS was silently inheriting. On top of that came
        # Electron (261 MiB) and a whole KDE/Qt5 widget stack (~300 MiB) pulled into
        # a desktop that ships no KDE.
        #
        # Dropping kdialog from the FHS would mean vendoring nixpkgs' fhsenv.nix,
        # which is a parallel path that drifts on every nixpkgs bump (Gate 4). And
        # Heroic loses nothing here: its targetPkgs already carry `zenity`, the GTK
        # dialog helper it uses on a GTK desktop like this one.
        #
        # Steam, Lutris, gamescope, gamemode, mangohud and the whole Wine/DXVK/
        # Bottles layer are UNCHANGED — this removes one launcher from the base
        # image, not gaming support.
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

      # Eval-LOUD prerequisite: the policy install surface needs a real browser
      # pkg. chromium is the canonical force-install target (the PWA + extension
      # engine); assert it exists on the chosen nixpkgs rev/arch so an unbuildable
      # browser fails eval instead of producing an inert /platforms entry.
      assertions = [
        {
          assertion = pkgs ? chromium;
          message = ''
            hart.subsystems.web.enable needs a browser package: pkgs.chromium is
            absent on this nixpkgs rev/arch, so the PWA + extension force-install
            surface cannot be built. Pin a rev where chromium builds, or disable
            hart.subsystems.web.
          '';
        }
      ];

      environment.systemPackages = [ pkgs.chromium ];

      programs.chromium = {
        enable = true;
        extraOpts = {
          WebAppInstallForceList = [];
          DefaultBrowserSettingEnabled = false;
          # Browser-extension FORCE-INSTALL list (Chromium enterprise policy).
          # programs.chromium writes extraOpts to /etc/chromium/policies/managed
          # as a managed JSON policy; the AppInstaller._install_browser_ext
          # appends {id, update_url} entries to this list (or, when allowExtensions
          # exposes a writable managed dir below, drops a sibling policy file) and
          # CONFIRMS by reading the on-disk policy back. Default empty -> no forced
          # extensions until something writes one. KEPT distinct from HART's own
          # in-process .hartpkg EXTENSION registry (different InstallerPlatform).
          ExtensionInstallForcelist = [];
        };
      };

      # ── /etc files: PWA helper (always) + writable managed-policy surface ──
      # ONE environment.etc binding (two `environment.etc = …` in the same attrset
      # would collide). The PWA helper is unconditional; the extension force-install
      # policy files are added only when hart.subsystems.web.allowExtensions is on,
      # via lib.optionalAttrs (so the disabled path ships no writable policy dir).
      #
      # programs.chromium's own managed policy file is in the read-only Nix store.
      # The installer needs a WRITABLE file Chromium ALSO reads so it can drop a
      # {id,update_url} force-install policy at runtime + verify it on disk:
      # /etc/chromium/policies/managed merges every *.json there. For a bundled
      # Firefox we expose /etc/firefox/policies/policies.json (the Firefox
      # enterprise policy path, same pattern as hart-email.nix's thunderbird
      # policy). Both stock surfaces — no fork.
      environment.etc = {
        # PWA installer helper (always present when web is enabled)
        "hart/bin/hart-pwa-install" = {
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
      } // lib.optionalAttrs sub.web.allowExtensions {
        # Chromium: a writable, installer-owned managed-policy file alongside the
        # store-managed one. mode 0664 + group hart so the installer (running as
        # hart) can rewrite the ExtensionInstallForcelist and read it back.
        "chromium/policies/managed/hart-extensions.json" = {
          mode = "0664";
          group = "hart";
          text = builtins.toJSON { ExtensionInstallForcelist = [ ]; };
        };
        # Firefox: enterprise policy file (Firefox reads /etc/firefox/policies/
        # policies.json). ExtensionSettings is the .xpi force-install surface
        # (installation_mode=force_installed + install_url). Seed empty; installer
        # merges entries + verifies on disk. Mirrors hart-email.nix's TB policy.
        "firefox/policies/policies.json" = {
          mode = "0664";
          group = "hart";
          text = builtins.toJSON { policies = { ExtensionSettings = { }; }; };
        };
      };
    })

    # ─────────────────────────────────────────────────────────
    # SUBSYSTEM 5: macOS Native (Darling) — EXPERIMENTAL, opt-in
    # ─────────────────────────────────────────────────────────
    # Darling is the open-source Mach-O / Darwin translation layer — the macOS
    # analogue of Wine (NOT an emulator: it implements the Darwin syscall ABI +
    # frameworks as native Linux libraries). It is x86_64-only and today runs
    # mostly CLI + some GUI macOS binaries; maturity is well below Wine, so this
    # stays default-OFF and never touches the default desktop build. The
    # `pkgs ? darling` guard keeps evaluation safe on a nixpkgs rev/arch where
    # darling is absent or unbuildable.
    (lib.mkIf sub.macos.enable {
      # Eval-LOUD prerequisite (only when macos is explicitly enabled — it is
      # default-OFF, so the common case never hits this). Darling is absent /
      # unbuildable on many revs+arches (x86_64-only, heavy build); if an operator
      # opts in on such a rev, fail eval with an actionable message rather than
      # silently shipping no runtime. The `pkgs ? darling` optional below still
      # keeps the DISABLED path eval-safe everywhere.
      assertions = [
        {
          assertion = pkgs ? darling;
          message = ''
            hart.subsystems.macos.enable is on but pkgs.darling is absent on this
            nixpkgs rev/arch (Darling is x86_64-only and not always packaged).
            macOS app support (experimental, CLI-leaning) cannot be built here —
            pin a rev/arch where darling builds, or leave hart.subsystems.macos
            disabled (the default). GUI / .dmg / .pkg remain unsupported regardless
            (the installer refuses them honestly via `darling shell`).
          '';
        }
      ];
      environment.systemPackages = lib.optional (pkgs ? darling) pkgs.darling;
      systemd.tmpfiles.rules = [
        "d /var/lib/hart/darwin 0750 hart hart -"
      ];
    })
  ]);
}
