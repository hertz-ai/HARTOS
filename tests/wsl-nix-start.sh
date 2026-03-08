#!/usr/bin/env bash
# Start nix daemon + verify, run as root inside WSL
export PATH="/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Start nix daemon in background
/nix/var/nix/profiles/default/bin/nix-daemon &
sleep 2

# Verify
nix --version
echo "---"

# Enable flakes
mkdir -p /root/.config/nix
echo "experimental-features = nix-command flakes" > /root/.config/nix/nix.conf

# Also for the regular user
mkdir -p /home/sathish/.config/nix
echo "experimental-features = nix-command flakes" > /home/sathish/.config/nix/nix.conf
chown -R sathish:sathish /home/sathish/.config

# Test nix works
nix eval --expr '1 + 1'
echo "=== NIX READY ==="
