# ═══════════════════════════════════════════════════════════════
# HART OS installer — hart-install (CLI) + offline install sources
# ═══════════════════════════════════════════════════════════════
#
# Step 4 of docs/architecture/HART_INSTALLER_UNION_PLAN.md: a THIN, scriptable
# orchestrator over the standard NixOS machinery (nixos-generate-config +
# nixos-install) whose output is the UNION — NixOS's hardware layer plus HART's
# OS layer — never stock NixOS. The graphical path (rebranded Calamares, step 5)
# calls the SAME config generator this CLI uses, so GUI and CLI cannot produce
# different systems.
#
# WHAT IT DELIBERATELY DOES NOT DO:
#   - It never edits the partition TABLE. The user (or later, Calamares's
#     partition page) creates the target partition first — gparted ships on the
#     ISO. A tool that both partitions and installs is where installers eat
#     Windows; this one formats exactly ONE partition it was explicitly given.
#   - It never formats an ESP it found. Dual-boot survival = the existing ESP
#     (with the Windows Boot Manager entry inside) is REUSED: mounted, written
#     into by the bootloader install, never mkfs'd. A missing ESP on an EFI
#     machine is an instructive ERROR, not an invitation to create one.
#   - It never writes NVRAM itself. boot.loader.efi.canTouchEfiVariables = true
#     in the WRITTEN CONFIG makes nixos-install's bootctl do it — the ordinary
#     NixOS path, registering HART beside the Windows entry, never replacing
#     EFI/BOOT/BOOTX64.EFI (Windows' fallback loader).
#
# OFFLINE STORY (why hartFlakeInputs exists): the installed /etc/nixos/flake.nix
# pins the HART flake by path (the repo source this module bakes at
# /etc/hart/src). Evaluating it needs the hart flake's OWN locked inputs; their
# lock entries carry narHashes, and nix resolves a narHash-pinned input from the
# local store when the source is already present. This module references every
# input's outPath, so the ISO closure carries them and `nixos-install` works
# with no network — offline-first, robots in fields, air-gapped rooms.
#
# ISO-gated: hart.installer.enable is set in configurations/desktop.nix's
# hartImageKind == "iso" branch. Installed systems and the raw image do not
# carry an installer.
# hartFlakeInputs is defaulted so importing the module set WITHOUT the HART
# flake's specialArgs (external nixosModules.hart consumers) still evaluates
# while the installer stays disabled; enabling it without the inputs is a
# legitimate eval error (the offline story cannot exist without the sources).
{ config, lib, pkgs, hartSrc, hartFlakeInputs ? { }, ... }:

