{ lib, pkgs, modulesPath, hartSrc, hartImageKind ? "iso", ... }:

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
  # ── Image-kind switch (hartImageKind specialArg; DEFAULT "iso") ──
  # "iso" (mkSystem -- the live/rescue medium, byte-identical to before this arg
  # existed since mkSystem does not pass it) pulls the CD profile + ISO branding.
  # "raw" (mkImage -- the INSTALLED system: raw-efi disk image with a WRITABLE
  # root) drops the live-CD plumbing entirely: state persists because the root
  # filesystem is a real disk, like any installed OS -- no HARTSTATE carve, no
  # bind-persist workarounds (steward 2026-07-16: "Live USB is still like C
  # Drive is what I was thinking"). The flash is FIRST-INSTALL; OTA
  # (nixos-rebuild switch) finally has an installed generation to switch.
  imports = [
      # The variant feature profile — the hart.* block moved 2026-07-28;
      # see profiles/desktop.nix for the three-consumer rationale.
      ../profiles/desktop.nix
    ]
    ++ lib.optionals (hartImageKind == "iso") [
      "${modulesPath}/installer/cd-dvd/installation-cd-graphical-gnome.nix"
      # ISO-only branding lives INSIDE the iso branch because the isoImage.*
      # options exist only while the CD profile above is imported -- setting
      # them unconditionally breaks the raw eval ("option does not exist").
      ({ config, lib, pkgs, ... }: {
        # The installer ships ON THE ISO ONLY (live medium = the thing you
        # install FROM). Installed systems and the raw image carry no installer.
        hart.installer.enable = true;

        # ─── ISO Branding ───
        isoImage = {
          isoName = lib.mkForce "hart-os-${config.hart.version}-desktop-${pkgs.system}.iso";
          volumeID = lib.mkForce "HART_OS";
          appendToMenuLabel = " HART OS Desktop";
          # The desktop closure (GNOME + HART + every subsystem + the compositor) sits at
          # the ISO9660 size ceiling. The dd841b65 build FAILED at xorriso:
          #   "Image size 3419136s exceeds free space on media 2742704s" (exit 32)
          # i.e. the squashfs (compressed with the installer profile's default
          # zstd -Xcompression-level 19) came out ~1.3 GiB LARGER than the nixpkgs ISO
          # size estimate assumed. zstd level 22 (max; squashfs compresses per-block so
          # the higher level costs CPU, not catastrophic memory) squeezes the squashfs
          # harder so the real image lands under the estimate — and a smaller ISO also
          # flashes faster. Bumped from the default 19 because the desktop variant is the
          # only one near the ceiling (server/edge build fine at 19).
          squashfsCompression = "zstd -Xcompression-level 22";

          # ─── Build-time on-stick HARTLOG partition: assessed NOT safely shippable ───
          # A build-time step to append a 64 MB FAT32 HARTLOG partition as the LAST
          # partition of this .iso (backup GPT at the IMAGE end, partition 1 + ESP
          # byte-identical, isohybrid MBR/El-Torito/GPT coherent) was evaluated and is
          # deliberately NOT shipped. Empirically tested 2026-06-30 on a synthetic
          # nixpkgs-equivalent isohybrid ISO (same flags as lib/make-iso9660-image.sh:
          # -isohybrid-mbr isohdpfx.bin / -eltorito-alt-boot -e boot/efi.img
          # -isohybrid-gpt-basdat):
          #   * The only post-build tool reachable from a wrapper derivation, xorriso
          #     -indev/-outdev -append_partition, is DESTRUCTIVE on re-commit: it DROPS
          #     the EFI System Partition (the 0xEF MBR + GPT entry vanished), flips
          #     partition 1 from the 0x00 isohybrid-basdat type to 0x83, and rewrites the
          #     System Area + iso9660 volume descriptors. That breaks UEFI boot - the
          #     opposite of a byte-identical append.
          #   * The only CORRECT route is -append_partition inside the ORIGINAL
          #     `xorriso -as mkisofs` call (the grub-mkrescue pattern: ESP preserved,
          #     partition 1 type preserved, HARTLOG added as partition 3 in MBR + GPT,
          #     backup GPT placed at the image end - all verified on the synthetic ISO).
          #     But nixpkgs' lib/make-iso9660-image.sh exposes NO hook to inject extra
          #     xorriso flags, so reaching it means forking that internal upstream script
          #     (unstable across nixpkgs bumps) and it still could not be validated
          #     without a full ISO build + real-HW UEFI boot (where the duplicate-label
          #     boot race also hides). Risking the boot layout for a diagnostic
          #     convenience is the wrong trade.
          # The on-stick HARTLOG is instead provided by hart.hartlogCreate (Live-OS,
          # guarded to never complete the boot-disk GPT) and hart.journalExport (to a
          # SEPARATE FAT32 stick). Revisit only if nixpkgs gains an xorriso-extra-args
          # hook, with a real-HW UEFI boot as the gate.
        };
      })
    ]
    ++ lib.optionals (hartImageKind == "raw") [
      ({ lib, ... }: {
        # First boot must claim the WHOLE stick: nixos-generators' raw-efi sizes
        # the root to the closure (pinned rev 8946737 sets neither of these), so
        # without growth a 28.7 GB stick would strand ~10 GB. growPartition
        # expands the root partition in the initrd; autoResize then grows the
        # ext4 to fill it. Root is the LAST partition in the raw-efi layout
        # (ESP first), which is exactly what growpart requires.
        boot.growPartition = true;
        fileSystems."/".autoResize = true;
      })
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
  # (now in the profile, slice 4) still trims the runtime locale-archive
  # to 18 locales.

  # ─── Workaround: systemd-hwdb update fails on CI/WSL2 build hosts ───
  # Replace the hwdb.bin derivation with a minimal stub.
  # The real hwdb.bin will be regenerated on first boot by udev.
  environment.etc."udev/hwdb.bin".source = lib.mkForce (
    pkgs.runCommand "hwdb-stub" {} ''
      # Create minimal valid hwdb binary (KSLP magic + empty index)
      printf 'KSLP\x00\x00\x00\x00' > $out
    ''
  );

  # ─── HART OS Core Services: moved to ../profiles/desktop.nix ───
  # The hart.* feature block (what makes the desktop a desktop) now lives in
  # profiles/desktop.nix, imported above, so the SAME block can also drive the
  # nixosTest nodes (#15) and the installer (#17) without duplicating it here.
  # This file keeps only what is image/media-specific plus hart.package below.

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # ─── The desktop EXPERIENCE: moved to ../profiles/desktop.nix ───
  # (Parity slice 4, task #21.) The app set, GNOME/GDM + libinput, greeter and
  # user dconf branding, fonts + i18n + input methods, XDG MIME defaults, the
  # com.hart.Agent D-Bus policy, bluetooth, NetworkManager + redistributable
  # firmware, printing/scanning, geoclue, accessibility, power management,
  # /etc/hart/branding assets, and the Plymouth boot splash (with its
  # hartLogoPng/hartPlymouth derivations and quiet+splash kernel params) are
  # VARIANT surface — the installed desktop must ship the same experience as
  # the image. All moved verbatim, comments included. This file keeps only
  # media concerns (image-kind switch above), live-CD countermeasures, and
  # hardware policy (GPU blocks below).

  # (ISO branding -- isoImage.isoName/volumeID/squashfsCompression + the
  # assessed-not-shippable build-time HARTLOG note -- moved into the
  # hartImageKind == "iso" imports branch at the top of this file: the
  # isoImage.* options only exist while the CD profile is imported.)


  # ─── Auto-login + recovery consoles: moved to ../profiles/desktop.nix ───
  # (Parity slice 3, task #21.) The hart-admin auto-login and the
  # Ctrl+Alt+F2..F6 recovery-TTY guarantee are VARIANT surface — an installed
  # desktop needs the same appliance login and the same escape hatch from a
  # wedged compositor. Load-bearing comments moved with them.

  # ─── Hide the NixOS live-installer user (NixOS must be invisible) ───
  # IMAGE-ONLY (stays here): installation-cd-graphical-gnome.nix (imported
  # above) injects a NORMAL `nixos` user (uid 1000) plus its own auto-login.
  # The profile auto-logs-in hart-admin; here we demote `nixos` to a hidden
  # SYSTEM account (uid < 1000) so GDM never lists it in the greeter, and we
  # drop the TTY auto-login so a Ctrl+Alt+F-key never lands on "nixos" either
  # (the recovery-TTY block in the profile keeps getty itself running).
  # Android hides Linux from its users; HART OS hides NixOS the same way.
  # Installed systems never import the CD profile, so there is nothing to hide.
  users.users.nixos = lib.mkForce {
    isSystemUser = true;
    group = "nixos";
  };
  users.groups.nixos = lib.mkForce {};
  services.getty.autologinUser = lib.mkForce null;

  # ─── Tier ladder host + copilot + audio: moved to ../profiles/desktop.nix ───
  # (Parity slice 2, task #21.) hart.layerShellHost, hart.copilot,
  # hart.audio.bootUnmute and services.pipewire are VARIANT surface — an
  # installed desktop (mkInstalledSystem = profile + hardware) must run the
  # same tier ladder, co-pilot and audio stack as the image. Their load-bearing
  # comments moved verbatim with them; only the HONEST HW CAVEAT about the
  # GTK4 first-paint hang stays here, because it describes THIS image's boot on
  # real hardware: a real boot TRIES Tier-1 (hart-comp), may hang ~shellPaint
  # seconds, drops to Tier-2 (sway, same host), then to the Tier-3 cage floor —
  # never a blank screen (on-HW journal reachable via the recovery TTY, b97f1ae).

  # GPU: Vulkan + 32-bit (required for DXVK/Proton)
  hardware.graphics = {
    enable = true;
    enable32Bit = true;
  };

  # ─── GPU: drive the desktop on the Intel iGPU; blacklist nouveau ───
  # The real-HW 2026-06-25 journal showed nouveau (the open-source driver for the
  # discrete GeForce 940MX) throwing `MMIO read ... FAULT [PRIVRING]` — a Maxwell
  # dGPU nouveau cannot reliably drive — which faults the GPU and drags out the
  # boot. The desktop only needs the Intel iGPU (healthy GL/Vulkan, drives the
  # panel via i915), so blacklist nouveau and KMS-off the dGPU (nouveau.modeset=0
  # below). The 940MX returns later, OPT-IN, via the proprietary driver for AI
  # compute (hardware-gated) — display never depends on it.
  boot.blacklistedKernelModules = [ "nouveau" ];

  # nouveau.modeset=0 keeps the faulting Maxwell dGPU from KMS-initialising at all
  # (belt-and-suspenders with the blacklist above) — the Intel iGPU owns the display.
  boot.kernelParams = [ "nouveau.modeset=0" ];
}
