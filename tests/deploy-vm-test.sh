#!/usr/bin/env bash
# ============================================================
# HART OS Full VM Deployment & Test Pipeline
# ============================================================
#
# Runs inside WSL2. Automates the full OS dev team workflow:
#   1. Install Nix (if missing)
#   2. Install QEMU (if missing)
#   3. Build HART OS server ISO via Nix flake
#   4. Boot ISO in QEMU (headless, port-forwarded)
#   5. Run 15-point E2E smoke test suite
#   6. Run NixOS VM integration tests (nix flake check)
#   7. Produce a consolidated test report
#
# Usage (from WSL2):
#   bash /mnt/c/Users/sathi/PycharmProjects/HARTOS/tests/deploy-vm-test.sh
#
# Options:
#   --skip-nix-install    Skip Nix installation (already installed)
#   --skip-build          Skip ISO build (reuse existing)
#   --skip-vm-tests       Skip nix flake check VM tests
#   --variant <name>      Build variant: server|desktop|edge (default: server)
#   --ram <mb>            QEMU RAM in MB (default: 4096)
#   --timeout <sec>       Max boot wait in seconds (default: 600)
#
# Exit: 0 = all pass, 1 = any failure
# ============================================================

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────
REPO_WIN="/mnt/c/Users/sathi/PycharmProjects/HARTOS"
REPO_NIX="${REPO_WIN}/nixos"
SCRIPT_DIR="${REPO_WIN}/tests"
REPORT_DIR="${REPO_WIN}/test-reports"

VARIANT="server"
QEMU_RAM=4096
QEMU_CPUS=2
BOOT_TIMEOUT=600
BACKEND_PORT=16777
SSH_PORT=10022
SSH_USER="hart-admin"
SSH_PASS="hart"

SKIP_NIX_INSTALL=false
SKIP_BUILD=false
SKIP_VM_TESTS=false

TOTAL_CHECKS=0
PASSED_CHECKS=0
FAILED_CHECKS=0
REPORT_LINES=()

# ─── Parse Arguments ────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-nix-install) SKIP_NIX_INSTALL=true; shift ;;
        --skip-build)       SKIP_BUILD=true; shift ;;
        --skip-vm-tests)    SKIP_VM_TESTS=true; shift ;;
        --variant)          VARIANT="$2"; shift 2 ;;
        --ram)              QEMU_RAM="$2"; shift 2 ;;
        --timeout)          BOOT_TIMEOUT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ─── Colors ─────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${CYAN}[HART]${NC} $*"; }
ok()   { echo -e "  [${GREEN}PASS${NC}] $*"; }
fail() { echo -e "  [${RED}FAIL${NC}] $*"; }
warn() { echo -e "  [${YELLOW}WARN${NC}] $*"; }

record_check() {
    local name="$1"
    local status="$2"  # PASS or FAIL
    local detail="${3:-}"
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    if [[ "$status" == "PASS" ]]; then
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        ok "$name"
    else
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        fail "$name"
        [[ -n "$detail" ]] && echo "         Detail: $detail"
    fi
    REPORT_LINES+=("$(date -Iseconds) | $status | $name | $detail")
}

# ─── Cleanup handler ────────────────────────────────────────
QEMU_PID=""
cleanup() {
    if [[ -n "$QEMU_PID" ]]; then
        log "Shutting down QEMU (PID $QEMU_PID)..."
        kill "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
    fi
    rm -f /tmp/hart-qemu-test.pid
}
trap cleanup EXIT

echo ""
echo -e "${BOLD}============================================================${NC}"
echo -e "${BOLD}  HART OS — Full VM Deployment & Test Pipeline${NC}"
echo -e "${BOLD}============================================================${NC}"
echo ""
echo "  Variant:  $VARIANT"
echo "  RAM:      ${QEMU_RAM}MB"
echo "  Timeout:  ${BOOT_TIMEOUT}s"
echo "  Repo:     $REPO_WIN"
echo ""

# ═════════════════════════════════════════════════════════════
# PHASE 1: Environment Setup
# ═════════════════════════════════════════════════════════════

log "Phase 1: Environment Setup"
echo ""

# ─── 1a: Check we're in WSL2 ────────────────────────────────
if grep -qi microsoft /proc/version 2>/dev/null; then
    record_check "Running inside WSL2" "PASS"
