#!/usr/bin/env bash
# ============================================================
# HyveOS OEM Configuration
#
# For pre-installing HyveOS on hardware before shipping.
# Installs everything but defers identity generation and
# configuration to the first real user.
#
# Usage:
#   sudo bash hyve-oem-config.sh --prepare    # OEM installs
#   # (ship hardware)
#   # On first boot, hyve-oem.service runs the setup wizard
# ============================================================

set -euo pipefail

ACTION="${1:---prepare}"

case "$ACTION" in
    --prepare)
        echo "[HyveOS OEM] Preparing device for shipping..."

        # Remove any existing identity
        rm -f /var/lib/hyve/node_private.key
        rm -f /var/lib/hyve/node_public.key

        # Wipe SSH host keys (regenerated on first real boot)
        rm -f /etc/ssh/ssh_host_*_key*
        rm -f /var/lib/hyve/hevolve_database.db
        rm -f /var/lib/hyve/.first-boot-done

        # Reset config to template
        cp /opt/hyve/deploy/linux/hyve.env.template /etc/hyve/hyve.env
        sed -i "s|^HEVOLVE_DB_PATH=.*|HEVOLVE_DB_PATH=/var/lib/hyve/hevolve_database.db|" /etc/hyve/hyve.env
        chmod 600 /etc/hyve/hyve.env
        chown hyve:hyve /etc/hyve/hyve.env

        # Stop services
        systemctl stop hyve.target 2>/dev/null || true

        # Enable OEM first-boot
        systemctl enable hyve-oem.service
        systemctl enable hyve-first-boot.service

        # Clean logs
        rm -f /var/log/hyve/*.log
        journalctl --rotate --vacuum-time=1s 2>/dev/null || true

        # Clean machine-id (will be regenerated on next boot)
        echo "uninitialized" > /etc/machine-id

        echo "[HyveOS OEM] Device prepared. Safe to ship."
        echo "  First user will see the setup wizard on boot."
        ;;

    --user-setup)
        # Called by hyve-oem.service on first real-user boot
        echo ""
        echo "============================================================"
        echo "  Welcome to HyveOS!"
        echo "  Setting up your node..."
        echo "============================================================"
        echo ""

        # Regenerate machine-id
        systemd-machine-id-setup 2>/dev/null || true

        # Regenerate SSH host keys (wiped during OEM prepare)
        ssh-keygen -A 2>/dev/null || true

        # Run the standard first-boot
        bash /opt/hyve/deploy/distro/first-boot/hyve-first-boot.sh

        # Disable OEM service (one-time)
        systemctl disable hyve-oem.service 2>/dev/null || true

        echo "[HyveOS OEM] User setup complete."
        ;;

    *)
        echo "Usage: hyve-oem-config.sh [--prepare|--user-setup]"
        exit 1
        ;;
esac
