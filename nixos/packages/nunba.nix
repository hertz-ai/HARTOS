{ lib, pkgs, fetchgit ? null, hartSrc ? null }:

# Nunba — HART OS Desktop Management App (launcher stub)
#
# Nunba source lives in a sibling repo (hertz-ai/Nunba) that is not
# checked out during HART OS ISO builds on GitHub Actions. Previous
# revisions tried `src = "${hartSrc}/../Nunba"` which silently falls
# back to a fetchgit with `sha256 = lib.fakeSha256` — every CI run
# errored with "do not know how to unpack source archive /../Nunba"
# and iso-desktop wouldn't build (see run 24642827236).
#
# The HART OS-side contract is just: `which nunba` must resolve to
# a launcher binary, and `nunba.desktop` must exist so
# AppRegistry + LiquidUI can discover the app. This stub satisfies
# both without needing the Nunba source tree. When a user runs
# `nunba` the first time, it downloads the real app (matching their
# platform) from the Nunba repo's releases and runs it. No heavy
# Python/Node/GTK build at ISO-build time.

let
  # Where the real installer lives. Public, stable URL.
  installerUrl = "https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba-x86_64.AppImage";
  installerFallback = "https://github.com/hertz-ai/Nunba/releases/latest";
in
pkgs.stdenv.mkDerivation {
  pname = "nunba";
  version = "stub-1.0.0";

  # No src — this is a pure launcher stub composed inline.
  src = null;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin $out/share/applications

    cat > $out/bin/nunba <<LAUNCHER
    #!${pkgs.bash}/bin/bash
    # Nunba launcher stub (HART OS bundle). First run downloads the
    # real AppImage from the Nunba repo's latest release and hands
    # off; subsequent runs exec the cached AppImage.
    set -eu
    CACHE_DIR="\''${XDG_CACHE_HOME:-\$HOME/.cache}/hart/nunba"
    APP="\$CACHE_DIR/Nunba-x86_64.AppImage"
    mkdir -p "\$CACHE_DIR"
    if [ ! -x "\$APP" ]; then
      echo "Nunba: first run — downloading launcher from GitHub…" >&2
      if ! ${pkgs.curl}/bin/curl -fL --retry 3 --connect-timeout 30 --speed-time 30 --speed-limit 2048 -o "\$APP" "${installerUrl}"; then
        echo "Nunba: download failed. See ${installerFallback}" >&2
        exit 1
      fi
      chmod +x "\$APP"
    fi
    export NUNBA_BACKEND_URL="\''${NUNBA_BACKEND_URL:-http://localhost:6777}"
    export NUNBA_PORT="\''${NUNBA_PORT:-5000}"
    exec "\$APP" "\$@"
    LAUNCHER
    chmod +x $out/bin/nunba

    cat > $out/share/applications/nunba.desktop <<DESKTOP
    [Desktop Entry]
    Name=Nunba
    Comment=HART OS agentic client — chat, communities, agents
    Exec=nunba
    Icon=nunba
    Terminal=false
    Type=Application
    Categories=Network;System;Utility;
    Keywords=hart;hevolve;ai;agents;chat;nunba;
    StartupNotify=true
    DESKTOP

    runHook postInstall
  '';

  meta = with lib; {
    description = "Nunba — HART OS agentic client (launcher stub)";
    longDescription = ''
      Thin launcher that first-time-downloads the Nunba AppImage from
      the sibling hertz-ai/Nunba repo's latest release. Keeps the
      HART OS ISO lean and decouples Nunba's release cadence from
      HART OS image builds.
    '';
    homepage = "https://hevolve.ai";
    license = licenses.mit;
    platforms = platforms.linux;
  };
}
