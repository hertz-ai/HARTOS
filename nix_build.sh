#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════
# HART OS — NixOS ISO Build Script (WSL2)
# ═══════════════════════════════════════════════════════════════
#
# Usage (from Git Bash on Windows):
#   MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu-22.04 -- /bin/bash /mnt/c/Users/sathi/PycharmProjects/HARTOS/nix_build.sh
#
# Prerequisites:
#   - WSL2 with Ubuntu-22.04
#   - Determinate Nix installed (https://install.determinate.systems)
#   - systemd=true in /etc/wsl.conf
#   - User in trusted-users in /etc/nix/nix.custom.conf
#
# What this script does:
#   1. Sources Nix environment
#   2. Strips CRLF from all .nix/.sh files (Windows cross-filesystem issue)
#   3. Sets git safe directory for cross-filesystem access
#   4. Builds the HART OS server ISO via nix flake
#   5. Reports ISO location and size

REPO="/mnt/c/Users/sathi/PycharmProjects/HARTOS"

# ─── Source Nix ───
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh 2>/dev/null || {
    echo "ERROR: Nix not found. Install: curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install"
    exit 1
}

echo "=== Nix store check ==="
nix store info 2>&1 | head -3

# ─── Fix CRLF line endings (Windows → WSL2 cross-filesystem) ───
echo ""
echo "=== Fixing line endings ==="
find "$REPO/nixos" -name "*.nix" -exec sed -i 's/\r$//' {} +
find "$REPO/nixos" -name "*.sh" -exec sed -i 's/\r$//' {} +
echo "Stripped CRLF from $(find "$REPO/nixos" -name '*.nix' | wc -l) .nix files"

# ─── Git safe directory ───
cd "$REPO"
git config --global --add safe.directory "$REPO" 2>/dev/null || true

# ─── Check flake is tracked ───
if ! git ls-files --error-unmatch nixos/flake.nix >/dev/null 2>&1; then
    echo "WARNING: nixos/flake.nix not tracked by git. Adding it..."
    git add nixos/flake.nix nixos/flake.lock 2>/dev/null || true
fi

# ─── Build ISO ───
echo ""
echo "=== Starting ISO build from nixos/ ==="
cd nixos
nix build .#iso-server --show-trace --print-build-logs 2>&1

echo ""
echo "=== Build complete ==="
ls -lh result/iso/*.iso 2>/dev/null || ls -lh result/ 2>/dev/null || echo "No result directory"
echo ""
echo "To boot in QEMU, run: bash $REPO/boot_vm.sh"
