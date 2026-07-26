#!/bin/bash
set +e
REPO="/mnt/c/Users/sathi/PycharmProjects/HARTOS"
echo "===any existing cargoLock.outputHashes in the tree (precedent)?==="
grep -rnE "outputHashes|allowBuiltinFetchGit|cargoLock" "$REPO/nixos" 2>/dev/null | grep -vE "\.git/" | head -30
echo ""
echo "===how nixpkgs 50ab793 importCargoLock treats git deps w/o outputHashes (the error msg)==="
NIXPKGS=$(find /root -maxdepth 6 -path '*build-support/rust*' -name 'import-cargo-lock.nix' 2>/dev/null | head -1)
echo "import-cargo-lock.nix = $NIXPKGS"
grep -nE "outputHashes|getName|fetchgit|No hash was found|builtins.fetchGit|allowBuiltinFetchGit" "$NIXPKGS" 2>/dev/null | head -25
echo ""
echo "===winit+smithay feature interaction: does winit.rs use delegate_dispatch2 (raw Dispatch) that would clash w/ wayland.rs handlers if BOTH on?==="
grep -nE "delegate_dispatch2|delegate_dispatch|smithay::delegate" "$REPO/compositor/src/winit.rs" | head
echo "--- does anything cfg-gate winit and smithay as mutually exclusive? ---"
grep -nE 'cfg\(feature|cfg\(all|cfg\(not' "$REPO/compositor/src/main.rs" | head -20
