#!/usr/bin/env bash
# ============================================================
# HyveOS Tamper-Evident Boot Audit Log
#
# Creates a signed, append-only audit entry after first boot.
# Each entry records node identity, tier, services, code hash,
# guardrail hash, and an Ed25519 signature proving authenticity.
#
# Usage: hyve-boot-audit.sh <NODE_ID> <TIER>
# Called by hyve-first-boot.sh after all services are up.
# ============================================================

set -euo pipefail

NODE_ID="${1:-unknown}"
TIER="${2:-unknown}"

DATA_DIR="/var/lib/hyve"
INSTALL_DIR="/opt/hyve"
AUDIT_LOG="$DATA_DIR/boot_audit.log"
PRIVATE_KEY="$DATA_DIR/node_private.key"
PYTHON="$INSTALL_DIR/venv/bin/python"

LOG="/var/log/hyve/boot-audit.log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

echo "[BootAudit] Starting tamper-evident boot audit..."

# ─── Gather timestamp ───
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# ─── Gather enabled services ───
SERVICES=""
for svc in hyve-backend hyve-discovery hyve-agent-daemon hyve-vision hyve-llm; do
    if systemctl is-enabled "${svc}.service" 2>/dev/null | grep -q "enabled"; then
        SERVICES="${SERVICES:+${SERVICES},}${svc}"
    fi
done
SERVICES="${SERVICES:-none}"

# ─── Compute code hash and guardrail hash via Python ───
HASHES=$("$PYTHON" -c "
import sys, os
sys.path.insert(0, '$INSTALL_DIR')
os.chdir('$INSTALL_DIR')

code_hash = 'unknown'
guardrail_hash = 'unknown'

try:
    from security.node_integrity import compute_code_hash
    code_hash = compute_code_hash('$INSTALL_DIR')
except Exception as e:
    print(f'# code_hash error: {e}', file=sys.stderr)

try:
    from security.hive_guardrails import compute_guardrail_hash
    guardrail_hash = compute_guardrail_hash()
except Exception as e:
    print(f'# guardrail_hash error: {e}', file=sys.stderr)

print(f'{code_hash}|{guardrail_hash}')
" 2>>"$LOG")

CODE_HASH=$(echo "$HASHES" | cut -d'|' -f1)
GUARDRAIL_HASH=$(echo "$HASHES" | cut -d'|' -f2)

# ─── Build audit entry (everything except signature) ───
ENTRY="${TIMESTAMP} | ${NODE_ID} | ${TIER} | ${SERVICES} | ${CODE_HASH} | ${GUARDRAIL_HASH}"

echo "[BootAudit] Entry: $ENTRY"

# ─── Sign the entry with the node's Ed25519 private key ───
SIGNATURE=$("$PYTHON" -c "
import sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

entry = '''$ENTRY'''

# HyveOS first-boot keys are raw 32-byte files
with open('$PRIVATE_KEY', 'rb') as f:
    key_bytes = f.read()

if len(key_bytes) == 32:
    private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
else:
    private_key = serialization.load_pem_private_key(key_bytes, password=None)

signature = private_key.sign(entry.encode('utf-8'))
print(signature.hex())
" 2>>"$LOG")

if [[ -z "$SIGNATURE" ]]; then
    echo "[BootAudit] ERROR: Failed to sign audit entry."
    SIGNATURE="UNSIGNED"
fi

FULL_ENTRY="${ENTRY} | ${SIGNATURE}"

# ─── Write to audit log ───
mkdir -p "$(dirname "$AUDIT_LOG")"

# Remove append-only flag if log already exists (for writing)
if [[ -f "$AUDIT_LOG" ]]; then
    chattr -a "$AUDIT_LOG" 2>/dev/null || true
fi

echo "$FULL_ENTRY" >> "$AUDIT_LOG"
chown hyve:hyve "$AUDIT_LOG"
chmod 644 "$AUDIT_LOG"

# Set append-only: even root cannot modify existing entries
chattr +a "$AUDIT_LOG" 2>/dev/null || true

echo "[BootAudit] Audit entry written and log set to append-only."
echo "[BootAudit] Done."
