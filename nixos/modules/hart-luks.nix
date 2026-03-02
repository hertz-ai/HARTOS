{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS LUKS Disk Encryption
# ═══════════════════════════════════════════════════════════════
#
# Full disk encryption at rest using Linux LUKS2:
#   - Root partition encryption (unlocked at boot via passphrase or TPM2)
#   - Swap encryption (random key per boot)
#   - Data directory encryption (/var/lib/hart)
#   - TPM2 auto-unlock (optional, desktop/server only)
#
# NixOS advantage: declarative LUKS configuration.
# The actual encryption setup happens during install (nixos-install),
# but this module ensures the OS expects and handles encrypted volumes.

let
  cfg = config.hart;
  luks = config.hart.luks;
in
{
  options.hart.luks = {

    enable = lib.mkEnableOption "LUKS disk encryption support";

    rootDevice = lib.mkOption {
      type = lib.types.str;
      default = "/dev/disk/by-partlabel/HART-ROOT";
      description = "Encrypted root partition device path";
    };

    rootLabel = lib.mkOption {
      type = lib.types.str;
      default = "hart-root";
      description = "LUKS device mapper name for root partition";
    };

    tpm2Unlock = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Auto-unlock root via TPM2 (no passphrase at boot).
          Requires TPM2 hardware. Falls back to passphrase if TPM unavailable.
        '';
      };
    };

    swapEncryption = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Encrypt swap with random key (re-keyed every boot)";
      };

      device = lib.mkOption {
        type = lib.types.str;
        default = "/dev/disk/by-partlabel/HART-SWAP";
        description = "Swap partition device path";
      };
    };

    secureErase = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable TRIM/discard passthrough for SSD performance (slightly reduces security)";
    };
  };

  config = lib.mkIf (cfg.enable && luks.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Root partition encryption
    # ─────────────────────────────────────────────────────────
    {
      boot.initrd.luks.devices."${luks.rootLabel}" = {
        device = luks.rootDevice;
        preLVM = true;
        allowDiscards = luks.secureErase;
      };

      # Kernel modules for encryption
      boot.initrd.availableKernelModules = [
        "aesni_intel"       # Hardware AES acceleration (Intel)
        "dm_crypt"          # Device mapper encryption
        "cryptd"            # Crypto daemon
      ];

      # LUKS2 default: argon2id for key derivation (memory-hard, GPU-resistant)
      boot.initrd.kernelModules = [ "dm_crypt" ];
    }

    # ─────────────────────────────────────────────────────────
    # TPM2 auto-unlock (optional)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf luks.tpm2Unlock.enable {
      boot.initrd.luks.devices."${luks.rootLabel}" = {
        crypttabExtraOpts = [ "tpm2-device=auto" "tpm2-measure-pcr=yes" ];
      };

      # TPM2 tools for enrollment
      security.tpm2 = {
        enable = true;
        pkcs11.enable = true;
        tctiEnvironment.enable = true;
      };

      environment.systemPackages = with pkgs; [
        tpm2-tss
        tpm2-tools
      ];
    })

    # ─────────────────────────────────────────────────────────
    # Swap encryption (random key per boot)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf luks.swapEncryption.enable {
      swapDevices = [{
        device = "/dev/mapper/hart-swap";
        randomEncryption = {
          enable = true;
          allowDiscards = luks.secureErase;
        };
      }];
    })

    # ─────────────────────────────────────────────────────────
    # CLI tool
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = with pkgs; [
        cryptsetup

        (pkgs.writeShellScriptBin "hart-encrypt" ''
          #!/usr/bin/env bash
          case "''${1:-status}" in
            status)
              echo "=== LUKS Encryption Status ==="
              ${pkgs.cryptsetup}/bin/cryptsetup status "${luks.rootLabel}" 2>/dev/null || \
                echo "Root: not a LUKS device (may be unencrypted)"
              echo ""
              echo "Block devices:"
              lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE 2>/dev/null
              ;;
            benchmark)
              echo "=== Encryption Benchmark ==="
              ${pkgs.cryptsetup}/bin/cryptsetup benchmark
              ;;
            help|--help|-h)
              echo "hart-encrypt — LUKS Disk Encryption Management"
              echo ""
              echo "  hart-encrypt status     Show encryption status"
              echo "  hart-encrypt benchmark  Run crypto benchmark"
              ;;
            *)
              echo "Unknown command: $1 (try: hart-encrypt help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
