#!/usr/bin/env bash
# Build HART OS ISO in WSL2 via Nix flake
# Run as root with nix daemon running
set -uo pipefail

export PATH="/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO="/mnt/c/Users/sathi/PycharmProjects/HARTOS/nixos"
LOG="/mnt/c/Users/sathi/PycharmProjects/HARTOS/test-reports/nix-build.log"
VARIANT="${1:-server}"

mkdir -p "$(dirname "$LOG")"

echo "=== HART OS ISO Build — $(date) ===" | tee "$LOG"
echo "Variant: $VARIANT" | tee -a "$LOG"
echo "Repo: $REPO" | tee -a "$LOG"
echo "" | tee -a "$LOG"

# Ensure nix daemon is running
if ! pgrep -x nix-daemon > /dev/null 2>&1; then
    echo "Starting nix daemon..." | tee -a "$LOG"
    /nix/var/nix/profiles/default/bin/nix-daemon &
    sleep 2
fi

echo "Building iso-${VARIANT}..." | tee -a "$LOG"
echo "(This may take 15-45 minutes on first run)" | tee -a "$LOG"

if nix build "${REPO}#iso-${VARIANT}" --out-link "${REPO}/result" 2>&1 | tee -a "$LOG"; then
    echo "" | tee -a "$LOG"
    echo "=== BUILD SUCCESS ===" | tee -a "$LOG"
    ISO=$(find "${REPO}/result" -name "*.iso" -print -quit 2>/dev/null)
    if [ -n "$ISO" ]; then
        ls -lh "$ISO" | tee -a "$LOG"
    else
        echo "No ISO found in result/" | tee -a "$LOG"
        ls -laR "${REPO}/result/" 2>/dev/null | tee -a "$LOG"
    fi
else
    echo "" | tee -a "$LOG"
    echo "=== BUILD FAILED ===" | tee -a "$LOG"
    exit 1
fi
