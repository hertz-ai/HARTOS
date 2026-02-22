#!/usr/bin/env bash
# ============================================================
# HART OS Recovery — Reset to factory state
#
# This script:
#   1. Stops all HART OS services
#   2. Wipes node data (keypairs, database, agent data)
#   3. Regenerates Ed25519 identity
#   4. Resets services to default config
#   5. Re-runs first-boot setup
#
# WARNING: This destroys all node data, trust history, and
# agent state. The node will get a new identity.
#
# Usage:
#   sudo bash hart-recovery.sh [--confirm]
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DATA_DIR="/var/lib/hart"
LOG_DIR="/var/log/hart"

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Must be run as root.${NC}"
    exit 1
fi

if [[ "${1:-}" != "--confirm" ]]; then
    echo -e "${YELLOW}WARNING: This will erase all HART OS node data!${NC}"
    echo ""
    echo "  The following will be deleted:"
    echo "    - Ed25519 node identity (keypair)"
    echo "    - Database (all social data, goals, agents)"
    echo "    - Agent data (recipes, ledgers, baselines)"
    echo "    - Trust history and fraud scores"
    echo ""
    echo "  The node will receive a NEW identity."
    echo "  OS packages and code will be preserved."
    echo ""
    read -p "Type 'RESET' to confirm: " confirmation
    if [[ "$confirmation" != "RESET" ]]; then
        echo "Cancelled."
        exit 0
    fi
fi

echo ""
echo -e "${YELLOW}[Recovery] Starting HART OS factory reset...${NC}"

# Step 1: Stop services
echo "[1/5] Stopping services..."
systemctl stop hart.target 2>/dev/null || true

# Step 2: Wipe node data
echo "[2/5] Wiping node data..."
# Clear immutable flag before removal
chattr -i "$DATA_DIR/node_private.key" 2>/dev/null || true
rm -f "$DATA_DIR/node_private.key"
rm -f "$DATA_DIR/node_public.key"
rm -f "$DATA_DIR/hevolve_database.db"
rm -f "$DATA_DIR/hevolve_database.db-journal"
rm -f "$DATA_DIR/hevolve_database.db-wal"
rm -rf "$DATA_DIR/code_hash_cache.json"
rm -f "$DATA_DIR/.first-boot-done"

# Wipe agent data but preserve directory
find /opt/hart/agent_data -type f -name '*.json' -delete 2>/dev/null || true
find /opt/hart/agent_data -type f -name '*.db' -delete 2>/dev/null || true

# Wipe logs
rm -f "$LOG_DIR"/*.log

echo "  Data wiped."

# Step 3: Reset config to template
echo "[3/5] Resetting configuration..."
if [[ -f /opt/hart/deploy/linux/hart.env.template ]]; then
    cp /opt/hart/deploy/linux/hart.env.template /etc/hart/hart.env
    sed -i "s|^HEVOLVE_DB_PATH=.*|HEVOLVE_DB_PATH=$DATA_DIR/hevolve_database.db|" /etc/hart/hart.env
    chmod 600 /etc/hart/hart.env
    chown hart:hart /etc/hart/hart.env
fi

# Step 4: Re-enable first-boot
echo "[4/5] Re-enabling first-boot setup..."
systemctl enable hart-first-boot.service 2>/dev/null || true

# Step 5: Trigger first-boot
echo "[5/5] Running first-boot setup..."
bash /opt/hart/deploy/distro/first-boot/hart-first-boot.sh

echo ""
echo -e "${GREEN}[Recovery] Factory reset complete.${NC}"
echo "  New node identity generated."
echo "  Run 'hart status' to verify."
