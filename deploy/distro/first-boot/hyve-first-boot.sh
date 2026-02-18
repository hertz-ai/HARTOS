#!/usr/bin/env bash
# ============================================================
# HyveOS First Boot Setup
# Runs once after ISO install. Generates node identity,
# classifies hardware, configures services per tier.
#
# Triggered by hyve-first-boot.service (systemd oneshot).
# Disables itself after completion.
# ============================================================

set -euo pipefail

MARKER="/var/lib/hyve/.first-boot-done"
DATA_DIR="/var/lib/hyve"
CONFIG_DIR="/etc/hyve"
INSTALL_DIR="/opt/hyve"
LOG="/var/log/hyve/first-boot.log"

exec > >(tee -a "$LOG") 2>&1

# Skip if already completed
if [[ -f "$MARKER" ]]; then
    echo "[HyveOS] First boot already completed. Skipping."
    exit 0
fi

echo "============================================================"
echo "  HyveOS First Boot Setup"
echo "============================================================"
echo ""

# ─── Step 1: Generate Ed25519 node keypair ───
echo "[1/5] Generating node identity..."

if [[ ! -f "$DATA_DIR/node_private.key" ]]; then
    "$INSTALL_DIR/venv/bin/python" -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import os

private_key = Ed25519PrivateKey.generate()
private_bytes = private_key.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption()
)
public_bytes = private_key.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw
)

with open('$DATA_DIR/node_private.key', 'wb') as f:
    f.write(private_bytes)
with open('$DATA_DIR/node_public.key', 'wb') as f:
    f.write(public_bytes)

os.chmod('$DATA_DIR/node_private.key', 0o600)
print(f'Node ID: {public_bytes.hex()[:16]}...')
"
    chown hyve:hyve "$DATA_DIR/node_private.key" "$DATA_DIR/node_public.key"
fi

# Read node ID (xxd with Python fallback)
if command -v xxd &>/dev/null; then
    NODE_ID=$(xxd -p "$DATA_DIR/node_public.key" | tr -d '\n' | head -c 16)
else
    NODE_ID=$("$INSTALL_DIR/venv/bin/python" -c "print(open('$DATA_DIR/node_public.key','rb').read().hex()[:16])" 2>/dev/null || echo "unknown")
fi
echo "  Node ID: ${NODE_ID}..."

# ─── Step 2: Detect hardware and classify tier ───
echo "[2/5] Detecting hardware..."

CPU_CORES=$(nproc)
RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
RAM_GB=$((RAM_KB / 1048576))
GPU="none"

if command -v nvidia-smi &>/dev/null; then
    GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l)
else
    GPU_COUNT=0
fi

# Tier classification (matches security/system_requirements.py)
TIER="OBSERVER"
if [[ $RAM_GB -ge 4 && $CPU_CORES -ge 2 ]]; then
    TIER="STANDARD"
fi
if [[ $RAM_GB -ge 8 && $CPU_CORES -ge 4 ]]; then
    TIER="PERFORMANCE"
fi
if [[ $RAM_GB -ge 16 && $CPU_CORES -ge 8 && $GPU_COUNT -ge 1 ]]; then
    TIER="COMPUTE_HOST"
fi

echo "  CPU: ${CPU_CORES} cores"
echo "  RAM: ${RAM_GB}GB"
echo "  GPU: ${GPU:-none} (${GPU_COUNT} device(s))"
echo "  Tier: ${TIER}"

# ─── Step 3: Configure services per tier ───
echo "[3/5] Configuring services for tier: ${TIER}..."

# Backend + Discovery always enabled (already in hyve.target)
# Agent daemon for STANDARD+
if [[ "$TIER" == "OBSERVER" ]]; then
    systemctl disable hyve-agent-daemon.service 2>/dev/null || true
    systemctl disable hyve-vision.service 2>/dev/null || true
    systemctl disable hyve-llm.service 2>/dev/null || true
    echo "  Observer mode: backend + discovery only"
fi

