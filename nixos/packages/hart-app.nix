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
    pydantic  # 1.10.x series from nixpkgs 24.11

    # Database
    sqlalchemy

    # Cryptography (Ed25519 identity, signing)
    cryptography

    # LangChain ecosystem
    # langchain  # Pinned version; available in nixpkgs or via pip2nix

    # ML / AI
    numpy
    pillow

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
