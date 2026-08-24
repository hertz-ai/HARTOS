{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — Curated app registry + offline-first App Store / Appearance backing
# ════════════════════════════════════════════════════════════════════════════
#
# WHY:
#   The App Store, the Appearance/wallpaper picker, and customisation all wrongly
#   needed the network: every catalog entry was a Flathub id whose ONLY install
#   path was an online `flatpak install` / `nix search`, and the wallpaper picker
#   read the FHS path /usr/share/backgrounds which does not exist on NixOS, so it
#   was EMPTY offline. None of these are network-by-nature: a curated catalog is
#   static data, and wallpapers are bundled assets.
#
# WHAT THIS MODULE DOES (additive, boot-safe, degrade-not-die):
#   1. Reads the ONE canonical catalog (./hart-app-catalog.json) — the same file
#      integrations/agent_engine/app_catalog.py serves to the offline store, so
#      Nix and Python share a SINGLE source of truth (no parallel list to drift).
#   2. Optionally BAKES the catalog's preinstall set into environment.systemPackages
#      (hart.apps.bakeMissing). Every package is attr-GUARDED: a renamed/absent
#      nixpkgs attr is silently dropped, never an eval error. OFF by default so the
#      module adds ZERO closure by default (the desktop ISO is at the ISO9660 size
#      ceiling — a deliberate, CI-size-checked opt-in grows it, never a surprise).
#   3. Can bundle offline wallpaper assets (hart.apps.wallpapers, opt-in) under
#      the NixOS-valid /run/current-system/sw/share/backgrounds path the backend
#      now scans. The GNOME desktop already ships gnome-backgrounds there, so the
#      backend scan alone makes Appearance offline; the option only GUARANTEES
#      wallpapers on a non-GNOME desktop. Off by default keeps server/edge clean.
#   4. Publishes the catalog's store path to the Model-Bus-style env var
#      HART_APP_CATALOG so any session-spawned tool resolves it deterministically;
#      the Python backend ALSO resolves it repo-relative, so this is a convenience,
#      not a dependency.
#
# DECENTRALISATION / PRIVACY (the lenses):
#   The store works fully with central + the internet OFF — the curated catalog is
#   local data and the preinstall set is on the box. The Flathub id is only an
#   OPTIONAL poster/source accelerant (app_poster.py), never a gatekeeper. No
#   network egress is introduced; the only online step remains the user-initiated
#   install of a CATALOG (non-preinstalled) app, which is explicit by definition.
#
# NEVER-FAIL: everything is gated on cfg.enable; a wrong value cannot brick boot.
#   Baking is opt-in and attr-guarded; the wallpaper package is attr-guarded; the
#   env var is inert. A missing catalog file degrades the bake set to empty.

let
  cfg = config.hart;
  apps = config.hart.apps;

  catalogFile = ./hart-app-catalog.json;

  # Where the app store installs to. MUST match app_installer.py's
  # `HART_APP_DIR` default ('${cfg.dataDir}/apps'), because that module creates
  # the flatpak root and this one has to make the DESKTOP able to see and launch
  # what it put there. Derived from cfg.dataDir rather than written out, so the
  # two cannot drift when the data dir moves.
  appsDir = "${cfg.dataDir}/apps";

  # Parse the canonical catalog. `or []` keeps eval safe if the schema is ever
  # reshaped; builtins.fromJSON on the committed file yields { apps = [ ... ]; }.
  catalog = builtins.fromJSON (builtins.readFile catalogFile);
  catalogApps = catalog.apps or [];

  # The preinstall set: entries flagged preinstall + carrying a nixpkgs attr.
  preinstallEntries = builtins.filter
    (a: (a.preinstall or false) && (a ? package) && a.package != "")
    catalogApps;

  # Map a package-name string to [ pkgs.<name> ] IFF the attr exists, else [].
  # `pkgs ? ${name}` is the dynamic has-attr guard — a catalog typo or a nixpkgs
  # rename can never fail eval, the entry is simply not baked (degrade-not-die).
  pkgFor = name: lib.optional (pkgs ? ${name}) pkgs.${name};

  preinstallPkgs = lib.concatMap (a: pkgFor a.package) preinstallEntries;

  # Offline wallpaper assets. gnome-backgrounds installs to
  # share/backgrounds/gnome, which lands at /run/current-system/sw/share/
  # backgrounds/gnome — exactly where the backend's wallpaper-collection scan
  # looks. Attr-guarded so a nixpkgs without the attr can never break eval.
  wallpaperPkgs = lib.optional (pkgs ? gnome-backgrounds) pkgs.gnome-backgrounds;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.apps = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Enable the curated offline-first app registry (catalog data + bundled
        wallpaper assets + the HART_APP_CATALOG pointer). A LOCAL feature, so it
        is ON by default (privacy-first: nothing leaves the device). Gated like
        every hart module on the master cfg.enable below, so it is still a pure
        no-op on a node that does not run HART.
      '';
    };

    bakeMissing = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Bake the catalog's preinstall set (the entries flagged preinstall=true
        with a nixpkgs attr) into environment.systemPackages, so those apps are
        on the box and the App Store shows Open (not a network Install) for them.

        OFF by default: the desktop ISO sits at the ISO9660 size ceiling, so
        growing the closure must be a DELIBERATE, CI-size-checked opt-in, never a
        surprise from importing this module. The apps already listed directly in
        desktop.nix are unaffected either way (NixOS de-dupes systemPackages); the
        only NET growth is the curated gap-fillers the catalog adds (e.g. VLC,
        Inkscape, Audacity). Every package is attr-guarded, so enabling this can
        never fail eval — at worst an unavailable attr is skipped.
      '';
    };

    wallpapers = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Bundle offline wallpaper assets (gnome-backgrounds) so the Appearance /
        wallpaper picker has content with the network OFF, under the NixOS-valid
        /run/current-system/sw/share/backgrounds path the shell backend scans.

        OFF by default to keep the headless server/edge closures clean (this
        module is imported by every variant). The GNOME desktop ALREADY ships
        gnome-backgrounds, so the backend's NixOS-valid path scan makes Appearance
        offline-functional on desktop WITHOUT this; turn it on (in desktop.nix)
        only to GUARANTEE bundled wallpapers on a non-GNOME desktop. NixOS de-dupes
        when GNOME already pulls it in.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (gated on the master cfg.enable like every hart module)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && apps.enable) {
    # Bake the preinstall set + the offline wallpaper assets (both opt-in knobs).
    # Purely additive; lib.optionals keeps each contribution empty when its knob
    # is off, so the DEFAULT import adds zero closure on every variant (only the
    # inert HART_APP_CATALOG pointer below) and can never grow the ISO by surprise.
    environment.systemPackages =
      (lib.optionals apps.bakeMissing preinstallPkgs)
      ++ (lib.optionals apps.wallpapers wallpaperPkgs);

    # Deterministic catalog pointer for any session-spawned shell tool. The store
    # path is the committed JSON copied into the nix store (read-only — we only
    # read it). The Python backend ALSO resolves the catalog repo-relative, so
    # this is a convenience, never the sole path.
    environment.sessionVariables.HART_APP_CATALOG = "${catalogFile}";

    # ── Make an INSTALLED app reachable from the desktop ────────────────────────
    # app_installer.py installs flatpaks at --user scope into
    # ${appsDir}/flatpak (FLATPAK_USER_DIR), because the backend runs sandboxed
    # as `hart` and cannot write /var/lib/flatpak without root/polkit. That is
    # correct, but on its own it makes a successful install UNUSABLE, and all
    # three reasons had to be fixed for "type firefox, get firefox" to work.
    # Measured end to end on the box 2026-08-24 after a real Flathub install:
    #
    #   1. PERMISSIONS. The installer created ${appsDir}/flatpak mode 0700
    #      hart:hart. The desktop session runs as a DIFFERENT user (hart-admin),
    #      so reading the exported .desktop was "Permission denied" — the app was
    #      invisible to the launcher that is supposed to start it. hart-admin is
    #      already in the `hart` group, so group-traversable dirs are enough; the
    #      setgid bit keeps that true for anything the installer creates later.
    #   2. DISCOVERY. The session's XDG_DATA_DIRS/PATH list ~/.local/share/flatpak
    #      and /var/lib/flatpak but NOT this root, so even a readable .desktop is
    #      never indexed.
    #   3. RESOLUTION (the one that still bit after 1 and 2). The exported
    #      .desktop runs `flatpak run ... org.mozilla.firefox`, and plain flatpak
    #      searches only its DEFAULT roots, so it answered
    #      "error: app/org.mozilla.firefox/x86_64/stable not installed" for an app
    #      that was installed. Exporting FLATPAK_USER_DIR points the launcher at
    #      the same root the installer used; verified by launching the exported
    #      binary as the session user (exit 0).
    #
    # extraInit rather than sessionVariables: these APPEND to variables the rest
    # of the session already set, and sessionVariables would clobber XDG_DATA_DIRS
    # and PATH wholesale.
    systemd.tmpfiles.rules = [
      "d ${appsDir} 2750 hart hart -"
      "d ${appsDir}/flatpak 2750 hart hart -"
    ];

    environment.extraInit = ''
      # One flatpak root for the OS: the one the app store installs into.
      export FLATPAK_USER_DIR="${appsDir}/flatpak"
      export XDG_DATA_DIRS="''${XDG_DATA_DIRS:+$XDG_DATA_DIRS:}${appsDir}/flatpak/exports/share"
      export PATH="$PATH:${appsDir}/flatpak/exports/bin"
    '';
  };
}