if [[ "$TIER" == "STANDARD" ]]; then
    systemctl enable hyve-agent-daemon.service
    systemctl disable hyve-vision.service 2>/dev/null || true
    systemctl disable hyve-llm.service 2>/dev/null || true
    echo "  Standard mode: + agent daemon"
fi

if [[ "$TIER" == "PERFORMANCE" ]]; then
    systemctl enable hyve-agent-daemon.service
    # Enable vision if model exists
    if [[ -d /opt/hyve/models/minicpm ]]; then
        systemctl enable hyve-vision.service
    fi
    # Enable LLM if model exists
    if [[ -f /opt/hyve/models/default.gguf ]]; then
        systemctl enable hyve-llm.service
    fi
    echo "  Performance mode: + vision + LLM (if models present)"
fi

if [[ "$TIER" == "COMPUTE_HOST" ]]; then
    systemctl enable hyve-agent-daemon.service
    systemctl enable hyve-vision.service 2>/dev/null || true
    systemctl enable hyve-llm.service 2>/dev/null || true
    echo "  Compute host mode: all services enabled"

    # Auto-download default GGUF model in background
    if [[ ! -f /opt/hyve/models/default.gguf ]]; then
        echo "  Downloading default model (background)..."
        # Download a small quantized model for local inference
        # This runs in the background so first-boot doesn't block
        (
            MODEL_URL="${HYVE_DEFAULT_MODEL_URL:-https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf}"
            MODEL_PATH="/opt/hyve/models/default.gguf"
            mkdir -p /opt/hyve/models
            if command -v curl &>/dev/null; then
                curl -sL -o "$MODEL_PATH" "$MODEL_URL" 2>/var/log/hyve/model-download.log
            elif command -v wget &>/dev/null; then
                wget -q -O "$MODEL_PATH" "$MODEL_URL" 2>/var/log/hyve/model-download.log
            fi
            if [[ -f "$MODEL_PATH" && -s "$MODEL_PATH" ]]; then
                chown hyve:hyve "$MODEL_PATH"
                echo "[HyveOS] Default model downloaded: $MODEL_PATH" >> /var/log/hyve/first-boot.log
                # Restart LLM service now that model exists
                systemctl restart hyve-llm.service 2>/dev/null || true
            else
                echo "[HyveOS] Model download failed" >> /var/log/hyve/first-boot.log
                rm -f "$MODEL_PATH"
            fi
        ) &
        echo "  Model download started in background (see /var/log/hyve/model-download.log)"
    fi
fi

# ─── Step 4: Initialize database ───
echo "[4/5] Initializing database..."

"$INSTALL_DIR/venv/bin/python" -c "
import os
os.environ['HEVOLVE_DB_PATH'] = '$DATA_DIR/hevolve_database.db'
from integrations.social.models import Base, get_engine
from integrations.social.migrations import run_migrations
engine = get_engine()
Base.metadata.create_all(engine)
run_migrations()
print('Database initialized.')
"

# ─── Step 5: Start services ───
echo "[5/5] Starting HyveOS services..."

systemctl daemon-reload
systemctl restart hyve.target

# Wait for backend
for i in $(seq 1 20); do
    if curl -s "http://localhost:6777/status" >/dev/null 2>&1; then
        echo "  Backend is running."
        break
    fi
    sleep 2
done

# ─── Mark completion ───
touch "$MARKER"
chown hyve:hyve "$MARKER"

# ─── Welcome message ───
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "============================================================"
echo "  HyveOS first boot complete!"
echo ""
echo "  Node ID:     ${NODE_ID}..."
echo "  Tier:        ${TIER}"
echo "  Dashboard:   http://${IP:-localhost}:6777"
echo "  CLI:         hyve status"
echo ""
echo "  Next steps:"
echo "    1. Edit API keys: sudo nano /etc/hyve/hyve.env"
echo "    2. Join a hive:   hyve join http://<peer>:6777"
echo "    3. Check status:  hyve health"
echo ""
echo "  Humans are always in control."
echo "============================================================"
