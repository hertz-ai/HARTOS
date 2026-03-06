{ config, lib, pkgs, modulesPath, ... }:

# Minimal test ISO — no HART modules, just SSH
{
  imports = [
    "${modulesPath}/installer/cd-dvd/installation-cd-minimal.nix"
  ];

  boot.supportedFilesystems.zfs = lib.mkForce false;

  services.openssh = {
    enable = true;
    settings.PermitRootLogin = "yes";
    settings.PasswordAuthentication = true;
  };

  users.users.root.hashedPassword = lib.mkForce "$6$AfVhhgH5HUHO0Dww$rb/YNzNp6Z29KRrjtweGvBj3Wh/7E92tFREXONqdHxvHFEa1y1rlk3hMbux9jE5NdycqpwQhHokxgdcX1SH6B.";

  boot.kernelParams = [ "console=ttyS0,115200n8" ];

  isoImage.makeBiosBootable = lib.mkDefault true;

  system.stateVersion = "24.11";
}
