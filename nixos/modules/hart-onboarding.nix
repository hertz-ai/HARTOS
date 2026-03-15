{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# HART OS Onboarding Module — "Light Your HART"
#
# Launches the native GTK4/libadwaita onboarding ceremony
# on the user's first graphical login. The ceremony runs
# BEFORE any web server or agent — pure native UI.
#
# Flow:
#   1. hart-first-boot.service completes (system level)
#   2. User logs into GNOME (GDM auto-login on first boot)
#   3. hart-onboarding.desktop autostart launches this app
#   4. User completes ceremony -> HART identity sealed
#   5. App exits -> normal desktop session begins
#
# After first completion, the autostart checks `--check` flag
# and exits immediately if already onboarded.

let
  cfg = config.hart;

  # Python with GTK4 + libadwaita + HART deps
  pythonForOnboarding = pkgs.python310.withPackages (ps: with ps; [
    pygobject3
    pycairo
    # HART deps for the onboarding backend
    sqlalchemy
    cryptography
  ]);

  # The onboarding script wrapper
  onboardingBin = pkgs.writeShellScript "hart-onboarding" ''
    # Exit silently if already onboarded
    if ${pythonForOnboarding}/bin/python3 \
        ${hartSrc}/integrations/agent_engine/native_onboarding.py \
        --user-id "$(id -u)" --check 2>/dev/null; then
      exit 0
    fi

    # Set GTK/Adwaita dark theme
    export GTK_THEME=Adwaita:dark
    export ADW_DISABLE_PORTAL=1

    # Ensure HART Python path
    export PYTHONPATH="${hartSrc}:''${PYTHONPATH:-}"
    export HART_INSTALL_DIR="${hartSrc}"

    # Launch the ceremony
    exec ${pythonForOnboarding}/bin/python3 \
      ${hartSrc}/integrations/agent_engine/native_onboarding.py \
      --user-id "$(id -u)"
  '';

  # XDG autostart desktop entry
  onboardingDesktop = pkgs.writeTextDir
    "etc/xdg/autostart/hart-onboarding.desktop"
    ''
      [Desktop Entry]
      Type=Application
      Name=Light Your HART
      Comment=HART OS first-time identity ceremony
      Exec=${onboardingBin}
      Icon=preferences-system
      Terminal=false
      X-GNOME-Autostart-Phase=Application
      X-GNOME-Autostart-enabled=true
      OnlyShowIn=GNOME;
      NoDisplay=true
    '';

  # Desktop menu entry (for re-viewing identity)
  identityDesktop = pkgs.writeTextDir
    "share/applications/hart-identity.desktop"
    ''
      [Desktop Entry]
      Type=Application
      Name=My HART Identity
      Comment=View your HART identity card
      Exec=${onboardingBin}
      Icon=contact-new
      Categories=System;Settings;
      Terminal=false
    '';

in
{
  config = lib.mkIf (cfg.enable && cfg.variant != "server") {

    # Install the autostart entry, desktop shortcut, and GTK4/libadwaita
    environment.systemPackages = [
      onboardingDesktop
      identityDesktop
      pkgs.gtk4
      pkgs.libadwaita
      pkgs.gobject-introspection
    ];

    # GSettings schema for Adwaita dark theme preference
    programs.dconf.profiles.user.databases = [{
      settings."org/gnome/desktop/interface" = {
        color-scheme = "prefer-dark";
      };
    }];
  };
}
