# HART OS — Thunderbird Email Client
#
# Full-featured email client with XDG default mail handler
# registration and GNOME Keyring integration.
#
# CLI: hart-email status|launch

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.email;
in
{
  options.hart.email = {
    enable = lib.mkEnableOption "HART OS email client (Thunderbird)";

    defaultMailer = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Set Thunderbird as the default mailto: handler.";
    };

    calendarIntegration = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable Lightning calendar add-on support.";
    };
  };

  config = lib.mkIf cfg.enable {
    # ── Thunderbird package ──
    environment.systemPackages = with pkgs; [
      thunderbird

      (writeShellScriptBin "hart-email" ''
        case "''${1:-status}" in
          status)
            echo "=== HART OS Email Client ==="
            echo "Client:    Thunderbird"
            echo "Default:   ${if cfg.defaultMailer then "yes" else "no"}"
            echo "Calendar:  ${if cfg.calendarIntegration then "enabled" else "disabled"}"
            echo ""
            if command -v thunderbird >/dev/null 2>&1; then
              echo "Thunderbird: installed ($(thunderbird --version 2>/dev/null || echo 'version unknown'))"
            else
              echo "Thunderbird: not found"
            fi
            echo ""
            echo "Default mailto handler:"
            xdg-mime query default x-scheme-handler/mailto 2>/dev/null || echo "  not configured"
            ;;
          launch)
            echo "Launching Thunderbird..."
            thunderbird "$@" &
            disown
            ;;
          help|--help|-h)
            echo "hart-email — HART OS Email Management"
            echo ""
            echo "  hart-email status    Show email client status"
            echo "  hart-email launch    Launch Thunderbird"
            ;;
          *)
            echo "Unknown command: $1 (try: hart-email help)"
            exit 1
            ;;
        esac
      '')
    ];

    # ── XDG default mail handler ──
    xdg.mime.defaultApplications = lib.mkIf cfg.defaultMailer {
      "x-scheme-handler/mailto" = "thunderbird.desktop";
      "message/rfc822" = "thunderbird.desktop";
      "x-scheme-handler/mid" = "thunderbird.desktop";
    };

    # ── GNOME Keyring for credential storage ──
    services.gnome.gnome-keyring.enable = true;
    security.pam.services.login.enableGnomeKeyring = true;

    # ── Thunderbird system-wide policies ──
    environment.etc."thunderbird/policies/policies.json" = {
      text = builtins.toJSON {
        policies = {
          DisableTelemetry = true;
          DisableFirefoxStudies = true;
          OfferToSaveLogins = false;
          HardwareAcceleration = true;
          ExtensionSettings = lib.mkIf cfg.calendarIntegration {
            # Lightning calendar is built-in since TB 78+; ensure it stays enabled
            "{e2fda1a4-762b-4020-b5ad-a41df1933103}" = {
              installation_mode = "normal_installed";
            };
          };
        };
      };
    };
  };
}
