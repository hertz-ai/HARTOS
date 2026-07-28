{ config, lib, pkgs, modulesPath, hartSrc, ... }:

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
    ../profiles/edge.nix     # variant feature profile (hart.* block; moved 2026-07-28)
    "${modulesPath}/installer/cd-dvd/installation-cd-minimal.nix"
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

  # CLI only
  environment.systemPackages = [
    (pkgs.callPackage ../packages/hart-cli.nix { inherit hartSrc; })
  ];

  # ISO branding
  isoImage = {
    isoName = lib.mkForce "hart-os-${config.hart.version}-edge-${pkgs.system}.iso";
    volumeID = lib.mkForce "HART_OS";
    appendToMenuLabel = " HART OS Edge";
  };

  # Headless
  services.xserver.enable = false;

  # Minimal footprint
  boot.kernel.sysctl."vm.swappiness" = lib.mkForce 60;

  # environment.noXlibs removed in nixpkgs — headless by not including X/GNOME
  documentation.enable = false;
  documentation.man.enable = false;
  documentation.nixos.enable = false;

  services.getty.autologinUser = lib.mkDefault "hart-admin";

  services.journald.extraConfig = ''
    SystemMaxUse=50M
    MaxRetentionSec=7day
  '';
}
