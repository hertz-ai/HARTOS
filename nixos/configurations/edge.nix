{ config, lib, pkgs, modulesPath, hartSrc, hartImageKind ? "iso", ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Edge Variant
# ═══════════════════════════════════════════════════════════════
#
# Minimal observer node:
#   - Backend + discovery only (participate in hive)
#   - Native kernel extensions (sandbox for safety)
#   - No AI compute, no Android, no Windows, no GUI
#   - Minimum 1GB RAM
#
# For: IoT devices, Raspberry Pi Zero, constrained ARM boards

{
  imports = [
      ../profiles/edge.nix   # variant feature profile (hart.* block; moved 2026-07-28)
    ]
    # ISO-ONLY (mirrors desktop.nix's hartImageKind guard): the CD profile +
    # isoImage branding. Raw/installed builds (hartImageKind = "raw") get an
    # installed system; console access comes from the profile's hart-admin
    # getty autologin.
    ++ lib.optionals (hartImageKind == "iso") [
      "${modulesPath}/installer/cd-dvd/installation-cd-minimal.nix"
      # isoImage.* options exist only while the CD profile above is imported —
      # setting them unconditionally breaks the raw eval (same shape as
      # desktop.nix / server.nix).
      ({ config, lib, pkgs, ... }: {
        # ISO branding
        isoImage = {
          isoName = lib.mkForce "hart-os-${config.hart.version}-edge-${pkgs.system}.iso";
          volumeID = lib.mkForce "HART_OS";
          appendToMenuLabel = " HART OS Edge";
          # BIOS/CSM boot as well as UEFI — same gap the desktop ISO had. Edge
          # targets old industrial boards, which are the MOST likely to be
          # BIOS-only, so an EFI-only edge ISO was the worst fit of the three.
          makeBiosBootable = lib.mkDefault true;
        };
      })
    ];

  # ─── Disable ZFS (broken in nixpkgs 24.11 for kernel 6.15) ───
  boot.supportedFilesystems.zfs = lib.mkForce false;

  # ─── HART OS Core Services: moved to ../profiles/edge.nix ───
  # The hart.* feature block (what makes the edge a edge) now lives in
  # profiles/edge.nix, imported above, so the SAME block can also drive the
  # nixosTest nodes (#15) and the installer (#17) without duplicating it here.
  # This file keeps only what is image/media-specific plus hart.package below.

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # ─── Edge experience: moved to ../profiles/edge.nix (task #21) ───
  # hart-cli, headless, swappiness, docs-off, getty autologin, journald
  # caps — an installed edge node composes the same surface.

  # ISO branding: moved into the hartImageKind == "iso" branch of `imports`
  # above (verbatim) — isoImage.* options exist only under the CD profile.
}