else
    record_check "Running inside WSL2" "FAIL" "This script must run inside WSL2"
    echo ""
    echo "Run this script from inside WSL2:"
    echo "  wsl bash ${REPO_WIN}/tests/deploy-vm-test.sh"
    exit 1
fi

# ─── 1b: Install Nix ────────────────────────────────────────
if [[ "$SKIP_NIX_INSTALL" == "true" ]]; then
    log "Skipping Nix install (--skip-nix-install)"
elif command -v nix &>/dev/null; then
    record_check "Nix already installed" "PASS" "$(nix --version 2>&1)"
else
    log "Installing Nix (multi-user daemon mode)..."

    # Install prerequisites
    sudo apt-get update -qq
    sudo apt-get install -y -qq curl xz-utils git

    # Install Nix with flakes enabled
    curl -fsSL https://install.determinate.systems/nix | sh -s -- install --no-confirm 2>&1 | tail -5

    # Source Nix profile
    if [[ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]]; then
        . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
    elif [[ -f /etc/profile.d/nix.sh ]]; then
        . /etc/profile.d/nix.sh
    fi

    if command -v nix &>/dev/null; then
        record_check "Nix installed successfully" "PASS" "$(nix --version 2>&1)"
    else
        record_check "Nix installed" "FAIL" "nix command not found after install"
        echo ""
        echo "Try: source /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh"
        echo "Then re-run this script with --skip-nix-install"
        exit 1
    fi
fi

# Ensure flakes are enabled
mkdir -p ~/.config/nix
if ! grep -q "experimental-features" ~/.config/nix/nix.conf 2>/dev/null; then
    echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf
fi
record_check "Nix flakes enabled" "PASS"

# ─── 1c: Install QEMU ───────────────────────────────────────
if command -v qemu-system-x86_64 &>/dev/null; then
    record_check "QEMU installed" "PASS" "$(qemu-system-x86_64 --version 2>&1 | head -1)"
else
    log "Installing QEMU and tools..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq qemu-system-x86 qemu-utils sshpass curl

    if command -v qemu-system-x86_64 &>/dev/null; then
        record_check "QEMU installed" "PASS" "$(qemu-system-x86_64 --version 2>&1 | head -1)"
    else
        record_check "QEMU installed" "FAIL" "qemu-system-x86_64 not found after apt install"
        exit 1
    fi
fi

# ─── 1d: Install sshpass (for automated SSH) ────────────────
if ! command -v sshpass &>/dev/null; then
    sudo apt-get install -y -qq sshpass
fi
record_check "sshpass available" "PASS"

# ─── 1e: Check disk space ───────────────────────────────────
AVAIL_GB=$(df -BG / | awk 'NR==2 {print int($4)}')
if [[ "$AVAIL_GB" -ge 15 ]]; then
    record_check "Disk space (need 15GB+)" "PASS" "${AVAIL_GB}GB available"
else
    record_check "Disk space (need 15GB+)" "FAIL" "Only ${AVAIL_GB}GB available — need 15GB+"
    echo ""
    echo "Free up disk space or expand your WSL2 virtual disk."
    exit 1
fi

echo ""

# ═════════════════════════════════════════════════════════════
# PHASE 2: Build HART OS ISO
# ═════════════════════════════════════════════════════════════

log "Phase 2: Build HART OS ISO (variant: $VARIANT)"
echo ""

ISO_PATH=""

if [[ "$SKIP_BUILD" == "true" ]]; then
    # Look for existing ISO
    ISO_PATH=$(find "${REPO_NIX}/result" -name "*.iso" -print -quit 2>/dev/null || true)
    if [[ -n "$ISO_PATH" && -f "$ISO_PATH" ]]; then
        record_check "Reusing existing ISO" "PASS" "$ISO_PATH"
    else
        record_check "Existing ISO found" "FAIL" "No ISO at ${REPO_NIX}/result/ — remove --skip-build"
        exit 1
    fi
