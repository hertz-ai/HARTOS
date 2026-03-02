{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Accessibility
# ═══════════════════════════════════════════════════════════════
#
# Declarative accessibility:
#   - Screen reader (Orca) — speech + braille output
#   - Font scaling — DPI-aware with per-app override
#   - High contrast theme — auto-applied to LiquidUI
#   - Keyboard navigation — full shell navigation without mouse
#   - Reduced motion — respects prefers-reduced-motion
#   - Large cursor — DPI-scaled pointer
#
# API-driven: LiquidUI reads accessibility state and adapts
# rendering (font size, contrast, animation toggles).

let
  cfg = config.hart;
  a11y = config.hart.accessibility;
in
{
  options.hart.accessibility = {

    enable = lib.mkEnableOption "HART OS accessibility features";

    screenReader = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Enable Orca screen reader at login";
      };
    };

    fontScale = lib.mkOption {
      type = lib.types.float;
      default = 1.0;
      description = "Global font scale factor (1.0 = 100%, 1.5 = 150%, 2.0 = 200%)";
    };

    highContrast = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable high contrast theme system-wide";
    };

    reducedMotion = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Disable animations and transitions";
    };

    largeCursor = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable large mouse cursor";
    };

    stickyKeys = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable sticky keys (modifier keys stay pressed)";
    };

    slowKeys = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Require keys to be held before registering";
      };

      delay = lib.mkOption {
        type = lib.types.int;
        default = 300;
        description = "Slow key delay in milliseconds";
      };
    };
  };

  config = lib.mkIf (cfg.enable && a11y.enable) (lib.mkMerge [

    # ─────────────────────────────────────────────────────────
    # Screen reader (Orca)
    # ─────────────────────────────────────────────────────────
    (lib.mkIf a11y.screenReader.enable {
      environment.systemPackages = with pkgs; [
        orca
        speech-dispatcher
        espeak-ng
      ];

      # Enable AT-SPI2 for screen reader access
      services.gnome.at-spi2-core.enable = true;

      # Auto-start Orca on login
      environment.variables = {
        GTK_MODULES = "gail:atk-bridge";
        QT_ACCESSIBILITY = "1";
        QT_LINUX_ACCESSIBILITY_ALWAYS_ON = "1";
      };
    })

    # ─────────────────────────────────────────────────────────
    # Font scaling + DPI
    # ─────────────────────────────────────────────────────────
    {
      # Write accessibility state for LiquidUI to read
      environment.etc."hart/accessibility.json".text = builtins.toJSON {
        font_scale = a11y.fontScale;
        high_contrast = a11y.highContrast;
        reduced_motion = a11y.reducedMotion;
        large_cursor = a11y.largeCursor;
        sticky_keys = a11y.stickyKeys;
        slow_keys = a11y.slowKeys.enable;
        slow_keys_delay = a11y.slowKeys.delay;
        screen_reader = a11y.screenReader.enable;
      };

      # X11/Wayland DPI scaling
      environment.variables = lib.mkIf (a11y.fontScale != 1.0) {
        GDK_SCALE = toString (builtins.ceil a11y.fontScale);
        GDK_DPI_SCALE = toString (1.0 / (builtins.ceil a11y.fontScale));
        QT_SCALE_FACTOR = toString a11y.fontScale;
      };
    }

    # ─────────────────────────────────────────────────────────
    # High contrast
    # ─────────────────────────────────────────────────────────
    (lib.mkIf a11y.highContrast {
      environment.systemPackages = with pkgs; [
        gnome-themes-extra    # Includes HighContrast theme
      ];

      environment.variables = {
        GTK_THEME = "HighContrast";
      };
    })

    # ─────────────────────────────────────────────────────────
    # Large cursor
    # ─────────────────────────────────────────────────────────
    (lib.mkIf a11y.largeCursor {
      environment.variables = {
        XCURSOR_SIZE = "48";
      };
    })

    # ─────────────────────────────────────────────────────────
    # CLI tool
    # ─────────────────────────────────────────────────────────
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-a11y" ''
          #!/usr/bin/env bash
          case "''${1:-status}" in
            status)
              echo "=== HART OS Accessibility ==="
              cat /etc/hart/accessibility.json 2>/dev/null | ${pkgs.jq}/bin/jq . || \
                echo "Accessibility config not found"
              ;;
            help|--help|-h)
              echo "hart-a11y — HART OS Accessibility"
              echo ""
              echo "  hart-a11y status   Show current accessibility settings"
              echo ""
              echo "Settings are declarative in NixOS configuration."
              echo "Runtime overrides available via API:"
              echo "  PUT /api/shell/accessibility"
              ;;
            *)
              echo "Unknown command: $1 (try: hart-a11y help)"
              exit 1
              ;;
          esac
        '')
      ];
    }
  ]);
}
