#!/usr/bin/env bash
# ============================================================
# HART OS E2E: Boot ISO in QEMU for automated testing
#
# Usage:
#   bash tests/boot-qemu-for-test.sh <iso-path> [--no-kvm]
#
# Launches QEMU in the background with:
#   - Port 16777 (host) → 6777 (guest: hart-backend)
#   - Port 10022 (host) → 22 (guest: SSH)
#   - Headless mode (no display, serial to stdout)
#
# Waits up to 5 minutes for the backend to respond.
# On success, exits 0 (QEMU keeps running for smoke tests).
# On failure, exits 1.
# ============================================================

set -euo pipefail

ISO="${1:?Usage: boot-qemu-for-test.sh <iso-path> [--no-kvm]}"
NO_KVM="${2:-}"

QEMU_RAM="${QEMU_RAM:-4096}"
QEMU_CPUS="${QEMU_CPUS:-2}"
QEMU_PID_FILE="/tmp/hart-qemu-test.pid"
QEMU_LOG="/tmp/hart-qemu-boot.log"
BACKEND_PORT="${BACKEND_PORT:-16777}"
SSH_PORT="${SSH_PORT:-10022}"
MAX_WAIT="${MAX_WAIT:-300}"  # 5 minutes

echo "============================================================"
echo "  HART OS E2E: Booting ISO in QEMU"
echo "============================================================"
echo ""
echo "  ISO:       $ISO"
echo "  RAM:       ${QEMU_RAM}MB"
echo "  CPUs:      ${QEMU_CPUS}"
echo "  Backend:   http://localhost:${BACKEND_PORT}"
echo "  SSH:       ssh -p ${SSH_PORT} hart-admin@localhost"
echo ""

if [[ ! -f "$ISO" ]]; then
    echo "ERROR: ISO file not found: $ISO"
    exit 1
fi

# Clean up any previous QEMU instance
if [[ -f "$QEMU_PID_FILE" ]]; then
    OLD_PID=$(cat "$QEMU_PID_FILE" 2>/dev/null || true)
    if [[ -n "$OLD_PID" ]]; then
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$QEMU_PID_FILE"
fi

# Create disk image for persistent state
DISK="/tmp/hart-test-disk.qcow2"
if [[ ! -f "$DISK" ]]; then
    qemu-img create -f qcow2 "$DISK" 20G
fi

# Build QEMU command
QEMU_CMD=(
    qemu-system-x86_64
    -m "$QEMU_RAM"
    -smp "$QEMU_CPUS"
    -cdrom "$ISO"
    -boot d
    -nographic
    -serial mon:stdio
    -drive "file=$DISK,format=qcow2,if=virtio"
    -net nic
    -net "user,hostfwd=tcp::${BACKEND_PORT}-:6777,hostfwd=tcp::${SSH_PORT}-:22"
)

# Add KVM if available and not disabled
if [[ "$NO_KVM" != "--no-kvm" ]] && [[ -e /dev/kvm ]]; then
    QEMU_CMD+=(-enable-kvm)
    echo "  KVM:       enabled"
else
    echo "  KVM:       disabled (emulation mode — slower)"
fi

echo ""
echo "  Launching QEMU..."

# Start QEMU in background, redirect output to log
"${QEMU_CMD[@]}" > "$QEMU_LOG" 2>&1 &
QEMU_PID=$!
echo "$QEMU_PID" > "$QEMU_PID_FILE"

echo "  QEMU PID:  $QEMU_PID"
echo "  Log:       $QEMU_LOG"
echo ""

# Wait for backend to come up
echo "  Waiting for backend (max ${MAX_WAIT}s)..."
ELAPSED=0
INTERVAL=10

while [[ $ELAPSED -lt $MAX_WAIT ]]; do
    # Check if QEMU is still running
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        echo ""
        echo "ERROR: QEMU process died. Last 20 lines of log:"
        tail -20 "$QEMU_LOG" 2>/dev/null || true
        exit 1
    fi

    # Probe backend
    if curl -sf "http://localhost:${BACKEND_PORT}/status" >/dev/null 2>&1; then
        echo ""
        echo "  Backend is up after ${ELAPSED}s!"
        echo ""
        echo "============================================================"
        echo "  QEMU is running. Ready for smoke tests."
        echo "============================================================"
        exit 0
    fi

    sleep "$INTERVAL"
    ELAPSED=$((ELAPSED + INTERVAL))
    echo "    ... ${ELAPSED}s elapsed"
done

# Timeout
echo ""
echo "TIMEOUT: Backend did not come up in ${MAX_WAIT} seconds."
echo ""
echo "Last 40 lines of QEMU log:"
tail -40 "$QEMU_LOG" 2>/dev/null || true
echo ""

# Cleanup
kill "$QEMU_PID" 2>/dev/null || true
rm -f "$QEMU_PID_FILE"
exit 1