let
  cfg = config.hart.installer;

  # One generator for the union config, shared by this CLI and (step 5) the
  # Calamares config module: it writes the flake that composes
  # hart.lib.mkInstalledSystem { variant; hardwareModules }. Kept as its own
  # script so Calamares can call it with a prepared /mnt without going through
  # hart-install's device handling.
  hartWriteInstallConfig = pkgs.writeShellApplication {
    name = "hart-write-install-config";
    runtimeInputs = [ pkgs.nixos-install-tools ];
    text = ''
      # hart-write-install-config <mounted-root> <variant> <efi|bios>
      # Writes /etc/nixos/{flake.nix} + /etc/hart/src on an already-mounted
      # target, generates hardware-configuration.nix, and prints the flake ref
      # to install. Assumes $1 is a mounted root (with /boot mounted on EFI).
      set -euo pipefail
      mnt="$1"; variant="$2"; firmware="$3"

      # NixOS hardware layer: the generated hardware-configuration.nix is what
      # makes this hardware-agnostic — no per-machine code anywhere in HART.
      # --show-hardware-config, NOT a bare --root run: the latter also writes a
      # stock configuration.nix onto the target — a second config claiming
      # authority on a machine whose one config is the union flake (review C:M1).
      mkdir -p "$mnt/etc/nixos"
      nixos-generate-config --root "$mnt" --show-hardware-config \
        > "$mnt/etc/nixos/hardware-configuration.nix"

      # HART OS layer: the repo source, so the installed machine can
      # `nixos-rebuild` offline forever. Store paths are read-only; the copy
      # must be writable for future `nix flake lock` runs.
      mkdir -p "$mnt/etc/hart"
      if [ ! -e "$mnt/etc/hart/src" ]; then
        cp -a ${hartSrc} "$mnt/etc/hart/src"
        chmod -R u+w "$mnt/etc/hart/src"
      fi

      # The bootloader module, by firmware — the ONLY hardware-dependent choice
      # the installer makes, and it makes it by PROBE, not by machine model.
      if [ "$firmware" = "efi" ]; then
        cat > "$mnt/etc/nixos/boot.nix" <<'BOOTEOF'
      { ... }: {
        # Installed dual-boot contract: register our OWN NVRAM entry beside the
        # Windows Boot Manager (systemd-boot auto-discovers Windows on a shared
        # ESP). This is the deliberate INVERSE of the portable raw image's
        # canTouchEfiVariables = false.
        boot.loader.systemd-boot.enable = true;
        boot.loader.efi.canTouchEfiVariables = true;
      }
      BOOTEOF
      else
        # GRUB needs the DISK, derived HERE from the mounted root so both the
        # CLI and the Calamares --mounted path get it (review C:M2 — the old
        # sed lived only in hart-install and the GUI path would have shipped a
        # literal @GRUB_DEVICE@ into the config).
        root_part="$(findmnt -no SOURCE "$mnt")"
        grub_disk="/dev/$(lsblk -no PKNAME "$root_part")"
        cat > "$mnt/etc/nixos/boot.nix" <<BOOTEOF
      { ... }: {
        # BIOS machine (old industrial controllers, pre-UEFI boxes): GRUB with
        # os-prober so an existing Windows/other OS gets a menu entry.
        boot.loader.grub.enable = true;
        boot.loader.grub.useOSProber = true;
        boot.loader.grub.device = "$grub_disk";
      }
      BOOTEOF
      fi

      # local.nix: THE machine-local extension point, always present so every
      # front-end writes INTO it instead of editing the flake — the CLI puts
      # --hostname here, the Calamares hartcfg module puts hostname/user/locale
      # here. Default is an empty module. Never overwrite an existing one
      # (front-ends write it BEFORE calling this generator on re-runs).
      if [ ! -e "$mnt/etc/nixos/local.nix" ]; then
        # DUAL-BOOT CLOCK (real-HW 2026-07-30, task #24). Windows keeps the RTC
        # in LOCAL time; NixOS assumes UTC. On the steward's machine the node
        # therefore ran +5:30 wrong for 23 minutes and, the instant wifi
        # connected, NTP yanked the wall clock BACKWARDS by the whole offset —
        # immediately before the desktop hung. A large backwards step breaks
        # anything computing `deadline - now` (paint watchdog, resource
        # governor, canary duration, SSE keepalives).
        #
        # NOT a blanket setting: on a single-OS machine the RTC really is UTC
        # and forcing local time would be wrong in the other direction. So it
        # is written ONLY when a Windows bootloader is actually present, using
        # the SAME probe the dual-boot install path already relies on
        # (bootmgfw.efi on the ESP we are about to share). NixOS's own
        # time.hardwareClockInLocalTime is the mechanism — nothing invented.
        _win=""
        for _p in "$mnt/boot/EFI/Microsoft/Boot/bootmgfw.efi" \
                  "$mnt/boot/efi/EFI/Microsoft/Boot/bootmgfw.efi"; do
          [ -e "$_p" ] && _win=1 && break
        done
        if [ -n "$_win" ]; then
          echo "[hart-install] Windows bootloader detected — RTC will be read as LOCAL time"
          cat > "$mnt/etc/nixos/local.nix" <<'LOCALEOF'
{ ... }:
{
  # Written by hart-install: a Windows bootloader was found on this machine.
  # Windows keeps the hardware clock in local time, so HART must read it the
  # same way or the two OSes fight over the clock and NTP steps the wall clock
  # by the timezone offset on first sync (task #24).
  time.hardwareClockInLocalTime = true;
}
LOCALEOF
        else
          printf '{ ... }: { }\n' > "$mnt/etc/nixos/local.nix"
        fi
      fi

      # The union flake. hart.lib.mkInstalledSystem is THE generator — the same
      # composition the flake's own eval-gated hart-desktop-installed fixture
      # builds, so this file is regression-checked upstream on every push.
      cat > "$mnt/etc/nixos/flake.nix" <<FLAKEEOF
      {
        description = "HART OS (installed) — NixOS hardware layer + HART OS layer";
        inputs.hart.url = "path:/etc/hart/src?dir=nixos";
        outputs = { self, hart, ... }: {
          nixosConfigurations.hart = hart.lib.mkInstalledSystem {
            system = "x86_64-linux";
            variant = "$variant";
            hardwareModules = [
              ./hardware-configuration.nix
              ./boot.nix
              ./local.nix
            ];
          };
        };
      }
      FLAKEEOF
      echo "$mnt/etc/nixos#hart"
    '';
  };

  hartInstall = pkgs.writeShellApplication {
    name = "hart-install";
    runtimeInputs = with pkgs; [
      util-linux e2fsprogs dosfstools nixos-install-tools hartWriteInstallConfig
    ];
    text = ''
      set -euo pipefail

      usage() {
        cat <<'USAGE'
      hart-install — install HART OS onto a partition, beside whatever is there.

        hart-install --root /dev/sdXN [--esp /dev/sdXM] [--variant desktop]
                     [--hostname NAME] [--yes] [--no-install] [--mounted /mnt]

        --root DEV     REQUIRED. The partition that becomes /. IT WILL BE
                       FORMATTED (ext4). Nothing else is ever formatted.
        --esp DEV      The EXISTING EFI System Partition to reuse (mounted,
                       written into, NEVER formatted). Default: auto-detect on
                       the same disk as --root.
        --variant V    desktop | server | edge (default desktop).
        --yes          Skip the confirmation (fleet/headless use).
        --no-install   Stop after writing the target's config — everything
                       except the closure build. This is also the test seam.
        --mounted DIR  Target is ALREADY mounted at DIR (e.g. by Calamares);
                       skip device handling entirely and only write the config.

      Partitioning is NOT this tool's job: create the target partition first
      (gparted is on this ISO). That division is what keeps this tool unable to
      eat a Windows disk.
      USAGE
        exit 1
      }

      ROOT_DEV=""; ESP_DEV=""; VARIANT="desktop"; HOSTNAME_ARG=""
      ASSUME_YES=0; NO_INSTALL=0; MOUNTED=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --root) ROOT_DEV="$2"; shift 2 ;;
          --esp) ESP_DEV="$2"; shift 2 ;;
          --variant) VARIANT="$2"; shift 2 ;;
          --hostname) HOSTNAME_ARG="$2"; shift 2 ;;
          --yes) ASSUME_YES=1; shift ;;
          --no-install) NO_INSTALL=1; shift ;;
          --mounted) MOUNTED="$2"; shift 2 ;;
          *) usage ;;
        esac
      done

      [ "$(id -u)" = 0 ] || { echo "hart-install: must run as root" >&2; exit 1; }
      case "$VARIANT" in desktop|server|edge) ;; *)
        echo "hart-install: unknown variant '$VARIANT'" >&2; exit 1 ;; esac

      # Firmware by probe — the only branch, and it is hardware-agnostic.
      if [ -d /sys/firmware/efi ]; then FIRMWARE=efi; else FIRMWARE=bios; fi

      if [ -n "$MOUNTED" ]; then
        # Calamares (or an operator) prepared the mounts; we only compose.
        FLAKE_REF="$(hart-write-install-config "$MOUNTED" "$VARIANT" "$FIRMWARE")"
        echo "hart-install: config written; install with:"
        echo "  nixos-install --root $MOUNTED --flake $FLAKE_REF --no-root-passwd"
        exit 0
      fi

      [ -n "$ROOT_DEV" ] || usage
      [ -b "$ROOT_DEV" ] || { echo "hart-install: $ROOT_DEV is not a block device" >&2; exit 1; }
      # A PARTITION, never a whole disk (review C:C1): a whole-disk node passes
      # -b, and `mkfs.ext4 -F` on it would clobber the partition table — the
      # exact thing this tool promises never to touch. lsblk TYPE is 'part' for
      # partitions on every backing (sd/nvme/vd/mmcblk), so this is the
      # capability check, not a name pattern.
      dev_type="$(lsblk -no TYPE "$ROOT_DEV" | head -1)"
      if [ "$dev_type" != "part" ]; then
        echo "hart-install: $ROOT_DEV is a '$dev_type', not a partition." >&2
        echo "  This tool formats exactly ONE partition and never edits the" >&2
        echo "  partition table. Create a partition first (gparted) and pass it." >&2
        exit 1
      fi

      # ── Refuse the boot medium. The live ISO's own disk must never be a target.
      boot_src="$(findmnt -no SOURCE /iso 2>/dev/null || findmnt -no SOURCE / || true)"
      boot_disk="$(lsblk -no PKNAME "$boot_src" 2>/dev/null || true)"
      root_disk="$(lsblk -no PKNAME "$ROOT_DEV" 2>/dev/null || true)"
      if [ -n "$boot_disk" ] && [ "$boot_disk" = "$root_disk" ]; then
        echo "hart-install: $ROOT_DEV is on the live boot medium ($boot_disk) — refusing" >&2
        exit 1
      fi

      # ── EFI: find the ESP to REUSE (never format). Same-disk, vfat, esp-typed.
      if [ "$FIRMWARE" = efi ] && [ -z "$ESP_DEV" ]; then
        ESP_DEV="$(lsblk -nlo PATH,PARTTYPE "/dev/$root_disk" 2>/dev/null \
          | awk 'tolower($2) == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b" {print $1; exit}')"
        if [ -z "$ESP_DEV" ]; then
          echo "hart-install: no EFI System Partition found on /dev/$root_disk." >&2
          echo "  Dual-boot reuses the EXISTING ESP; this tool never creates or" >&2
          echo "  formats one. If this disk has no OS yet, create a 512M EF00" >&2
          echo "  partition (gparted) and pass it with --esp." >&2
          exit 1
        fi
      fi

      echo "hart-install: plan"
      echo "  root    : $ROOT_DEV  (WILL BE FORMATTED ext4)"
      [ "$FIRMWARE" = efi ] && echo "  esp     : $ESP_DEV  (reused, NOT formatted)"
      echo "  firmware: $FIRMWARE"
      echo "  variant : $VARIANT"
      if [ "$ASSUME_YES" != 1 ]; then
        printf "Type the root device path to confirm formatting it: "
        read -r confirm
        [ "$confirm" = "$ROOT_DEV" ] || { echo "aborted"; exit 1; }
      fi

      # The ONE destructive act, on the ONE explicitly-named partition.
      mkfs.ext4 -F -L hart-root "$ROOT_DEV"

      mkdir -p /mnt
      mount "$ROOT_DEV" /mnt
      if [ "$FIRMWARE" = efi ]; then
        mkdir -p /mnt/boot
        mount "$ESP_DEV" /mnt/boot   # reuse: mounted, never mkfs'd
      fi

      FLAKE_REF="$(hart-write-install-config /mnt "$VARIANT" "$FIRMWARE")"

      if [ -n "$HOSTNAME_ARG" ]; then
        # local.nix is the generator-provided extension point (always in the
        # flake's module list) — no post-hoc sed of the flake, no second file.
        printf '{ ... }: { networking.hostName = "%s"; }\n' "$HOSTNAME_ARG" \
          > /mnt/etc/nixos/local.nix
      fi

      if [ "$NO_INSTALL" = 1 ]; then
        echo "hart-install: --no-install — target is composed at /mnt, not built."
        echo "  finish with: nixos-install --root /mnt --flake $FLAKE_REF --no-root-passwd"
        exit 0
      fi

      nixos-install --root /mnt --flake "$FLAKE_REF" --no-root-passwd
      echo "hart-install: done. Reboot and pick 'HART OS' in the boot menu —"
      echo "  the existing Windows Boot Manager entry is untouched beside it."
    '';
  };
  # ── Rebranded Calamares (plan step 5): reuse + extend + rebrand, zero fork ──
  # pkgs.calamares-nixos ships wholesale (its partition/mount module configs and
  # QML machinery untouched). Calamares reads /etc/calamares IN PREFERENCE to the
  # package share dir, and its modules-search already includes
  # /run/current-system/sw/lib/calamares/modules — so HART needs only:
  #   1. a settings.conf whose exec swaps the stock `nixos` config-writer for
  #      `hartcfg` (ours), keeping partition/mount/umount stock;
  #   2. the hartcfg job module (installer/calamares/hartcfg-main.py — a real
  #      repo file, unit-tested on the dev box), which writes GUI choices into
  #      local.nix DECLARATIVELY and calls the SAME hart-write-install-config
  #      the CLI uses. One writer; GUI and CLI cannot produce different systems.
  #   3. HART branding (name + the existing nixos/branding logo).
  # Stock's post-install `users` mutation job is deliberately DROPPED, not
  # ported: local.nix carries hashedPassword — on a declarative system the
  # config is the truth, never a chroot edit of /etc/shadow.
  hartCalamaresModule = pkgs.runCommand "hart-calamares-hartcfg" { } ''
    install -Dm644 ${../installer/calamares/hartcfg-main.py} \
      $out/lib/calamares/modules/hartcfg/main.py
    cat > $out/lib/calamares/modules/hartcfg/module.desc <<'DESC'
    ---
    type: "job"
    name: "hartcfg"
    interface: "python"
    script: "main.py"
    DESC
  '';

  hartCalamaresSettings = pkgs.writeText "hart-calamares-settings.conf" ''
    # HART OS installer sequence — stock Calamares machinery, HART config-writer.
    ---
    modules-search: [ local, /run/current-system/sw/lib/calamares/modules ]
    sequence:
    - show:
      - welcome
      - locale
      - keyboard
      - users
      - partition
      - summary
    - exec:
      - partition
      - mount
      - hartcfg
      - umount
    - show:
      - finished
    branding: hart
    # Point of no return gets an explicit prompt — this GUI can format a
    # partition the user picked with a mouse, unlike the CLI's retype-the-path.
    prompt-install: true
    dont-chroot: false
    oem-setup: false
    disable-cancel: false
    disable-cancel-during-exec: true
    hide-back-and-next-during-exec: false
    quit-at-end: false
  '';

  hartCalamaresBranding = pkgs.runCommand "hart-calamares-branding" { } ''
    mkdir -p $out
    cp ${../branding/hart-logo.svg} $out/logo.svg
    cat > $out/branding.desc <<'BRAND'
    ---
    componentName: hart
    strings:
      productName: "HART OS"
      shortProductName: "HART OS"
      version: "1.0"
      shortVersion: "1.0"
      versionedName: "HART OS"
      shortVersionedName: "HART OS"
      bootloaderEntryName: "HART OS"
      productUrl: "https://hevolve.ai"
    images:
      productLogo: "logo.svg"
      productIcon: "logo.svg"
      productWelcome: "logo.svg"
    slideshowAPI: 2
    slideshow: "show.qml"
    style:
      SidebarBackground: "#0b0f1a"
      SidebarText: "#e8ecf5"
      SidebarTextCurrent: "#7ae0c3"
    BRAND
    cat > $out/show.qml <<'QML'
    import QtQuick 2.0
    Rectangle {
      color: "#0b0f1a"
      Image { source: "logo.svg"; anchors.centerIn: parent; width: 220; height: 220 }
      Text {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom; anchors.bottomMargin: 48
        color: "#e8ecf5"; font.pixelSize: 18
        text: "HART OS — intelligence in the hands of everyone"
      }
    }
    QML
  '';
