{ config, lib, ... }:

# HART OS Nunba Module — options only (no separate daemon, no AppImage)
#
# The Nunba React UI is NOT a separate app/daemon any more. It is compiled to a
# native static dist by nixos/packages/nunba.nix ($out/lib/nunba/static) and
# served from inside the LiquidUI glass shell via hart-liquid-ui.nix's
# NUNBA_STATIC_DIR (hart.liquidUI.embedNunba). The previous
# `nunba --server-only` user service ran a runtime-downloaded ~200 MB AppImage
# on :5000 — a redundant SECOND copy of the UI outside the OS closure. It is
# REMOVED: one UI path (the native dist served by LiquidUIService), no AppImage.
#
# This module now only carries the `hart.nunba.*` options, which other modules
# still read — hart-liquid-ui.nix uses `config.hart.nunba.port` (glass-shell
# fallback URL) and `config.hart.nunba.enable` (the embedNunba default). Keeping
# the options here keeps those references resolvable without a parallel path.

{
  # ─── Options ──────────────────────────────────────────────
  options.hart.nunba = {
    enable = lib.mkEnableOption "Nunba React UI (served natively via LiquidUI embedNunba)";

    port = lib.mkOption {
      type = lib.types.port;
      default = 5000;
      description = ''
        Legacy Nunba port. Retained as the value hart-liquid-ui.nix reads for the
        glass-shell fallback URL. No daemon listens here any more — the UI is
        served by LiquidUIService from the static dist (hart.liquidUI.embedNunba).
      '';
    };

    addToFavorites = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Add a Nunba shortcut to the GNOME dock (not needed — LiquidUI IS the shell)";
    };
  };

  # ─── Configuration ────────────────────────────────────────
  # Only the optional GNOME-dock favorites remain (off by default). The AppImage
  # package install + the `nunba --server-only` systemd service are gone.
  config = lib.mkIf (config.hart.enable && config.hart.nunba.enable && config.hart.nunba.addToFavorites) {
    programs.dconf = {
      enable = true;
      profiles.user.databases = [{
        settings = {
          "org/gnome/shell" = {
            favorite-apps = [
              "firefox.desktop"
              "org.gnome.Terminal.desktop"
              "org.gnome.Nautilus.desktop"
            ];
          };
        };
      }];
    };
  };
}