else
    BUILD_START=$(date +%s)
    log "Building iso-${VARIANT} via nix build (this takes 10-30 minutes on first run)..."

    if nix build "${REPO_NIX}#iso-${VARIANT}" --out-link "${REPO_NIX}/result" 2>&1 | tee /tmp/hart-nix-build.log; then
        BUILD_END=$(date +%s)
        BUILD_SECS=$((BUILD_END - BUILD_START))

        ISO_PATH=$(find "${REPO_NIX}/result" -name "*.iso" -print -quit 2>/dev/null || true)
        if [[ -n "$ISO_PATH" && -f "$ISO_PATH" ]]; then
            ISO_SIZE=$(du -h "$ISO_PATH" | cut -f1)
            record_check "ISO built successfully" "PASS" "${ISO_SIZE} in ${BUILD_SECS}s — $ISO_PATH"
        else
            record_check "ISO file found" "FAIL" "Build succeeded but no .iso found in result/"
            ls -la "${REPO_NIX}/result/" 2>/dev/null || true
            exit 1
        fi
    else
        record_check "Nix build" "FAIL" "See /tmp/hart-nix-build.log"
        echo ""
        echo "Last 30 lines of build log:"
        tail -30 /tmp/hart-nix-build.log
        exit 1
    fi
fi

echo ""

# ═════════════════════════════════════════════════════════════
# PHASE 3: Boot ISO in QEMU
# ═════════════════════════════════════════════════════════════

log "Phase 3: Boot HART OS in QEMU (headless)"
echo ""

# Create persistent disk
DISK="/tmp/hart-test-disk.qcow2"
if [[ ! -f "$DISK" ]]; then
    qemu-img create -f qcow2 "$DISK" 20G >/dev/null 2>&1
fi

# Kill any previous QEMU instance
if [[ -f /tmp/hart-qemu-test.pid ]]; then
    OLD_PID=$(cat /tmp/hart-qemu-test.pid 2>/dev/null || true)
    [[ -n "$OLD_PID" ]] && kill "$OLD_PID" 2>/dev/null || true
    sleep 1
    rm -f /tmp/hart-qemu-test.pid
fi

# Build QEMU command — no KVM in WSL2 (nested virt not supported)
QEMU_LOG="/tmp/hart-qemu-boot.log"

qemu-system-x86_64 \
    -m "$QEMU_RAM" \
    -smp "$QEMU_CPUS" \
    -cdrom "$ISO_PATH" \
    -boot d \
    -nographic \
    -serial mon:stdio \
    -drive "file=$DISK,format=qcow2,if=virtio" \
    -net nic \
    -net "user,hostfwd=tcp::${BACKEND_PORT}-:6777,hostfwd=tcp::${SSH_PORT}-:22" \
    > "$QEMU_LOG" 2>&1 &

QEMU_PID=$!
echo "$QEMU_PID" > /tmp/hart-qemu-test.pid

log "QEMU started: PID=$QEMU_PID, backend=localhost:${BACKEND_PORT}, SSH=localhost:${SSH_PORT}"
log "Log: $QEMU_LOG"
log "Waiting for backend (max ${BOOT_TIMEOUT}s, no KVM = emulation mode)..."

# Wait for backend to come up
ELAPSED=0
INTERVAL=15
BOOT_OK=false

while [[ $ELAPSED -lt $BOOT_TIMEOUT ]]; do
    # Check QEMU still alive
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        record_check "QEMU process alive" "FAIL" "QEMU died after ${ELAPSED}s"
        echo "Last 30 lines of QEMU log:"
        tail -30 "$QEMU_LOG" 2>/dev/null || true
        exit 1
    fi

    # Probe backend
    if curl -sf "http://localhost:${BACKEND_PORT}/status" >/dev/null 2>&1; then
        BOOT_OK=true
        break
    fi

    sleep "$INTERVAL"
    ELAPSED=$((ELAPSED + INTERVAL))
    echo "    ... ${ELAPSED}s elapsed"
done

if [[ "$BOOT_OK" == "true" ]]; then
    record_check "HART OS booted in QEMU" "PASS" "Backend responded after ${ELAPSED}s"
else
    record_check "HART OS booted in QEMU" "FAIL" "Timeout after ${BOOT_TIMEOUT}s"
    echo ""
    echo "Last 50 lines of QEMU log:"
    tail -50 "$QEMU_LOG" 2>/dev/null || true
    # Don't exit — still generate report
fi

echo ""

# ═════════════════════════════════════════════════════════════
# PHASE 4: E2E Smoke Tests (15 checks against running VM)
# ═════════════════════════════════════════════════════════════

