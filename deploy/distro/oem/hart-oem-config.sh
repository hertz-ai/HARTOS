#!/usr/bin/env bash
# ============================================================
# HART OS OEM Configuration
#
# For pre-installing HART OS on hardware before shipping.
# Installs everything but defers identity generation and
# configuration to the first real user.
#
# Usage:
#   sudo bash hart-oem-config.sh --prepare    # OEM installs
#   # (ship hardware)
#   # On first boot, hart-oem.service runs the setup wizard
# ============================================================

set -euo pipefail

ACTION="${1:---prepare}"

case "$ACTION" in
    --prepare)
        echo "[HART OS OEM] Preparing device for shipping..."

        # Remove any existing identity
        rm -f /var/lib/hart/node_private.key
        rm -f /var/lib/hart/node_public.key

        # Wipe SSH host keys (regenerated on first real boot)
        rm -f /etc/ssh/ssh_host_*_key*
        rm -f /var/lib/hart/hevolve_database.db
        rm -f /var/lib/hart/.first-boot-done

        # Reset config to template
        cp /opt/hart/deploy/linux/hart.env.template /etc/hart/hart.env
        sed -i "s|^HEVOLVE_DB_PATH=.*|HEVOLVE_DB_PATH=/var/lib/hart/hevolve_database.db|" /etc/hart/hart.env
        chmod 600 /etc/hart/hart.env
        chown hart:hart /etc/hart/hart.env

        # Stop services
        systemctl stop hart.target 2>/dev/null || true

        # Enable OEM first-boot
        systemctl enable hart-oem.service
        systemctl enable hart-first-boot.service

        # Clean logs
        rm -f /var/log/hart/*.log
        journalctl --rotate --vacuum-time=1s 2>/dev/null || true

        # Clean machine-id (will be regenerated on next boot)
        echo "uninitialized" > /etc/machine-id

        echo "[HART OS OEM] Device prepared. Safe to ship."
        echo "  First user will see the setup wizard on boot."
        ;;

    --user-setup)
        # Called by hart-oem.service on first real-user boot
        echo ""
        echo "============================================================"
        echo "  Welcome to HART OS!"
        echo "  Setting up your node..."
        echo "============================================================"
        echo ""

        # Regenerate machine-id
        systemd-machine-id-setup 2>/dev/null || true

        # Regenerate SSH host keys (wiped during OEM prepare)
        ssh-keygen -A 2>/dev/null || true

        # Run the standard first-boot
        bash /opt/hart/deploy/distro/first-boot/hart-first-boot.sh

        # Disable OEM service (one-time)
        systemctl disable hart-oem.service 2>/dev/null || true

        echo "[HART OS OEM] User setup complete."
        ;;

    *)
        echo "Usage: hart-oem-config.sh [--prepare|--user-setup]"
        exit 1
        ;;
esac
