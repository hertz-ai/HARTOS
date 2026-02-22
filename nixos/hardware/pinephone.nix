{ config, lib, pkgs, mobile-nixos, ... }:

# HART OS — PinePhone / PinePhone Pro Hardware Configuration
# Allwinner A64 SoC, cellular modem (EG25-G), touch display, sensors

{
  imports = [
    # Mobile NixOS provides PinePhone kernel, device tree, modem support
    "${mobile-nixos}/devices/pine64-pinephone"
  ];

  # ─── Boot ───
  boot = {
    # Mobile NixOS provides the PinePhone kernel
    kernelParams = [
      "console=ttyS0,115200"
    ];

    # PinePhone-specific modules
    initrd.availableKernelModules = [
      "sun4i-drm"        # Display
      "sun8i-mixer"      # Display mixer
      "axp20x-pek"       # Power button
      "axp20x-battery"   # Battery driver
      "gc2145"           # Rear camera
      "ov5640"           # Front camera
    ];
  };

  # ─── Display ───
  # 5.95" 720x1440 IPS LCD
  hardware.opengl = {
    enable = true;
    driSupport = true;
  };

  # ─── Cellular Modem (Quectel EG25-G) ───
  # ModemManager handles voice calls, SMS, data
  services.modemManager.enable = true;

  # Modem firmware + power management
  systemd.services.eg25-manager = {
    description = "PinePhone EG25-G Modem Manager";
    after = [ "ModemManager.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.eg25-manager}/bin/eg25-manager";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };

  # ─── Audio ───
  # PineWire for audio routing (earpiece, speaker, headphone, modem)
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
  };

  # Audio codecs
  hardware.enableAllFirmware = true;

  # ─── WiFi & Bluetooth ───
  # RTL8723CS WiFi/BT combo
  hardware.enableRedistributableFirmware = true;

  networking.networkmanager = {
    enable = true;
    wifi.powersave = true;
  };

  hardware.bluetooth = {
    enable = true;
    powerOnBoot = false;  # Save battery; enable on demand
  };

  # ─── Sensors ───
  hardware.sensor.iio.enable = true;  # Accelerometer, proximity, light

  # ─── GPS ───
  services.geoclue2.enable = true;

  # ─── Power Management ───
  services.upower.enable = true;

  # Aggressive power saving for battery life
  services.tlp = {
    enable = true;
    settings = {
      CPU_SCALING_GOVERNOR_ON_BAT = "powersave";
      CPU_SCALING_GOVERNOR_ON_AC = "schedutil";
      WIFI_PWR_ON_BAT = "on";
    };
  };

  # ─── Touch + Input ───
  # Goodix touch controller
  services.udev.extraRules = ''
    # PinePhone touch screen
    SUBSYSTEM=="input", ATTRS{name}=="1c2ac00.i2c:touchscreen@5d", ENV{LIBINPUT_CALIBRATION_MATRIX}="1 0 0 0 1 0"
    # Haptic feedback motor
    SUBSYSTEM=="leds", KERNEL=="vibrator", MODE="0660", GROUP="hart"
  '';

  # ─── Filesystem ───
  fileSystems."/" = {
    device = "/dev/disk/by-label/NIXOS_SD";
    fsType = "ext4";
  };

  # Minimal swap on eMMC/SD
  swapDevices = [{
    device = "/var/swapfile";
    size = 1024;
  }];

  # ─── PinePhone-specific sysctl ───
  boot.kernel.sysctl = {
    # Reduce write pressure on eMMC/SD
    "vm.dirty_writeback_centisecs" = 6000;
    "vm.laptop_mode" = 5;
  };
}