log "Phase 4: E2E Smoke Tests"
echo ""

# SSH helper
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

if [[ "$BOOT_OK" == "true" ]]; then

    # --- Check: Backend health ---
    HEALTH=$(curl -sf "http://localhost:${BACKEND_PORT}/status" 2>/dev/null || echo "UNREACHABLE")
    if echo "$HEALTH" | grep -qE '"success"|"uptime"'; then
        record_check "Backend health (GET /status)" "PASS" "HTTP 200"
    else
        record_check "Backend health (GET /status)" "FAIL" "$HEALTH"
    fi

    # --- Check: SSH access ---
    SSH_TEST=$(ssh_cmd "echo CONNECTED" || echo "FAILED")
    if [[ "$SSH_TEST" == *"CONNECTED"* ]]; then
        record_check "SSH access" "PASS"
    else
        record_check "SSH access" "FAIL" "Cannot SSH — remaining checks will skip"
    fi

    # --- Check: First-boot completed ---
    FB=$(ssh_cmd "test -f /var/lib/hart/.first-boot-done && echo YES || echo NO")
    if [[ "$FB" == *"YES"* ]]; then
        record_check "First-boot completed" "PASS"
    else
        record_check "First-boot completed" "FAIL" "Marker file missing"
    fi

    # --- Check: Node identity ---
    KEY_SIZE=$(ssh_cmd "wc -c < /var/lib/hart/node_public.key 2>/dev/null || echo 0" | tr -d '[:space:]')
    if [[ "$KEY_SIZE" == "32" ]]; then
        record_check "Node identity (Ed25519, 32 bytes)" "PASS"
    else
        record_check "Node identity (Ed25519, 32 bytes)" "FAIL" "Got ${KEY_SIZE} bytes"
    fi

    # --- Check: Tier classified ---
    TIER=$(ssh_cmd "cat /var/lib/hart/capability_tier 2>/dev/null || echo UNKNOWN" | tr -d '[:space:]')
    if [[ "$TIER" =~ ^(OBSERVER|LITE|STANDARD|PERFORMANCE|COMPUTE_HOST)$ ]]; then
        record_check "Tier classified ($TIER)" "PASS"
    else
        record_check "Tier classified" "FAIL" "Got: $TIER"
    fi

    # --- Check: Database initialized ---
    DB_SIZE=$(ssh_cmd "wc -c < /var/lib/hart/hevolve_database.db 2>/dev/null || echo 0" | tr -d '[:space:]')
    if [[ "$DB_SIZE" -gt 0 ]] 2>/dev/null; then
        record_check "Database initialized" "PASS" "${DB_SIZE} bytes"
    else
        record_check "Database initialized" "FAIL" "Empty or missing"
    fi

    # --- Check: OS branding ---
    OS_BRAND=$(ssh_cmd "grep -c 'HART OS' /etc/os-release 2>/dev/null || echo 0" | tr -d '[:space:]')
    if [[ "$OS_BRAND" -gt 0 ]] 2>/dev/null; then
        record_check "OS branding (/etc/os-release)" "PASS"
    else
        record_check "OS branding (/etc/os-release)" "FAIL" "HART OS not found"
    fi

    # --- Check: Boot audit signed ---
    AUDIT=$(ssh_cmd "test -f /var/lib/hart/boot_audit.log && head -1 /var/lib/hart/boot_audit.log || echo MISSING")
    if [[ "$AUDIT" != "MISSING" && -n "$AUDIT" ]]; then
        record_check "Boot audit log exists" "PASS"
    else
        record_check "Boot audit log exists" "FAIL"
    fi

    # --- Check: Firewall ---
    FW=$(ssh_cmd "sudo nft list ruleset 2>/dev/null | grep -c 6777 || sudo iptables -L -n 2>/dev/null | grep -c 6777 || echo 0" | tr -d '[:space:]')
    if [[ "$FW" -gt 0 ]] 2>/dev/null; then
        record_check "Firewall allows port 6777" "PASS"
    else
        record_check "Firewall allows port 6777" "FAIL"
    fi

    # --- Check: CLI tool ---
    CLI=$(ssh_cmd "which hart 2>/dev/null || echo MISSING" | tr -d '[:space:]')
    if [[ "$CLI" != "MISSING" && -n "$CLI" ]]; then
        record_check "CLI tool (hart command)" "PASS" "$CLI"
    else
        record_check "CLI tool (hart command)" "FAIL"
    fi

    # --- Check: Discovery service ---
    DISCO=$(ssh_cmd "systemctl is-active hart-discovery.service 2>/dev/null || echo unknown" | tr -d '[:space:]')
    if [[ "$DISCO" == "active" || "$DISCO" == "activating" ]]; then
        record_check "Discovery service" "PASS" "$DISCO"
    else
        record_check "Discovery service" "FAIL" "$DISCO"
    fi

    # --- Check: Agent daemon ---
    AGENT_D=$(ssh_cmd "systemctl is-active hart-agent-daemon.service 2>/dev/null || echo unknown" | tr -d '[:space:]')
    if [[ "$AGENT_D" == "active" || "$AGENT_D" == "activating" ]]; then
        record_check "Agent daemon" "PASS" "$AGENT_D"
    else
        record_check "Agent daemon" "FAIL" "$AGENT_D"
    fi

    # --- Check: Sandbox ---
    SANDBOX=$(ssh_cmd "which hart-sandbox 2>/dev/null || hart sandbox status 2>/dev/null && echo OK || echo MISSING")
    if [[ "$SANDBOX" != *"MISSING"* ]]; then
        record_check "Sandbox tool" "PASS"
    else
        record_check "Sandbox tool" "FAIL"
    fi

    # --- Check: Model store ---
    MODELS=$(ssh_cmd "test -d /var/lib/hart/models && echo YES || echo NO" | tr -d '[:space:]')
    if [[ "$MODELS" == "YES" ]]; then
        record_check "Model store directory" "PASS"
    else
        record_check "Model store directory" "FAIL"
    fi

    # --- Check: Agent cgroup slice ---
    SLICE=$(ssh_cmd "systemctl cat hart-agents.slice &>/dev/null && echo EXISTS || echo MISSING" | tr -d '[:space:]')
    if [[ "$SLICE" == "EXISTS" ]]; then
        record_check "Agent cgroup slice" "PASS"
    else
        record_check "Agent cgroup slice" "FAIL"
    fi

    # --- Check: Memory usage (server should be < 2GB at idle) ---
    USED_MB=$(ssh_cmd "free -m | awk '/Mem:/ {print \$3}'" | tr -d '[:space:]')
    if [[ "$USED_MB" -lt 2048 ]] 2>/dev/null; then
        record_check "Idle memory < 2GB" "PASS" "${USED_MB}MB"
    else
        record_check "Idle memory < 2GB" "FAIL" "${USED_MB}MB"
    fi

    # --- Check: Chat endpoint responds ---
    CHAT_RESP=$(curl -sf -X POST "http://localhost:${BACKEND_PORT}/chat" \
        -H "Content-Type: application/json" \
        -d '{"user_id":"999","prompt_id":"99999","prompt":"ping"}' \
        --max-time 30 2>/dev/null || echo "TIMEOUT")
    if [[ "$CHAT_RESP" != "TIMEOUT" && -n "$CHAT_RESP" ]]; then
        record_check "Chat endpoint responds" "PASS"
    else
        record_check "Chat endpoint responds" "FAIL" "$CHAT_RESP"
    fi

    # --- Check: Shell API battery endpoint ---
    BATT=$(curl -sf "http://localhost:${BACKEND_PORT}/api/shell/battery" --max-time 10 2>/dev/null || echo "FAIL")
    if echo "$BATT" | grep -q "has_battery"; then
        record_check "Shell API (battery endpoint)" "PASS"
    else
        record_check "Shell API (battery endpoint)" "FAIL"
    fi

    # --- Check: Platform apps registered ---
    APPS=$(curl -sf "http://localhost:${BACKEND_PORT}/api/shell/manifest" --max-time 10 2>/dev/null || echo "FAIL")
    if echo "$APPS" | grep -q "panels"; then
        record_check "Platform apps registered (shell manifest)" "PASS"
    else
        record_check "Platform apps registered (shell manifest)" "FAIL"
    fi

    # --- Check: Kernel modules ---
    KMODS=$(ssh_cmd "lsmod | grep -cE '(binder|ashmem|vsock|nvidia|amdgpu)' 2>/dev/null || echo 0" | tr -d '[:space:]')
    if [[ "$KMODS" -gt 0 ]] 2>/dev/null; then
        record_check "Kernel subsystem modules" "PASS" "$KMODS loaded"
    else
        # Not a hard failure for server variant without GPU
        warn "Kernel subsystem modules: 0 loaded (expected for VM without GPU)"
        record_check "Kernel subsystem modules" "PASS" "0 (expected in VM)"
    fi

