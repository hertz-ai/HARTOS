# HART OS — Input Method Editor (IME)
#
# CJK and multilingual text input via fcitx5 (default) or ibus.
# Supports: Chinese (Pinyin), Japanese (Mozc), Korean (Hangul),
#           and more via plugin addons.
#
# CLI: hart-ime status|list|switch <layout>

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.ime;
in
{
  options.hart.ime = {
    enable = lib.mkEnableOption "HART OS input method editor (IME)";

    engine = lib.mkOption {
      type = lib.types.enum [ "fcitx5" "ibus" ];
      default = "fcitx5";
      description = "IME framework: fcitx5 (recommended) or ibus.";
    };

    inputMethods = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = ''
        Input methods to install. Available:
        pinyin (Chinese), mozc (Japanese), hangul (Korean),
        anthy (Japanese alt), chewing (Traditional Chinese),
        libpinyin (Chinese alt).
      '';
      example = [ "pinyin" "mozc" "hangul" ];
    };

    defaultLayouts = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "us" ];
      description = "Default keyboard layouts.";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    # ── fcitx5 engine ──
    (lib.mkIf (cfg.engine == "fcitx5") {
      i18n.inputMethod = {
        enabled = "fcitx5";
        fcitx5.addons = with pkgs; lib.filter (p: p != null) [
          fcitx5-gtk                                              # GTK integration
          (if builtins.elem "pinyin" cfg.inputMethods
           then fcitx5-chinese-addons else null)                  # Chinese Pinyin
          (if builtins.elem "mozc" cfg.inputMethods
           then fcitx5-mozc else null)                            # Japanese
          (if builtins.elem "hangul" cfg.inputMethods
           then fcitx5-hangul else null)                          # Korean
          (if builtins.elem "anthy" cfg.inputMethods
           then fcitx5-anthy else null)                           # Japanese alt
          (if builtins.elem "chewing" cfg.inputMethods
           then fcitx5-chewing else null)                         # Traditional Chinese
        ];
      };

      environment.variables = {
        INPUT_METHOD = "fcitx";
        GTK_IM_MODULE = "fcitx";
        QT_IM_MODULE = "fcitx";
        XMODIFIERS = "@im=fcitx";
        SDL_IM_MODULE = "fcitx";
        GLFW_IM_MODULE = "ibus";   # GLFW uses ibus protocol even with fcitx
      };
    })

    # ── ibus engine ──
    (lib.mkIf (cfg.engine == "ibus") {
      i18n.inputMethod = {
        enabled = "ibus";
        ibus.engines = with pkgs.ibus-engines; lib.filter (p: p != null) [
          (if builtins.elem "pinyin" cfg.inputMethods
           then libpinyin else null)
          (if builtins.elem "mozc" cfg.inputMethods
           then mozc else null)
          (if builtins.elem "hangul" cfg.inputMethods
           then hangul else null)
          (if builtins.elem "anthy" cfg.inputMethods
           then anthy else null)
          m17n                                                    # Multilingual base
        ];
      };

      environment.variables = {
        GTK_IM_MODULE = "ibus";
        QT_IM_MODULE = "ibus";
        XMODIFIERS = "@im=ibus";
      };
    })

    # ── Keyboard layout configuration ──
    {
      services.xserver.xkb.layout = lib.concatStringsSep "," cfg.defaultLayouts;
      services.xserver.xkb.options = "grp:alt_shift_toggle";

      # ── CLI tool ──
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-ime" ''
          case "''${1:-status}" in
            status)
              echo "=== Input Method ==="
              echo "Engine: ${cfg.engine}"
              echo "Layouts: ${lib.concatStringsSep ", " cfg.defaultLayouts}"
              echo "Input methods: ${lib.concatStringsSep ", " cfg.inputMethods}"
              echo ""
              echo "Current layout:"
              ${pkgs.xorg.setxkbmap}/bin/setxkbmap -query 2>/dev/null || echo "  (X11 not running)"
              ;;
            list)
              echo "Available keyboard layouts:"
              ${pkgs.systemd}/bin/localectl list-x11-keymap-layouts 2>/dev/null | head -50
              ;;
            switch)
              layout="''${2:-us}"
              ${pkgs.xorg.setxkbmap}/bin/setxkbmap "$layout"
              echo "Switched to layout: $layout"
              ;;
            *)
              echo "Usage: hart-ime {status|list|switch <layout>}"
              ;;
          esac
        '')
      ];
    }
  ]);
}
