#!/bin/bash
set -euo pipefail

# Source nix environment
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh 2>/dev/null || true

echo "=== Nix store check ==="
nix store info 2>&1 | head -3

echo ""
echo "=== Checking flake ==="
cd /mnt/c/Users/sathi/PycharmProjects/HARTOS

# Set git safe directory
git config --global --add safe.directory /mnt/c/Users/sathi/PycharmProjects/HARTOS 2>/dev/null || true

# Check if flake.nix is git-tracked
if ! git ls-files --error-unmatch nixos/flake.nix >/dev/null 2>&1; then
    echo "WARNING: nixos/flake.nix not tracked by git. Adding it..."
    git add nixos/flake.nix nixos/flake.lock 2>/dev/null || true
fi

# Build from the nixos subdirectory
echo "=== Starting ISO build from nixos/ ==="
cd nixos
nix build .#iso-server --show-trace --print-build-logs 2>&1

echo ""
echo "=== Build complete ==="
ls -lh result/iso/*.iso 2>/dev/null || ls -lh result/ 2>/dev/null || echo "No result directory"
