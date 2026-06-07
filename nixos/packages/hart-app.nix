{ lib, pkgs, hartSrc }:

# Nix derivation for the HART application
# Builds Python 3.10 environment with all dependencies
# Output: /nix/store/<hash>-hart-app/ with all source + venv

let
  python = pkgs.python310;

  # Python environment with all dependencies
  pythonEnv = python.withPackages (ps: with ps; [
    # Core framework
    flask
    waitress
    requests
    beautifulsoup4  # bs4 — imported at hart_intelligence_entry startup (web
                    # scraping / crawl4ai). Its absence is the "No module named
                    # 'bs4'" that killed hart-backend on the first ISO boot:
                    # waitress prints the import error and exits 0, so
                    # Restart=on-failure never fires and the backend stays dead.
    pydantic  # 1.10.x series from nixpkgs 24.11

    # Database
    sqlalchemy

    # Cryptography (Ed25519 identity, signing)
    cryptography

    # systemd integration — REQUIRED by every Type="notify" service
    # (hart-liquid-ui, hart-app-bridge, hart-model-bus). Their ExecStart
    # Python does `import systemd.daemon; systemd.daemon.notify('READY=1')`.
    # Without this the import raises ModuleNotFoundError, the process dies,
    # and systemd kills the unit at TimeoutStartSec — i.e. the LiquidUI server
    # and the app bridge never come up on a fresh ISO boot.
    systemd

    # GObject introspection (`import gi`) — REQUIRED by the LiquidUI glass
    # shell (cage kiosk client). It does `import gi; gi.require_version('Gtk',
    # '3.0'); gi.require_version('WebKit2','4.1')`. Missing pygobject3 crashes
    # the shell window → cage exits → the greeter falls back to GDM (which then
    # leaks the installer 'nixos' user). The GI *typelibs* (Gtk/WebKit2) are
    # put on GI_TYPELIB_PATH by hart-liquid-ui.nix's glassShell wrapper.
    pygobject3

    # LangChain ecosystem
    # langchain  # Pinned version; available in nixpkgs or via pip2nix

    # ML / AI
    numpy
    pillow

    # System metrics — LiquidUI's /api/shell/system/metrics (the CPU/RAM/disk
    # context that IS the adaptive shell's value: "explains WHY the GPU is
    # busy"). Imported lazily + guarded, so its absence only silently degrades
    # the dashboard rather than crashing the service — exactly the kind of
    # quietly-broken feature that hides without this.
    psutil

    # Utilities
    python-dateutil
    pyyaml
    jinja2
    aiohttp
    websockets

    # AutoGen (multi-agent framework)
    # autogen  # May need overlay or fetchPypi
  ]);
in
pkgs.stdenv.mkDerivation {
  pname = "hart-app";
  version = "1.0.0";

  src = lib.cleanSourceWith {
    src = hartSrc;
    filter = path: type:
      let
        baseName = baseNameOf path;
        relPath = lib.removePrefix (toString hartSrc + "/") (toString path);
      in
      # Exclude dev artifacts, tests, build outputs
      !(
        baseName == ".git" ||
        baseName == "__pycache__" ||
        baseName == ".idea" ||
        baseName == ".pycharm_plugin" ||
        baseName == "venv310" ||
        baseName == "autogen-0.2.37" ||
        baseName == "docs" ||
        baseName == "tests" ||
        baseName == "nixos" ||
        baseName == ".env" ||
        lib.hasSuffix ".pyc" baseName ||
        lib.hasSuffix ".egg-info" baseName ||
        lib.hasSuffix ".dist-info" baseName ||
        (lib.hasPrefix "agent_data/" relPath && lib.hasSuffix ".db" baseName)
      );
  };

  buildInputs = [ pythonEnv ];

  installPhase = ''
    mkdir -p $out
    cp -r . $out/

    # Make the Python environment accessible
    mkdir -p $out/bin
    ln -s ${pythonEnv}/bin/python $out/bin/python
    ln -s ${pythonEnv}/bin/python3 $out/bin/python3

    # Create agent_data directory structure
    mkdir -p $out/agent_data
  '';

  # Expose the Python env for service modules to reference
  passthru = {
    python = pythonEnv;
    inherit pythonEnv;
  };

  meta = {
    description = "HART OS — Crowdsourced Agentic Intelligence Platform";
    homepage = "https://hevolve.ai";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
