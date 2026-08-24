{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# HART OS Nunba Module — the FULL Nunba (Python + React) as a NATIVE OS daemon
#
# Steward directive (2026-07-09): "all existing Nunba + HARTOS functionalities
# should be natively wired to the HART OS" — package the exact Nunba desktop build
# as an in-process OS daemon on a UNIX SOCKET (no host TCP port), calling the native
# HARTOS backend (:6777). This module runs that daemon; nixos/packages/nunba.nix
# builds it (HARTOS/GUI/ML-excluded); hart-liquid-ui.nix reverse-proxies the socket
# same-origin so every 'route' panel (/social, /agents, …) resolves — retiring the
# old React-only NUNBA_STATIC_DIR path that LOST all of Nunba's Python.
#
# The daemon runs `main.py` DIRECTLY — main.py IS the headless server (app.py is
# only the pywebview GUI wrapper, never launched here). No `--server-only` flag, no
# reinvention (Gate 4). main.py binds unix:$HART_NUNBA_SOCKET (its existing
# Hypercorn/Waitress block, Nunba cb849ba9) and reaches native HARTOS via
# HARTOS_BACKEND_URL. NUNBA_BUNDLED is UNSET so hartos_backend_adapter takes the
# explicit-URL HTTP path to :6777.
#
# ── ZERO-REGRESSION / STAGING ──
# hart.nunba.enable defaults FALSE, so the desktop ISO closure does NOT build the
# heavy Nunba Python+webpack package until CI has (a) pinned the FODs
# (nunbaRev/nunbaHash/npmDepsHash) and (b) walked the import-domino build loop green
# on `nix build .#packages.<sys>.nunba`. Flip it on (desktop.nix) only after that —
# the current React-static nightly stays byte-for-byte unchanged until then.

let
  cfg = config.hart;
  # ONE package expression (the same file hart-liquid-ui.nix callPackages for the
  # NUNBA_STATIC_DIR floor) → the daemon and the floor share the SAME store path.
  nunbaPkg = pkgs.callPackage ../packages/nunba.nix { inherit hartSrc; };
  # The NATIVE HARTOS tree (the SAME derivation the backend runs). Nunba's own code
  # imports HARTOS packages directly — models/catalog.py does an unguarded
  # `import integrations.service_tools.model_catalog` (→ integrations/__init__ →
  # core/__init__), because HART OS OWNS the server-managed model stack. On desktop
  # cx_Freeze bundles HARTOS into Nunba; on HART OS we instead put HARTOS's native
  # tree on the daemon's PYTHONPATH so `import core`/`import integrations` resolve to
  # the ONE native HARTOS — no re-bundled copy (steward: "HARTOS shd not be
  # transitively bundled again into Nunba"). hartApp is already in the closure (the
  # backend uses it), so this adds no build.
  hartApp = config.hart.package;
  # ── `nunba` ON PATH: the install-time half of the native-app contract ──
  # core/platform/bootstrap.py:_register_native_apps registers a NATIVE_APPS entry
  # ONLY when `shutil.which(entry['exec'])` resolves, and Nunba's manifest sets
  # exec='nunba'. tests/unit/test_platform_apps.py guards the BOOTSTRAP half and
  # names e2e-os-smoke.sh #16/#17 as the INSTALL half. The daemon above starts from
  # an ABSOLUTE store path, so nothing was ever named `nunba` on PATH: on a booted
  # desktop AppRegistry silently never registered Nunba (it is not a "first-class
  # native agentic client the runtime can dispatch to" at all), and #16 has been red
  # on every E2E run since desktop.nix PROMOTED nunba.enable on 2026-08-14.
  #
  # In OS mode the daemon binds a unix socket with NO host TCP port and
  # hart-liquid-ui reverse-proxies it same-origin, so the Nunba surface IS the shell
  # URL. Read the ONE option (hart.liquidUI.port) instead of repeating the number,
  # and honour HART_SHELL_URL: the same override both glass-shell renderers read.
  nunbaLauncher = pkgs.writeShellScriptBin "nunba" ''
    exec ${pkgs.xdg-utils}/bin/xdg-open \
      "''${HART_SHELL_URL:-http://localhost:${toString (config.hart.liquidUI.port or 6800)}}"
  '';
  # share/applications/nunba.desktop, which is what the shell catalog launches Nunba
  # from outside LiquidUI (e2e #17 looks for exactly this filename under
  # /run/current-system/sw/share/applications). Same writeTextDir pattern as
  # hart-onboarding.nix's identityDesktop, and Exec is the ABSOLUTE store path for
  # the same reason the cage floor launcher is (a bare name is not on the session's
  # PATH, which is how that floor once died rc=127).
  nunbaDesktopItem = pkgs.writeTextDir
    "share/applications/nunba.desktop"
    ''
      [Desktop Entry]
      Type=Application
      Name=Nunba
      Comment=HART OS companion: chat, communities, agents
      Exec=${nunbaLauncher}/bin/nunba
      Icon=hart
      Categories=Network;Chat;
      Terminal=false
    '';
in
{
  # ─── Options ──────────────────────────────────────────────
  options.hart.nunba = {
    enable = lib.mkEnableOption "Nunba full daemon (Python + React) as a native HART OS unix-socket service";

    socket = lib.mkOption {
      type = lib.types.str;
      default = "/run/hart/nunba.sock";
      description = ''
        Unix socket the Nunba daemon binds (main.py HART_NUNBA_SOCKET) and LiquidUI
        reverse-proxies. Under the shared /run/hart (0750 hart hart) tmpfs so both
        the daemon and the glass shell (both run as `hart`) can reach it — no host
        TCP port is occupied.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 5000;
      description = ''
        Legacy Nunba TCP port. Retained as the value hart-liquid-ui.nix reads for the
        glass-shell fallback URL. No daemon listens here in OS mode — the daemon binds
        the unix socket (hart.nunba.socket); this is only the desktop-mode default.
      '';
    };

    addToFavorites = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Add a Nunba shortcut to the GNOME dock (not needed — LiquidUI IS the shell)";
    };
  };

  # ─── Configuration ────────────────────────────────────────
  config = lib.mkMerge [

    # ── The native daemon (the actual wiring) ──
    (lib.mkIf (cfg.enable && cfg.nunba.enable) {
      # Ensure the shared socket dir exists even if no other /run/hart producer is
      # enabled (idempotent with the other modules' identical rule).
      systemd.tmpfiles.rules = [ "d /run/hart 0750 hart hart -" ];

      # Gated on the SAME condition as the daemon: `nunba` only appears on PATH on a
      # system that actually runs Nunba, so AppRegistry's which() probe stays honest
      # (it must not register the app on a variant where the daemon is absent).
      environment.systemPackages = [ nunbaLauncher nunbaDesktopItem ];

      systemd.services.hart-nunba = {
        description = "Nunba native daemon (full Python + React, unix socket)";
        documentation = [ "https://github.com/hertz-ai/Nunba" ];
        # After the backend it proxies to; part of the hart target (never a boot gate).
        after = [ "hart-backend.service" ];
        wants = [ "hart-backend.service" ];
        partOf = [ "hart.target" ];
        wantedBy = [ "hart.target" ];

        environment = {
          # Inbound: bind the unix socket — NO host TCP port (steward directive).
          HART_NUNBA_SOCKET = cfg.nunba.socket;
          # Outbound: reach the ONE native HARTOS backend (no re-bundled copy).
          HARTOS_BACKEND_URL = "http://127.0.0.1:${toString cfg.ports.backend}";
          # UNSET NUNBA_BUNDLED → hartos_backend_adapter takes the explicit-URL HTTP
          # path to native HARTOS (not an in-bundle import).
          # OS mode is SSE-primary; the WAMP router stays deferred (main.py's
          # _wamp_is_needed() gate) so no :8088 host port either.
          #
          # Native HARTOS on the path (NOT a copy): Nunba's models.catalog /
          # models.orchestrator delegate to HARTOS's integrations.service_tools.*
          # (which pull core/*). Nunba's own tree is sys.path[0] (WorkingDirectory)
          # so Nunba's modules win any name overlap (e.g. the desktop/ package both
          # repos have); HARTOS's core/integrations resolve from here. This is the
          # single authoritative HARTOS the backend also runs — no double-bundle.
          PYTHONPATH = "${hartApp}";
          PYTHONDONTWRITEBYTECODE = "1";
          PYTHONUNBUFFERED = "1";
          # The ONE node database, same value as hart-backend/hart-agent/
          # hart-discovery/hart-compute-mesh/hart-app-bridge. This unit was the
          # only HARTOS-importing service WITHOUT it, so every DB read inside
          # this process auto-detected some other (empty) path. Measured on the
          # real box 2026-08-22: hart-backend saw users=1 while THIS process
          # resolved 0 users, so core/event_attribution's single-tenant
          # fallback returned None and the P3a guard refused every
          # agent.action.completed this process emitted -- 1404 refusals in
          # 5 minutes -- which is exactly the agents panel sitting on its
          # "reconnecting / retry" button while /api/social/dashboard/agents
          # (served by hart-backend, which HAS the variable) answered 200.
          # One writer + readers is SQLite-safe here: WAL is enabled and the
          # sibling services already share this path.
          HEVOLVE_DB_PATH = "${cfg.dataDir}/hevolve_database.db";
        };

        serviceConfig = {
          Type = "simple";
          User = "hart";
          Group = "hart";
          WorkingDirectory = "${nunbaPkg}/lib/nunba";
          # Call main.py directly via the package's Python env (same pattern as
          # hart-backend.nix). main.py IS the headless server; it binds
          # unix:$HART_NUNBA_SOCKET from its existing Hypercorn/Waitress block.
          ExecStart = "${nunbaPkg.python}/bin/python ${nunbaPkg}/lib/nunba/main.py";

          Restart = "on-failure";
          RestartSec = 5;
          # Daemon imports Nunba's Python service layer (no ML) — give it headroom
          # but not the backend's ML budget. No WatchdogSec (Hypercorn/Waitress do
          # not sd_notify WATCHDOG). Restart=on-failure covers real crashes.
          TimeoutStartSec = 45;
          TimeoutStopSec = 15;

          # ── Security hardening (mirror hart-backend.nix) ──
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          # /run/hart must be writable so main.py can create/unlink the socket under
          # ProtectSystem=strict (which mounts the FS read-only otherwise).
          ReadWritePaths = [
            "/run/hart"
            cfg.dataDir
            cfg.logDir
          ];
          PrivateTmp = true;
          ProtectClock = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectKernelLogs = true;
          # AF_UNIX for the socket; AF_INET/AF_INET6 for the → :6777 backend call.
          RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
          SystemCallFilter = [ "@system-service" ];
          MemoryDenyWriteExecute = false;
          LockPersonality = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          # NO PrivateNetwork: the daemon MUST reach 127.0.0.1:6777 (native HARTOS)
          # on the host loopback; a private netns would isolate it from the backend.

          MemoryMax = if cfg.variant == "edge" then "512M" else "2G";
          MemoryHigh = if cfg.variant == "edge" then "384M" else "1536M";
          CPUWeight = if cfg.variant == "edge" then 50 else 100;
          TasksMax = if cfg.variant == "edge" then 64 else 256;

          StandardOutput = "journal";
          StandardError = "journal";
          SyslogIdentifier = "hart-nunba";
        };
      };
    })

    # ── Optional GNOME-dock favorites (unchanged; off by default) ──
    (lib.mkIf (cfg.enable && cfg.nunba.enable && cfg.nunba.addToFavorites) {
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
    })
  ];
}