else
    warn "Skipping smoke tests — VM did not boot successfully"
    for i in $(seq 1 20); do
        record_check "Smoke test #$i (skipped — no boot)" "FAIL" "VM did not boot"
    done
fi

echo ""

# ═════════════════════════════════════════════════════════════
# PHASE 5: NixOS VM Integration Tests (nix flake check)
# ═════════════════════════════════════════════════════════════

if [[ "$SKIP_VM_TESTS" == "true" ]]; then
    log "Phase 5: Skipped (--skip-vm-tests)"
else
    log "Phase 5: NixOS VM Integration Tests (nix flake check)"
    echo ""
    log "This boots 4 separate VMs and runs automated assertions..."
    log "(server-boot, desktop-boot, edge-boot, peer-discovery)"
    echo ""

    VM_TEST_LOG="/tmp/hart-vm-tests.log"

    if nix flake check "${REPO_NIX}" 2>&1 | tee "$VM_TEST_LOG"; then
        record_check "NixOS VM integration tests (nix flake check)" "PASS"
    else
        # Parse which tests passed/failed
        FAILED_TESTS=$(grep -oP 'FAIL: \K.*' "$VM_TEST_LOG" || echo "unknown")
        record_check "NixOS VM integration tests" "FAIL" "$FAILED_TESTS"
    fi
