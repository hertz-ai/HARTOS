{ config, lib, pkgs, ... }:

# HART OS — Generic RISC-V Hardware Configuration
# StarFive VisionFive 2, SiFive HiFive Unmatched, Milk-V, etc.
# Extlinux boot (no GRUB on RISC-V), CPU-only inference, conservative memory

{
  # ─── Boot ───
  boot = {
    # Use latest kernel for broadest RISC-V SoC support
    kernelPackages = pkgs.linuxPackages_latest;

    loader = {
      grub.enable = false;
      generic-extlinux-compatible.enable = true;
    };

    kernelParams = [
      "console=ttyS0,115200"
      "earlycon"
    ];
  };

  # ─── AI Inference ───
  # No GPU on most RISC-V boards — force CPU-only
  environment.variables = {
    HART_FORCE_CPU = "true";
    CUDA_VISIBLE_DEVICES = "";
    HART_LLM_WORKERS = "1";
  };

  # ─── Memory ───
  # Conservative settings for boards with 2-8 GB RAM
  boot.kernel.sysctl = {
    "vm.overcommit_memory" = 0;
    "vm.dirty_writeback_centisecs" = 6000;
  };

  swapDevices = [{
    device = "/var/swapfile";
    size = 2048;  # 2 GB swap
  }];

  # ─── Network ───
  networking.networkmanager.enable = lib.mkDefault true;
}
