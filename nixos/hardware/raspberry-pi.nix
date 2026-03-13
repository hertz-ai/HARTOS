{ config, lib, pkgs, nixos-hardware, ... }:

# HART OS — Raspberry Pi 4 / 5 Hardware Configuration
# SD card boot, GPU firmware, WiFi/Bluetooth, device tree

{
  imports = [
    # nixos-hardware provides RPi-specific kernel, firmware, device tree
    # Auto-detects RPi 4 vs 5 from device tree at runtime
    nixos-hardware.nixosModules.raspberry-pi-4
  ];

  # ─── Boot ───
  boot = {
    # Use the RPi-specific kernel
    kernelPackages = pkgs.linuxPackages_rpi4;

    # Enable device tree for hardware detection
    loader = {
      grub.enable = false;
      generic-extlinux-compatible.enable = true;
    };

    # Console on serial + HDMI
    kernelParams = [
      "console=ttyS1,115200"
      "console=tty0"
    ];

    # RPi firmware blobs
    initrd.availableKernelModules = [
      "usbhid"
      "usb_storage"
      "vc4"
      "bcm2835_dma"
      "i2c_bcm2835"
      "bcm2835_rng"
    ];
  };

  # ─── GPU ───
  # VideoCore IV/VI GPU (hardware decode, display)
  hardware.raspberry-pi."4" = {
    fkms-3d.enable = true;  # Full KMS display driver
  };

  # ─── WiFi & Bluetooth ───
  hardware.enableRedistributableFirmware = true;  # Broadcom WiFi/BT firmware

  networking.wireless = {
    enable = false;  # Use NetworkManager instead
  };
  networking.networkmanager.enable = lib.mkDefault true;

  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
  };

  # ─── SD Card ───
  fileSystems."/" = {
    device = "/dev/disk/by-label/NIXOS_SD";
    fsType = "ext4";
  };

  # ─── Power ───
  # RPi has no ACPI; manage via kernel
  powerManagement.cpuFreqGovernor = lib.mkDefault "ondemand";

  # ─── GPIO ───
  # Allow hart user access to GPIO for IoT/sensor integrations
  services.udev.extraRules = ''
    SUBSYSTEM=="gpio", KERNEL=="gpiochip*", MODE="0660", GROUP="hart"
  '';

  # ─── Swap ───
  # SD cards benefit from minimal swap (avoid wear)
  swapDevices = [{
    device = "/var/swapfile";
    size = 1024;  # 1GB swap
  }];

  # ─── RPi-specific sysctl ───
  boot.kernel.sysctl = {
    # Reduce SD card write pressure
    "vm.dirty_writeback_centisecs" = 6000;
    "vm.dirty_expire_centisecs" = 6000;
  };
}
