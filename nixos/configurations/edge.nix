{ config, lib, pkgs, hartSrc, ... }:

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
    "${toString <nixpkgs>}/nixos/modules/installer/cd-dvd/installation-cd-minimal.nix"
  ];

  # ─── HART OS (minimal) ───
  hart = {
    enable = true;
    variant = "edge";

    # Observer: no agent, no LLM, no vision
    agent.enable = false;
    llm.enable = false;
    vision.enable = false;

    # Kernel: minimal — only agent sandbox for security
    kernel = {
      enable = true;
      androidNative.enable = false;
      windowsNative.enable = false;
      aiCompute.enable = false;
      agentSandbox.enable = false;     # No agents on edge
    };

    # Sandbox for diagnostics
    sandbox.enable = true;

    # ── AI-Native: Compute Mesh Only ──
    # Edge devices contribute compute to the user's mesh.
    # No local models, no UI, no subsystems — just compute donation.
    computeMesh = {
      enable = true;
      maxOffloadPercent = 80;          # Edge donates most of its compute
      allowWAN = true;
    };
    # No modelBus (edge has no local models — uses mesh)
    # No liquidUI (edge has no display)
    # No appBridge (edge has no subsystems)
  };

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
  boot.kernel.sysctl."vm.swappiness" = 60;

  environment.noXlibs = true;
  documentation.enable = false;
  documentation.man.enable = false;
  documentation.nixos.enable = false;

  services.getty.autologinUser = lib.mkDefault "hart-admin";

  services.journald.extraConfig = ''
    SystemMaxUse=50M
    MaxRetentionSec=7day
  '';
}
