{ config, lib, pkgs, modulesPath, hartSrc, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Server Variant
# ═══════════════════════════════════════════════════════════════
#
# Headless powerhouse:
#   - All AI services (LLM, vision, agents)
#   - Native kernel extensions (GPU compute, agent sandboxing)
#   - AI runtime with full GPU scheduling
#   - Flatpak for server GUI tools (via SSH X11 forwarding)
#   - No desktop environment, no Android, no Windows
#
# Minimum 4GB RAM. Recommended 16GB+ for LLM hosting.

{
  imports = [
    "${modulesPath}/installer/cd-dvd/installation-cd-minimal.nix"
  ];

  # ─── Disable ZFS (broken in nixpkgs 24.11 for kernel 6.15) ───
  boot.supportedFilesystems.zfs = lib.mkForce false;
  nixpkgs.config.allowBroken = false;

  # ─── Workaround: systemd-hwdb update fails on WSL2 build hosts ───
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
    variant = "server";

    # All AI services
    agent.enable = true;
    llm.enable = true;
    vision.enable = true;

    # ── Kernel Extensions ──
    kernel = {
      enable = true;
      androidNative.enable = false;    # No Android on server
      windowsNative.enable = false;    # No Windows on server
      aiCompute = {
        enable = true;                 # Full GPU compute
        hugePagesCount = 0;            # Auto (set high for dedicated inference)
      };
      agentSandbox.enable = true;      # Isolate agents
    };

    # ── AI Runtime (full power) ──
    aiRuntime = {
      enable = true;
      gpu.enable = true;
      worldModel.enable = true;
      agents = {
        maxConcurrent = 16;            # Server can handle many agents
        maxMemoryPerAgent = "4G";
      };
      # Semantic intelligence: self-healing services + predictive prefetch
      semantic = {
        enable = true;
        serviceIntelligence = true;
        predictivePrefetch = true;
        smartFS = false;               # No user files on server typically
      };
    };

    # ── AI-Native OS Layers ──
    # Model Bus: every app/service gets native AI access
    modelBus.enable = true;

    # Compute Mesh: share this server's GPU with user's other devices
    computeMesh = {
      enable = true;
      maxOffloadPercent = 70;          # Server donates generously
      allowWAN = true;
    };

    # No LiquidUI (headless server)
    # No App Bridge (no subsystems on server)

    # ── Sandbox ──
    sandbox.enable = true;
  };

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # CLI tool
  environment.systemPackages = [
    (pkgs.callPackage ../packages/hart-cli.nix { inherit hartSrc; })
  ];

  # ISO branding
  isoImage = {
    isoName = lib.mkForce "hart-os-${config.hart.version}-server-${pkgs.system}.iso";
    volumeID = lib.mkForce "HART_OS";
    appendToMenuLabel = " HART OS Server";
    # EFI-only: skip legacy BIOS/syslinux (empty isolinux.bin on WSL2 builds)
    makeBiosBootable = lib.mkForce false;
  };

  # Boot configuration
  boot.loader.timeout = lib.mkForce 5;

  # Serial console for headless/QEMU boot
  boot.kernelParams = [ "console=ttyS0,115200n8" ];

  # SSH for remote access (NixOS live env)
  services.openssh = {
    enable = true;
    settings.PermitRootLogin = "yes";
    settings.PermitEmptyPasswords = "yes";
    settings.PasswordAuthentication = true;
  };

  # SSH key + password auth for dev machine access
  users.users.root = {
    initialHashedPassword = lib.mkForce "$6$6eou36tyKBY.i3XA$bTuCl2eaJaTOYCELTsal6N/acBlksH8fZ5/ugH/jlLdzFfUQkCEoLrmhh8mXimAwybvrQCjKqmYAAio9Ta4p41";
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJZsU51nixnLUQMV/T4IeXruPZBfe17rB00pNb/WQEDc sathish@hertzai.com"
    ];
  };
  users.users.nixos = {
    initialHashedPassword = lib.mkForce "$6$6eou36tyKBY.i3XA$bTuCl2eaJaTOYCELTsal6N/acBlksH8fZ5/ugH/jlLdzFfUQkCEoLrmhh8mXimAwybvrQCjKqmYAAio9Ta4p41";
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJZsU51nixnLUQMV/T4IeXruPZBfe17rB00pNb/WQEDc sathish@hertzai.com"
    ];
  };

  # Headless: no desktop
  services.xserver.enable = false;

  # Auto-login on console (first-time setup)
  services.getty.autologinUser = lib.mkDefault "hart-admin";
}