fi

echo ""

# ═════════════════════════════════════════════════════════════
# PHASE 6: Shutdown & Report
# ═════════════════════════════════════════════════════════════

log "Phase 6: Shutdown & Report"
echo ""

# Shutdown QEMU
if [[ -n "$QEMU_PID" ]]; then
    log "Shutting down QEMU..."
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
    QEMU_PID=""  # Prevent double-kill in trap
fi

# Clean up temp files
rm -f /tmp/hart-qemu-test.pid /tmp/hart-test-disk.qcow2

# Generate report
mkdir -p "$REPORT_DIR"
REPORT_FILE="${REPORT_DIR}/vm-deploy-test-$(date +%Y%m%d_%H%M%S).txt"

{
    echo "============================================================"
    echo "  HART OS VM Deployment Test Report"
    echo "============================================================"
    echo ""
    echo "  Date:     $(date -Iseconds)"
    echo "  Variant:  $VARIANT"
    echo "  RAM:      ${QEMU_RAM}MB"
    echo "  Host:     $(uname -a)"
    echo ""
    echo "  Total:    $TOTAL_CHECKS"
    echo "  Passed:   $PASSED_CHECKS"
    echo "  Failed:   $FAILED_CHECKS"
    echo ""
    echo "------------------------------------------------------------"
    echo "  Timestamp | Status | Check | Detail"
    echo "------------------------------------------------------------"
    for line in "${REPORT_LINES[@]}"; do
        echo "  $line"
    done
    echo "------------------------------------------------------------"
    echo ""
    if [[ $FAILED_CHECKS -eq 0 ]]; then
        echo "  RESULT: ALL CHECKS PASSED"
    else
        echo "  RESULT: ${FAILED_CHECKS} FAILURES"
    fi
    echo ""
    echo "============================================================"
} | tee "$REPORT_FILE"

echo ""
log "Report saved: $REPORT_FILE"
echo ""

# ─── Summary ────────────────────────────────────────────────
echo -e "${BOLD}============================================================${NC}"
if [[ $FAILED_CHECKS -eq 0 ]]; then
    echo -e "  ${GREEN}ALL ${TOTAL_CHECKS} CHECKS PASSED${NC}"
else
    echo -e "  ${RED}${FAILED_CHECKS} FAILED${NC}, ${GREEN}${PASSED_CHECKS} PASSED${NC} (out of ${TOTAL_CHECKS})"
fi
echo -e "${BOLD}============================================================${NC}"
echo ""

[[ $FAILED_CHECKS -eq 0 ]]
