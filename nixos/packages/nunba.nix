{ lib, pkgs, fetchgit ? null, hartSrc ? null }:

# Nunba — HART OS Desktop Management App
# PyWebView (GTK/WebKit2) + React 18 SPA + Flask backend
# Provides: full node management, chat, communities, agent goals, settings

let
  # Python environment with PyWebView and Flask dependencies
  pythonEnv = pkgs.python310.withPackages (ps: with ps; [
    flask
    flask-cors
    waitress
    requests
    pywebview      # GTK/WebKit2 on Linux
    pydantic
    sqlalchemy
    cryptography
  ]);

  # Node.js for building the React frontend
  nodejs = pkgs.nodejs_20;

in
pkgs.stdenv.mkDerivation rec {
  pname = "nunba";
  version = "1.0.0";

  # Source: Nunba directory (sibling repo or embedded)
  # In production: fetchGit or fetchFromGitHub
  # For now: use nix path or override via overlay
  src = if hartSrc != null
    then "${hartSrc}/../Nunba"
    else pkgs.fetchgit {
      url = "https://github.com/hevolve-ai/nunba.git";
      rev = "HEAD";
      sha256 = lib.fakeSha256;  # Replace with actual hash on first build
    };

  nativeBuildInputs = [
    nodejs
    pkgs.nodePackages.npm
  ];

  buildInputs = [
    pythonEnv
    # GTK/WebKit2 for PyWebView Linux backend
    pkgs.gtk3
    pkgs.webkitgtk_4_1
    pkgs.gobject-introspection
    pkgs.glib
    pkgs.wrapGAppsHook
  ];

  # Build React frontend
  buildPhase = ''
    # Skip if landing-page doesn't exist (Python-only mode)
    if [ -d "landing-page" ]; then
      echo "Building React frontend..."
      cd landing-page
      npm ci --ignore-scripts 2>/dev/null || npm install --ignore-scripts
      npm run build
      cd ..
    fi
  '';

  installPhase = ''
    mkdir -p $out/lib/nunba
    mkdir -p $out/bin
    mkdir -p $out/share/applications
    mkdir -p $out/share/icons/hicolor/256x256/apps

    # Copy Python backend
    cp -r *.py $out/lib/nunba/ 2>/dev/null || true
    cp -r api/ $out/lib/nunba/api/ 2>/dev/null || true
    cp -r adapters/ $out/lib/nunba/adapters/ 2>/dev/null || true
    cp -r config/ $out/lib/nunba/config/ 2>/dev/null || true

    # Copy React build
    if [ -d "landing-page/build" ]; then
      cp -r landing-page/build $out/lib/nunba/static
    fi

    # Copy icon if available
    if [ -f "assets/icon.png" ]; then
      cp assets/icon.png $out/share/icons/hicolor/256x256/apps/nunba.png
    fi

    # Create launcher script
    cat > $out/bin/nunba << 'LAUNCHER'
#!/bin/bash
# Nunba — HART OS Management App
export PYWEBVIEW_GUI="gtk"
export NUNBA_BACKEND_URL="http://localhost:6777"
export NUNBA_PORT="''${NUNBA_PORT:-5000}"
exec ${pythonEnv}/bin/python /run/current-system/sw/lib/nunba/main.py "$@"
LAUNCHER
    chmod +x $out/bin/nunba

    # Create .desktop file
    cat > $out/share/applications/nunba.desktop << 'DESKTOP'
[Desktop Entry]
Name=HART
Comment=HART OS Management — Chat, Communities, Agents
Exec=nunba
Icon=nunba
Terminal=false
Type=Application
Categories=Network;System;Utility;
Keywords=hart;hevolve;ai;agents;chat;
StartupNotify=true
DESKTOP
  '';

  # Expose Python env for NixOS module to reference
  passthru = {
    python = pythonEnv;
  };

  meta = with lib; {
    description = "Nunba — HART OS Desktop Management App";
    homepage = "https://hevolve.ai";
    license = licenses.mit;
    platforms = platforms.linux;
  };
}