in
{
  options.hart.installer = {
    enable = lib.mkEnableOption ''
      the HART OS installer (hart-install CLI + offline install sources).
      ISO-only: enabled from the iso branch of the variant configuration
    '';

    gui.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        the graphical installer (rebranded Calamares driving the SAME
        hart-write-install-config generator as the CLI). Follows
        hart.installer.enable; headless/fleet media set this false
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [
      hartInstall
      hartWriteInstallConfig
      pkgs.gptfdisk
      pkgs.parted
    ] ++ lib.optionals cfg.gui.enable [
      # Stock Calamares-for-NixOS wholesale; /etc/calamares below overrides ONLY
      # settings + branding, and hartCalamaresModule lands on the module search
      # path via /run/current-system/sw/lib/calamares/modules.
      pkgs.calamares-nixos
      hartCalamaresModule
    ];

    # Per-key (NOT a whole environment.etc attrset): this config block already
    # defines environment.etc."hart/..." entries, and a second `environment.etc =`
    # in the same attrset is a duplicate-attribute eval error.
    environment.etc."calamares/settings.conf" =
      lib.mkIf cfg.gui.enable { source = hartCalamaresSettings; };
    environment.etc."calamares/branding/hart" =
      lib.mkIf cfg.gui.enable { source = hartCalamaresBranding; };

    # `nixos-install --flake` is a hard dependency of the written config, and
    # nothing else guarantees flakes on the live medium (review C:C3). List
    # options merge by concatenation, so this composes with any other setter.
    nix.settings.experimental-features = [ "nix-command" "flakes" ];

    # The HART flake source + its locked inputs, baked into the ISO closure so
    # the written flake evaluates OFFLINE (see header). The /etc symlinks are
    # what pull the store paths in; hart-write-install-config copies from the
    # hartSrc store path directly.
    environment.etc."hart/src".source = hartSrc;
    environment.etc."hart/inputs/nixpkgs".source = hartFlakeInputs.nixpkgs.outPath;
    environment.etc."hart/inputs/nixpkgs-rust".source = hartFlakeInputs.nixpkgs-rust.outPath;
    environment.etc."hart/inputs/crane".source = hartFlakeInputs.crane.outPath;
    environment.etc."hart/inputs/llama-cpp".source = hartFlakeInputs.llama-cpp.outPath;
    environment.etc."hart/inputs/nixos-generators".source = hartFlakeInputs.nixos-generators.outPath;
    environment.etc."hart/inputs/nixos-hardware".source = hartFlakeInputs.nixos-hardware.outPath;
    environment.etc."hart/inputs/mobile-nixos".source = hartFlakeInputs.mobile-nixos.outPath;
  };
}
