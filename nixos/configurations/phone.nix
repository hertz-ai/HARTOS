{ config, lib, pkgs, hartSrc, mobile-nixos, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Phone Variant
# ═══════════════════════════════════════════════════════════════
#
# Native multi-platform phone OS:
#   - Linux apps (native, touch-adaptive via Phosh)
#   - Android apps (native ART — runs WhatsApp, banking, maps natively)
#   - AI agent (native, offloads LLM to hive peers)
#   - Nunba as primary management app
#   - Conky dashboard overlay
#
# For: PinePhone, PinePhone Pro, future ARM phones

{
  imports = [ ../profiles/phone.nix ];  # variant feature profile (hart.* block)

  # ─── HART OS Core Services: moved to ../profiles/phone.nix ───
  # The hart.* feature block (what makes the phone a phone) now lives in
  # profiles/phone.nix, imported above, so the SAME block can also drive the
  # nixosTest nodes (#15) and the installer (#17) without duplicating it here.
  # This file keeps only what is image/media-specific plus hart.package below.

  # HART application package
  hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };

  # ─── Phone Packages ───
  environment.systemPackages = with pkgs; [
    (pkgs.callPackage ../packages/hart-cli.nix { inherit hartSrc; })

    # Phone essentials
    squeekboard
    gnome-contacts
    gnome-calls
    chatty
    megapixels
    gnome-clocks
    gnome-calculator
    firefox
    epiphany
    gnome-files
  ];

  # ─── Phosh (GNOME Mobile Shell) ───
  services.xserver.enable = false;   # Wayland only

  # phosh is launched directly by greetd below.  There is NO
  # `programs.phosh` NixOS option (the nix flake-check #70 failure:
  # "The option `programs.phosh' does not exist"); the greetd session
  # command is the supported launch path on this nixpkgs pin.  HiDPI
  # output scaling lives in phoc's own config (phoc.ini), written via
  # environment.etc below, not a non-existent NixOS option.
  services.greetd = {
    enable = true;
    settings.default_session = {
      command = "${pkgs.phosh}/bin/phosh";
      user = "hart-admin";
    };
  };

  # phoc (the phosh Wayland compositor) reads this for per-output config;
  # replaces the invalid programs.phosh.phocConfig.output."DSI-1".scale.
  environment.etc."phosh/phoc.ini".text = ''
    [output:DSI-1]
    scale = 2
  '';

  # ─── Cellular ───
  # ModemManager comes with networking.networkmanager.enable below — there is
  # no standalone services.modemManager option on this nixpkgs pin (#70: the
  # eval error "services.modemManager does not exist", surfaced once the phosh
  # error above was cleared).

  networking = {
    networkmanager = {
      enable = true;
      wifi.powersave = true;
    };
    wireless.enable = false;
    firewall = {
      allowedTCPPorts = [ config.hart.ports.backend 22 ];
      allowedUDPPorts = [ config.hart.ports.discovery ];
    };
  };

  # ─── Power ───
  services.upower.enable = true;
  services.tlp = {
    enable = true;
    settings = {
      CPU_SCALING_GOVERNOR_ON_BAT = "powersave";
      CPU_SCALING_GOVERNOR_ON_AC = "performance";
      WIFI_PWR_ON_BAT = "on";
    };
  };

  # ─── Audio ───
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
  };

  # ─── Peripherals ───
  hardware.bluetooth = { enable = true; powerOnBoot = true; };
  hardware.sensor.iio.enable = true;
  services.geoclue2.enable = true;

  # ─── Display ───
  services.displayManager.autoLogin = { enable = true; user = "hart-admin"; };

  # ─── Phone Tuning ───
  boot.kernel.sysctl = {
    "vm.laptop_mode" = 5;
    "vm.dirty_writeback_centisecs" = 6000;
  };

  services.journald.extraConfig = ''
    SystemMaxUse=50M
    MaxRetentionSec=3days
  '';
}
