#!/usr/bin/env bash
# WSL2 setup for HART OS VM testing
# All output goes to /mnt/c/ accessible log file
set -uo pipefail

LOG="/mnt/c/Users/sathi/PycharmProjects/HARTOS/test-reports/wsl-setup.log"
mkdir -p "$(dirname "$LOG")"

exec > "$LOG" 2>&1

echo "=== HART OS WSL2 Setup — $(date) ==="
echo ""

echo "[1/4] Switching to fastest mirror..."
# Use Indian mirror for faster downloads
sudo sed -i 's|http://archive.ubuntu.com/ubuntu|http://in.archive.ubuntu.com/ubuntu|g' /etc/apt/sources.list 2>/dev/null || true

echo "[1/4] apt-get update..."
sudo apt-get update -y
echo "[1/4] DONE"

echo ""
echo "[2/4] Installing packages (QEMU minimal, sshpass, curl)..."
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl xz-utils git sshpass \
    qemu-system-x86 qemu-utils
echo "[2/4] DONE"

echo ""
echo "[3/4] Verifying..."
echo "  qemu: $(which qemu-system-x86_64 2>/dev/null || echo MISSING)"
echo "  sshpass: $(which sshpass 2>/dev/null || echo MISSING)"
echo "  curl: $(which curl 2>/dev/null || echo MISSING)"
echo "  git: $(which git 2>/dev/null || echo MISSING)"
echo "[3/4] DONE"

echo ""
echo "[4/4] Installing Nix..."
if command -v nix &>/dev/null; then
    echo "  Nix already installed: $(nix --version)"
else
    curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install --no-confirm || echo "WARN: Nix install issue"
    [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ] && . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
    echo "  Nix: $(nix --version 2>/dev/null || echo 'needs new shell')"
fi

mkdir -p ~/.config/nix
grep -q "experimental-features" ~/.config/nix/nix.conf 2>/dev/null || \
    echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf
echo "[4/4] DONE"

echo ""
echo "=== SETUP COMPLETE — $(date) ==="
