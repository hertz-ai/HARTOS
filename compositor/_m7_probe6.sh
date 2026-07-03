#!/bin/bash
set +e
REPO="/mnt/c/Users/sathi/PycharmProjects/HARTOS"
echo "===does wayland.rs invoke delegate_dispatch2 (REQUIRED to actually serve protocols on the DRM State)?==="
grep -nE "delegate_dispatch2|delegate_dispatch|delegate_" "$REPO/compositor/src/wayland.rs"
echo "  (if empty: wayland.rs has handlers but NEVER wires Dispatch -> can't serve clients yet)"
echo ""
echo "===locate importCargoLock in any nix store / nixpkgs checkout==="
find / -maxdepth 9 -name 'import-cargo-lock.nix' -path '*rust*' 2>/dev/null | head -5
find / -maxdepth 9 -name 'cargo-lock' -path '*rust*' -type d 2>/dev/null | head -5
echo ""
echo "===is there a nixpkgs source unpacked anywhere we can read the fetchgit/outputHashes logic?==="
ls -d /root/.cache/nix 2>/dev/null
nix --version 2>/dev/null || echo "no nix on PATH in this shell"
echo ""
echo "===confirm: building --features smithay ALONE excludes ipc.rs + screencopy.rs (both cfg winit)==="
head -55 "$REPO/compositor/src/ipc.rs" | grep -nE 'cfg\(feature'
head -55 "$REPO/compositor/src/screencopy.rs" | grep -nE 'cfg\(feature'
