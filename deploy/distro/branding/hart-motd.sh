#!/bin/bash
# HART OS Message of the Day (MOTD)
# Install to /etc/update-motd.d/99-hart

# Colors
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo -e "${CYAN}  HART OS 1.0 — Crowdsourced Agentic Intelligence${NC}"
echo ""

# Node ID
if [[ -f /var/lib/hart/node_public.key ]]; then
    if command -v xxd &>/dev/null; then
        NODE_ID=$(xxd -p /var/lib/hart/node_public.key | tr -d '\n' | head -c 16)
    else
        NODE_ID=$(python3 -c "print(open('/var/lib/hart/node_public.key','rb').read().hex()[:16])" 2>/dev/null || echo "unknown")
    fi
    echo -e "  Node ID:    ${GREEN}${NODE_ID}...${NC}"
fi

# Capability tier
if command -v hart &>/dev/null; then
    TIER=$(hart health 2>/dev/null | grep -i tier | awk '{print $NF}' || echo "unknown")
    echo -e "  Tier:       ${TIER}"
fi

# Service status
BACKEND=$(systemctl is-active hart-backend.service 2>/dev/null || echo "unknown")
if [[ "$BACKEND" == "active" ]]; then
    echo -e "  Backend:    ${GREEN}running${NC}"
else
    echo -e "  Backend:    ${YELLOW}${BACKEND}${NC}"
fi

# Peer count
PEERS=$(curl -s http://localhost:6777/api/social/peers 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('peers',[])))" 2>/dev/null || echo "?")
echo -e "  Peers:      ${PEERS}"

# Uptime
echo -e "  Uptime:     $(uptime -p 2>/dev/null || echo 'unknown')"

# Dashboard
PORT=$(grep HART_BACKEND_PORT /etc/hart/hart.env 2>/dev/null | cut -d= -f2 || echo 6777)
echo ""
echo -e "  Dashboard:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT:-6777}"
echo -e "  CLI:        ${GREEN}hart status${NC}"
echo ""
