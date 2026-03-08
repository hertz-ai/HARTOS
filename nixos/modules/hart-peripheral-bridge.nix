{ config, lib, pkgs, ... }:

# HART OS Peripheral Bridge Module
# USB/IP, Bluetooth HID relay, and Gamepad forwarding for remote desktop
# Orchestrates system tools (usbip, bluez, evdev) — does not reimplement drivers

let
  cfg = config.hart;
in
{
  options.hart.peripheralBridge = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable peripheral forwarding for HART remote desktop";
    };

    usbip = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable USB/IP forwarding (kernel module + usbipd)";
      };
    };

    bluetooth = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable Bluetooth HID relay via BlueZ";
      };
    };

    gamepad = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable gamepad forwarding via evdev/uinput";
      };
    };
  };

  config = lib.mkIf (cfg.enable && config.hart.peripheralBridge.enable) {

    # ── USB/IP + gamepad kernel modules ─────────────────────────
    boot.kernelModules =
      (lib.optionals config.hart.peripheralBridge.usbip.enable [
        "usbip-core"
        "usbip-host"
        "vhci-hcd"      # Virtual Host Controller (viewer side)
      ]) ++
      (lib.optionals config.hart.peripheralBridge.gamepad.enable [
        "uinput"
      ]);

    systemd.services.hart-usbipd = lib.mkIf config.hart.peripheralBridge.usbip.enable {
      description = "HART USB/IP Daemon";
      documentation = [ "https://github.com/hevolve-ai/hart" ];
      after = [ "network.target" ];
      partOf = [ "hart.target" ];
      wantedBy = [ "hart.target" ];
      serviceConfig = {
        ExecStart = "${pkgs.linuxPackages.usbip}/bin/usbipd";
        Restart = "on-failure";
        RestartSec = 5;
        # Security hardening
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = [ "CAP_SYS_ADMIN" "CAP_NET_ADMIN" ];
      };
    };

    # ── Bluetooth HID relay ────────────────────────────────────
    hardware.bluetooth = lib.mkIf config.hart.peripheralBridge.bluetooth.enable {
      enable = true;
    };

    services.blueman = lib.mkIf config.hart.peripheralBridge.bluetooth.enable {
      enable = true;
    };

    # ── Gamepad / uinput ───────────────────────────────────────
    # (kernel module "uinput" merged into boot.kernelModules above)

    # udev rules for gamepad + uinput access
    services.udev.extraRules = lib.mkIf config.hart.peripheralBridge.gamepad.enable ''
      # Allow hart group to access input devices
      SUBSYSTEM=="input", GROUP="hart", MODE="0660"
      # Allow hart group to create virtual input devices
      KERNEL=="uinput", GROUP="hart", MODE="0660"
    '';

    # ── Python dependencies (via system packages) ──────────────
    environment.systemPackages = lib.mkIf config.hart.peripheralBridge.gamepad.enable [
      (pkgs.python3.withPackages (ps: [ ps.evdev ]))
    ];

    # ── Firewall: USB/IP default port ──────────────────────────
    networking.firewall.allowedTCPPorts =
      lib.mkIf config.hart.peripheralBridge.usbip.enable [ 3240 ];
  };
}
