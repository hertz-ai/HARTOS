#!/usr/bin/env bash
# ============================================================
# HART OS E2E Smoke Test Suite
#
# Runs 10 automated checks against a running HART OS instance
# (booted in QEMU via boot-qemu-for-test.sh).
#
# Checks use HTTP (port 16777) and SSH (port 10022).
#
# Usage:
#   bash tests/e2e-os-smoke.sh
#
# Exit: 0 = all pass, 1 = any failure
# ============================================================

set -euo pipefail

BACKEND_PORT="${BACKEND_PORT:-16777}"
SSH_PORT="${SSH_PORT:-10022}"
SSH_USER="${SSH_USER:-hart-admin}"
SSH_PASS="${SSH_PASS:-hart}"

PASS=0
FAIL=0
TOTAL=15

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  HART OS E2E Smoke Tests${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

# SSH helper (uses sshpass for password auth)
ssh_cmd() {
    sshpass -p "$SSH_PASS" ssh \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 \
        -o LogLevel=ERROR \
        -p "$SSH_PORT" \
        "${SSH_USER}@localhost" \
        "$@" 2>/dev/null
}

check() {
    local num="$1"
    local name="$2"
    local result="$3"
    local expected="$4"

    if [[ "$result" == "$expected" || "$result" == *"$expected"* ]]; then
        echo -e "  [${GREEN}PASS${NC}] #${num} ${name}"
        PASS=$((PASS + 1))
    else
        echo -e "  [${RED}FAIL${NC}] #${num} ${name}"
        echo -e "         Expected: ${expected}"
        echo -e "         Got:      ${result}"
        FAIL=$((FAIL + 1))
    fi
}

# ─── Check 1: Backend health ─────────────────────────────────
HEALTH=$(curl -sf "http://localhost:${BACKEND_PORT}/status" 2>/dev/null || echo "UNREACHABLE")
if echo "$HEALTH" | grep -q '"success"'; then
    check 1 "Backend health (GET /status)" "HTTP 200 + success" "HTTP 200 + success"
else
    check 1 "Backend health (GET /status)" "$HEALTH" "HTTP 200 + success"
fi

# ─── Check 2: First-boot completed ───────────────────────────
FB_MARKER=$(ssh_cmd "test -f /var/lib/hart/.first-boot-done && echo EXISTS || echo MISSING")
check 2 "First-boot completed (.first-boot-done)" "$FB_MARKER" "EXISTS"

# ─── Check 3: Node identity generated ────────────────────────
NODE_KEY=$(ssh_cmd "test -f /var/lib/hart/node_public.key && wc -c < /var/lib/hart/node_public.key || echo 0")
NODE_KEY=$(echo "$NODE_KEY" | tr -d '[:space:]')
if [[ "$NODE_KEY" == "32" ]]; then
    check 3 "Node identity (Ed25519 public key = 32 bytes)" "32" "32"
else
    check 3 "Node identity (Ed25519 public key = 32 bytes)" "$NODE_KEY bytes" "32"
fi

# ─── Check 4: Tier classified ────────────────────────────────
TIER=$(ssh_cmd "cat /var/lib/hart/capability_tier 2>/dev/null || echo UNKNOWN")
TIER=$(echo "$TIER" | tr -d '[:space:]')
if [[ "$TIER" =~ ^(OBSERVER|LITE|STANDARD|PERFORMANCE|COMPUTE_HOST)$ ]]; then
    check 4 "Tier classified" "$TIER" "$TIER"
else
    check 4 "Tier classified" "$TIER" "OBSERVER|LITE|STANDARD|PERFORMANCE|COMPUTE_HOST"
fi

# ─── Check 5: Database initialized ───────────────────────────
DB_SIZE=$(ssh_cmd "test -f /var/lib/hart/hevolve_database.db && wc -c < /var/lib/hart/hevolve_database.db || echo 0")
DB_SIZE=$(echo "$DB_SIZE" | tr -d '[:space:]')
if [[ "$DB_SIZE" -gt 0 ]] 2>/dev/null; then
    check 5 "Database initialized (non-empty)" "${DB_SIZE} bytes" "non-empty"
else
    check 5 "Database initialized (non-empty)" "empty or missing" "non-empty"
fi

# ─── Check 6: OS branding ────────────────────────────────────
OS_RELEASE=$(ssh_cmd "grep -c 'HART OS' /etc/os-release 2>/dev/null || echo 0")
OS_RELEASE=$(echo "$OS_RELEASE" | tr -d '[:space:]')
if [[ "$OS_RELEASE" -gt 0 ]] 2>/dev/null; then
    check 6 "OS branding (/etc/os-release contains HART OS)" "found" "found"
else
    check 6 "OS branding (/etc/os-release contains HART OS)" "not found" "found"
fi

# ─── Check 7: Boot audit signed ──────────────────────────────
AUDIT=$(ssh_cmd "test -f /var/lib/hart/boot_audit.log && head -1 /var/lib/hart/boot_audit.log || echo MISSING")
if [[ "$AUDIT" != "MISSING" ]] && [[ "$AUDIT" != *"UNSIGNED"* ]] && [[ -n "$AUDIT" ]]; then
    check 7 "Boot audit (signed entry exists)" "signed" "signed"
else
    check 7 "Boot audit (signed entry exists)" "$AUDIT" "signed"
fi

# ─── Check 8: Firewall configured ────────────────────────────
# NixOS uses nftables by default; check if port 6777 is allowed
FW_STATUS=$(ssh_cmd "sudo nft list ruleset 2>/dev/null | grep -c 6777 || sudo ufw status 2>/dev/null | grep -c 6777 || echo 0")
FW_STATUS=$(echo "$FW_STATUS" | tr -d '[:space:]')
if [[ "$FW_STATUS" -gt 0 ]] 2>/dev/null; then
    check 8 "Firewall allows port 6777" "configured" "configured"
else
    # Fallback: check if iptables has the port
    FW_IPTA=$(ssh_cmd "sudo iptables -L -n 2>/dev/null | grep -c 6777 || echo 0")
    FW_IPTA=$(echo "$FW_IPTA" | tr -d '[:space:]')
    if [[ "$FW_IPTA" -gt 0 ]] 2>/dev/null; then
        check 8 "Firewall allows port 6777" "configured" "configured"
    else
        check 8 "Firewall allows port 6777" "not found" "configured"
    fi
fi

# ─── Check 9: CLI tool available ─────────────────────────────
CLI_PATH=$(ssh_cmd "which hart 2>/dev/null || echo MISSING")
CLI_PATH=$(echo "$CLI_PATH" | tr -d '[:space:]')
if [[ "$CLI_PATH" != "MISSING" ]] && [[ -n "$CLI_PATH" ]]; then
    check 9 "CLI tool available (hart command)" "$CLI_PATH" "/nix/"
else
    check 9 "CLI tool available (hart command)" "MISSING" "/nix/"
fi

# ─── Check 10: Discovery service ─────────────────────────────
DISCO=$(ssh_cmd "systemctl is-active hart-discovery.service 2>/dev/null || echo unknown")
DISCO=$(echo "$DISCO" | tr -d '[:space:]')
if [[ "$DISCO" == "active" || "$DISCO" == "activating" ]]; then
    check 10 "Discovery service running" "$DISCO" "$DISCO"
else
    check 10 "Discovery service running" "$DISCO" "active"
fi

# ─── Check 11: Sandbox tool available ──────────────────────────
SANDBOX_PATH=$(ssh_cmd "which hart-sandbox 2>/dev/null || echo MISSING")
SANDBOX_PATH=$(echo "$SANDBOX_PATH" | tr -d '[:space:]')
if [[ "$SANDBOX_PATH" != "MISSING" ]] && [[ -n "$SANDBOX_PATH" ]]; then
    check 11 "Sandbox tool available (hart-sandbox)" "$SANDBOX_PATH" "/nix/"
else
    # Fallback: check via hart wrapper
    HART_SANDBOX=$(ssh_cmd "hart sandbox status 2>/dev/null && echo OK || echo MISSING")
    if [[ "$HART_SANDBOX" == *"OK"* ]]; then
        check 11 "Sandbox tool available (via hart sandbox)" "OK" "OK"
    else
        check 11 "Sandbox tool available (hart-sandbox)" "MISSING" "/nix/"
    fi
fi

# ─── Check 12: Agent cgroup slice ─────────────────────────────
SLICE=$(ssh_cmd "systemctl show hart-agents.slice --property=ActiveState 2>/dev/null | head -1 || echo unknown")
SLICE=$(echo "$SLICE" | tr -d '[:space:]')
if [[ "$SLICE" == *"active"* ]] || [[ "$SLICE" == *"ActiveState=active"* ]]; then
    check 12 "Agent cgroup slice (hart-agents.slice)" "active" "active"
else
    # Slice may exist but be inactive (no agents running yet)
    SLICE_EXISTS=$(ssh_cmd "systemctl cat hart-agents.slice &>/dev/null && echo EXISTS || echo MISSING")
    SLICE_EXISTS=$(echo "$SLICE_EXISTS" | tr -d '[:space:]')
    if [[ "$SLICE_EXISTS" == "EXISTS" ]]; then
        check 12 "Agent cgroup slice (hart-agents.slice)" "configured" "configured"
    else
        check 12 "Agent cgroup slice (hart-agents.slice)" "$SLICE" "active"
    fi
fi

# ─── Check 13: Model store directory ──────────────────────────
MODEL_DIR=$(ssh_cmd "test -d /var/lib/hart/models && echo EXISTS || echo MISSING")
MODEL_DIR=$(echo "$MODEL_DIR" | tr -d '[:space:]')
if [[ "$MODEL_DIR" == "EXISTS" ]]; then
    SUBDIRS=$(ssh_cmd "ls -d /var/lib/hart/models/*/ 2>/dev/null | wc -l")
    SUBDIRS=$(echo "$SUBDIRS" | tr -d '[:space:]')
    check 13 "Model store directory (/var/lib/hart/models)" "$SUBDIRS subdirs" "subdirs"
else
    check 13 "Model store directory (/var/lib/hart/models)" "MISSING" "EXISTS"
fi

# ─── Check 14: Kernel modules (subsystem support) ─────────────
KMOD_COUNT=$(ssh_cmd "lsmod 2>/dev/null | grep -cE '(binder|ashmem|vsock|nvidia|amdgpu)' || echo 0")
KMOD_COUNT=$(echo "$KMOD_COUNT" | tr -d '[:space:]')
if [[ "$KMOD_COUNT" -gt 0 ]] 2>/dev/null; then
    check 14 "Kernel subsystem modules loaded" "$KMOD_COUNT module(s)" "module"
else
    # On minimal/edge, this is expected to be 0
    VARIANT=$(ssh_cmd "cat /var/lib/hart/variant 2>/dev/null || echo unknown")
    VARIANT=$(echo "$VARIANT" | tr -d '[:space:]')
    if [[ "$VARIANT" == "edge" ]]; then
        check 14 "Kernel subsystem modules loaded" "0 (edge — expected)" "expected"
    else
        check 14 "Kernel subsystem modules loaded" "0 modules" "module"
    fi
fi

# ─── Check 15: Conky config deployed (desktop/phone only) ─────
CONKY_CFG=$(ssh_cmd "find /nix/store -name 'hart.conkyrc' -print -quit 2>/dev/null || echo MISSING")
CONKY_CFG=$(echo "$CONKY_CFG" | tr -d '[:space:]')
if [[ "$CONKY_CFG" != "MISSING" ]] && [[ -n "$CONKY_CFG" ]]; then
    check 15 "Conky config deployed" "found" "found"
else
    # On server/edge, Conky is not expected
    VARIANT=$(ssh_cmd "cat /var/lib/hart/variant 2>/dev/null || echo unknown")
    VARIANT=$(echo "$VARIANT" | tr -d '[:space:]')
    if [[ "$VARIANT" == "server" || "$VARIANT" == "edge" ]]; then
        check 15 "Conky config (not expected on $VARIANT)" "skipped" "skipped"
    else
        check 15 "Conky config deployed" "MISSING" "found"
    fi
fi

# ─── Summary ─────────────────────────────────────────────────
echo ""
echo "============================================================"
if [[ $FAIL -eq 0 ]]; then
    echo -e "  ${GREEN}ALL ${TOTAL} CHECKS PASSED${NC}"
else
    echo -e "  ${RED}${FAIL} FAILED${NC}, ${GREEN}${PASS} PASSED${NC} (out of ${TOTAL})"
fi
echo "============================================================"
echo ""

# Exit with failure if any checks failed
[[ $FAIL -eq 0 ]]
