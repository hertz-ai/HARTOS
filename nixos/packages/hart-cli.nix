{ lib, pkgs, hartSrc }:

# Nix derivation for the HART CLI tool
# Standalone command-line interface: `hart status`, `hart health`, `hart join`

let
  python = pkgs.python310;
  pythonEnv = python.withPackages (ps: with ps; [
    requests
  ]);
in
pkgs.stdenv.mkDerivation {
  pname = "hart-cli";
  version = "1.0.0";

  src = hartSrc + "/deploy/linux";

  installPhase = ''
    mkdir -p $out/bin

    # Install CLI script with correct shebang
    substitute hart-cli.py $out/bin/hart \
      --replace '#!/usr/bin/env python3' '#!${pythonEnv}/bin/python3'
    chmod +x $out/bin/hart
  '';

  meta = {
    description = "HART OS CLI tool";
    homepage = "https://hevolve.ai";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "hart";
  };
}
