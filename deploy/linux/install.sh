#!/usr/bin/env bash
# ============================================================
# HART OS Installer — Ubuntu Server 22.04+
# Crowdsourced Agentic Intelligence Platform
#
# Usage:
#   sudo bash install.sh [OPTIONS]
#
# Options:
#   --dry-run       Check prerequisites only, don't install
#   --join-peer URL Auto-join an existing hive after install
#   --port N        Override backend port (default: 6777)
#   --no-vision     Skip MiniCPM vision service
#   --no-llm        Skip llama.cpp local inference
#   --from-iso      Called from ISO autoinstall (skip user prompts)
#   --uninstall     Remove HART OS completely
#   --help          Show this message
# ============================================================

set -euo pipefail

HART_VERSION="1.0.0"
INSTALL_DIR="/opt/hart"
CONFIG_DIR="/etc/hart"
DATA_DIR="/var/lib/hart"
LOG_DIR="/var/log/hart"
SYSTEMD_DIR="/etc/systemd/system"
HART_USER="hart"
HART_GROUP="hart"

# Defaults
BACKEND_PORT=6777
JOIN_PEER=""
DRY_RUN=false
NO_VISION=false
NO_LLM=false
FROM_ISO=false
UNINSTALL=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[HART OS]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[HART OS]${NC} $1"; }
log_error() { echo -e "${RED}[HART OS]${NC} $1"; }
log_step()  { echo -e "${CYAN}[HART OS]${NC} >>> $1"; }

# ============================================================
# Parse arguments
# ============================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true; shift ;;
        --join-peer)  JOIN_PEER="$2"; shift 2 ;;
        --port)       BACKEND_PORT="$2"; shift 2 ;;
        --no-vision)  NO_VISION=true; shift ;;
        --no-llm)     NO_LLM=true; shift ;;
        --from-iso)   FROM_ISO=true; shift ;;
        --uninstall)  UNINSTALL=true; shift ;;
        --help|-h)
            head -20 "$0" | tail -15
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================
# Root check
# ============================================================
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

# ============================================================
# Uninstall
# ============================================================
if $UNINSTALL; then
    log_step "Uninstalling HART OS..."
    systemctl stop hart.target 2>/dev/null || true
    systemctl disable hart.target 2>/dev/null || true

    rm -f "$SYSTEMD_DIR"/hart-backend.service
    rm -f "$SYSTEMD_DIR"/hart-discovery.service
    rm -f "$SYSTEMD_DIR"/hart-vision.service
    rm -f "$SYSTEMD_DIR"/hart-llm.service
    rm -f "$SYSTEMD_DIR"/hart-agent-daemon.service
    rm -f "$SYSTEMD_DIR"/hart.target
    systemctl daemon-reload

    rm -rf "$INSTALL_DIR"
    rm -rf "$CONFIG_DIR"
    rm -rf "$LOG_DIR"
    # Preserve data dir — user must explicitly remove it
    log_warn "Data directory $DATA_DIR preserved. Remove manually if desired."

    userdel "$HART_USER" 2>/dev/null || true
    groupdel "$HART_GROUP" 2>/dev/null || true

    rm -f /usr/local/bin/hart
    rm -f /etc/ufw/applications.d/hart

    log_info "HART OS uninstalled successfully."
    exit 0
fi

# ============================================================
# Pre-flight checks
# ============================================================
log_step "Checking prerequisites..."

ERRORS=0

# OS check
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    if [[ "$ID" != "ubuntu" && "$ID_LIKE" != *"ubuntu"* && "$ID" != "debian" && "$ID" != "hart-os" ]]; then
        log_error "Unsupported OS: $PRETTY_NAME (requires Ubuntu 22.04+ or Debian 12+)"
        ERRORS=$((ERRORS + 1))
    fi
    if [[ "$ID" == "ubuntu" ]]; then
        MAJOR_VER=$(echo "$VERSION_ID" | cut -d. -f1)
        if [[ "$MAJOR_VER" -lt 22 ]]; then
            log_error "Ubuntu $VERSION_ID too old (requires 22.04+)"
            ERRORS=$((ERRORS + 1))
        fi
    fi
else
    log_error "Cannot detect OS (missing /etc/os-release)"
    ERRORS=$((ERRORS + 1))
fi

# systemd check
if ! command -v systemctl &>/dev/null; then
    log_error "systemd not found (required)"
    ERRORS=$((ERRORS + 1))
fi

# RAM check (4GB minimum)
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$((TOTAL_RAM_KB / 1048576))
if [[ $TOTAL_RAM_GB -lt 3 ]]; then
    log_error "Insufficient RAM: ${TOTAL_RAM_GB}GB (requires 4GB+)"
    ERRORS=$((ERRORS + 1))
else
    log_info "RAM: ${TOTAL_RAM_GB}GB OK"
fi

# Disk check (10GB minimum in /opt)
AVAIL_KB=$(df /opt --output=avail | tail -1 | tr -d ' ')
AVAIL_GB=$((AVAIL_KB / 1048576))
if [[ $AVAIL_GB -lt 10 ]]; then
    log_error "Insufficient disk space in /opt: ${AVAIL_GB}GB (requires 10GB+)"
    ERRORS=$((ERRORS + 1))
else
    log_info "Disk space: ${AVAIL_GB}GB available OK"
fi

# Python check
PYTHON_CMD=""
for cmd in python3.10 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        PY_VER=$("$cmd" --version 2>&1 | awk '{print $2}')
        PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
        PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
        if [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -ge 10 && "$PY_MINOR" -le 11 ]]; then
            PYTHON_CMD="$cmd"
            log_info "Python: $PY_VER ($cmd) OK"
            break
        elif [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -ge 12 ]]; then
            log_warn "Python $PY_VER detected but 3.12+ has pydantic 1.x compat issues. Prefer 3.10 or 3.11."
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    log_warn "Python 3.10+ not found. Will attempt to install."
fi

# GPU detection
GPU_INFO="none"
if command -v nvidia-smi &>/dev/null; then
    GPU_INFO="nvidia ($(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1))"
    log_info "GPU: $GPU_INFO"
elif [[ -d /sys/class/drm ]]; then
    GPU_INFO="integrated"
    log_info "GPU: integrated graphics"
else
    log_info "GPU: none detected (CPU-only mode)"
fi

if [[ $ERRORS -gt 0 ]]; then
    log_error "$ERRORS prerequisite check(s) failed."
    exit 1
fi

if $DRY_RUN; then
    echo ""
    log_info "=== DRY RUN COMPLETE ==="
    log_info "All prerequisites passed. Run without --dry-run to install."
    log_info "  OS: ${PRETTY_NAME:-unknown}"
    log_info "  RAM: ${TOTAL_RAM_GB}GB"
    log_info "  Disk: ${AVAIL_GB}GB available"
    log_info "  Python: ${PYTHON_CMD:-will install}"
    log_info "  GPU: $GPU_INFO"
    exit 0
fi

# ============================================================
# Install Python if needed
# ============================================================
if [[ -z "$PYTHON_CMD" ]]; then
    log_step "Installing Python 3.10..."
    apt-get update -qq
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.10 python3.10-venv python3.10-dev
    PYTHON_CMD="python3.10"
fi

# Ensure venv module available
if ! "$PYTHON_CMD" -m venv --help &>/dev/null; then
    log_step "Installing python3-venv..."
    apt-get install -y -qq python3.10-venv 2>/dev/null || apt-get install -y -qq python3-venv
fi

# Ensure xxd available (for node ID display)
if ! command -v xxd &>/dev/null; then
    log_step "Installing xxd..."
    apt-get install -y -qq xxd 2>/dev/null || true
fi

# ============================================================
# Create user and directories
# ============================================================
log_step "Creating hart user and directories..."

if ! getent group "$HART_GROUP" &>/dev/null; then
    groupadd --system "$HART_GROUP"
fi
if ! getent passwd "$HART_USER" &>/dev/null; then
    useradd --system --gid "$HART_GROUP" --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$HART_USER"
fi

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$INSTALL_DIR/models"
chown -R "$HART_USER:$HART_GROUP" "$DATA_DIR" "$LOG_DIR"

# ============================================================
# Copy application code
# ============================================================
log_step "Installing HART OS application..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# If running from install bundle (tar.gz extracted), source is parent
if [[ -f "$SOURCE_DIR/langchain_gpt_api.py" ]]; then
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='venv*' \
          --exclude='*.pyc' --exclude='tests/' --exclude='agent_data/*.db' \
          --exclude='.env' --exclude='*.egg-info' \
          "$SOURCE_DIR/" "$INSTALL_DIR/"
elif [[ -f "$SCRIPT_DIR/../../langchain_gpt_api.py" ]]; then
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='venv*' \
          --exclude='*.pyc' --exclude='tests/' --exclude='agent_data/*.db' \
          --exclude='.env' --exclude='*.egg-info' \
          "$SCRIPT_DIR/../../" "$INSTALL_DIR/"
else
    log_error "Cannot find application source. Expected langchain_gpt_api.py in parent directory."
    exit 1
fi

chown -R "$HART_USER:$HART_GROUP" "$INSTALL_DIR"

# ============================================================
# Create virtual environment and install dependencies
# ============================================================
log_step "Setting up Python virtual environment..."

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    "$PYTHON_CMD" -m venv "$INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

log_info "Python dependencies installed."

# ── HevolveAI source protection ──
# Compile .py → .pyc, strip source, clean metadata that leaks git URLs
if [[ -f "$INSTALL_DIR/scripts/compile_hevolveai.py" ]]; then
    log_step "Protecting HevolveAI source code..."
    "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/scripts/compile_hevolveai.py" \
        --strip-source \
        --manifest-out "$INSTALL_DIR/security/hevolveai_manifest.json" 2>&1 || true

    # Remove .dist-info metadata that leaks install source URL
    find "$INSTALL_DIR/venv/lib" -path '*hevolveai*dist-info/direct_url.json' -delete 2>/dev/null || true
    find "$INSTALL_DIR/venv/lib" -path '*embodied*ai*dist-info/direct_url.json' -delete 2>/dev/null || true
    find "$INSTALL_DIR/venv/lib" -path '*hevolveai*dist-info/RECORD' -delete 2>/dev/null || true
    find "$INSTALL_DIR/venv/lib" -path '*embodied*ai*dist-info/RECORD' -delete 2>/dev/null || true

    log_info "HevolveAI source protected (compiled + stripped)."
fi

# ============================================================
# Generate Ed25519 node keypair
# ============================================================
log_step "Generating node identity..."

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
    chown "$HART_USER:$HART_GROUP" "$DATA_DIR/node_private.key" "$DATA_DIR/node_public.key"
    log_info "Node keypair generated."
else
    log_info "Node keypair already exists."
fi

# Read node ID (xxd with Python fallback)
if command -v xxd &>/dev/null; then
    NODE_ID=$(xxd -p "$DATA_DIR/node_public.key" | tr -d '\n' | head -c 16)
else
    NODE_ID=$("$INSTALL_DIR/venv/bin/python" -c "print(open('$DATA_DIR/node_public.key','rb').read().hex()[:16])" 2>/dev/null || echo "unknown")
fi
log_info "Node ID: ${NODE_ID}..."

# ============================================================
# Install systemd service units
# ============================================================
log_step "Installing systemd services..."

cp "$INSTALL_DIR/deploy/linux/systemd/hart-backend.service" "$SYSTEMD_DIR/"
cp "$INSTALL_DIR/deploy/linux/systemd/hart-discovery.service" "$SYSTEMD_DIR/"
cp "$INSTALL_DIR/deploy/linux/systemd/hart-agent-daemon.service" "$SYSTEMD_DIR/"
cp "$INSTALL_DIR/deploy/linux/systemd/hart.target" "$SYSTEMD_DIR/"

if ! $NO_VISION; then
    cp "$INSTALL_DIR/deploy/linux/systemd/hart-vision.service" "$SYSTEMD_DIR/"
    log_info "Vision service installed."
fi

if ! $NO_LLM; then
    cp "$INSTALL_DIR/deploy/linux/systemd/hart-llm.service" "$SYSTEMD_DIR/"
    log_info "LLM service installed."
fi

# D-Bus service (for desktop environments)
if [[ -f "$INSTALL_DIR/deploy/linux/systemd/hart-dbus.service" ]]; then
    cp "$INSTALL_DIR/deploy/linux/systemd/hart-dbus.service" "$SYSTEMD_DIR/"
    if [[ -f "$INSTALL_DIR/deploy/linux/dbus/com.hart.Agent.conf" ]]; then
        mkdir -p /etc/dbus-1/system.d
        cp "$INSTALL_DIR/deploy/linux/dbus/com.hart.Agent.conf" /etc/dbus-1/system.d/
    fi
    log_info "D-Bus service installed."
fi

# ============================================================
# Configure environment
# ============================================================
log_step "Configuring environment..."

if [[ ! -f "$CONFIG_DIR/hart.env" ]]; then
    cp "$INSTALL_DIR/deploy/linux/hart.env.template" "$CONFIG_DIR/hart.env"
    # Set actual values
    sed -i "s|^HEVOLVE_DB_PATH=.*|HEVOLVE_DB_PATH=$DATA_DIR/hevolve_database.db|" "$CONFIG_DIR/hart.env"
    sed -i "s|^HARTOS_BACKEND_PORT=.*|HARTOS_BACKEND_PORT=$BACKEND_PORT|" "$CONFIG_DIR/hart.env"
    chmod 600 "$CONFIG_DIR/hart.env"
    chown "$HART_USER:$HART_GROUP" "$CONFIG_DIR/hart.env"
    log_info "Environment configured at $CONFIG_DIR/hart.env"
else
    log_info "Environment file exists, preserving."
fi

# ============================================================
# Configure firewall
# ============================================================
log_step "Configuring firewall..."

if command -v ufw &>/dev/null; then
    cp "$INSTALL_DIR/deploy/linux/firewall/hart-ufw.profile" /etc/ufw/applications.d/hart 2>/dev/null || true
    ufw allow "$BACKEND_PORT/tcp" >/dev/null 2>&1 || true
    ufw allow "6780/udp" >/dev/null 2>&1 || true
    log_info "UFW rules configured."
elif command -v firewall-cmd &>/dev/null; then
    cp "$INSTALL_DIR/deploy/linux/firewall/hart-firewalld.xml" /etc/firewalld/services/hart.xml 2>/dev/null || true
    firewall-cmd --permanent --add-service=hart 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    log_info "firewalld rules configured."
else
    log_warn "No firewall detected. Ensure ports $BACKEND_PORT/tcp and 6780/udp are accessible."
fi

# ============================================================
# Install CLI tool
# ============================================================
log_step "Installing HART OS CLI..."

cp "$INSTALL_DIR/deploy/linux/hart-cli.py" /usr/local/bin/hart
chmod +x /usr/local/bin/hart
log_info "CLI installed: /usr/local/bin/hart"

# ============================================================
# Enable and start services
# ============================================================
log_step "Enabling HART OS services..."

systemctl daemon-reload
systemctl enable hart.target
systemctl start hart.target

# Wait for backend to come up
log_info "Waiting for backend to start..."
for i in $(seq 1 30); do
    if curl -s "http://localhost:$BACKEND_PORT/status" >/dev/null 2>&1; then
        log_info "Backend is running."
        break
    fi
    sleep 1
done

# ============================================================
# Auto-join hive (if specified)
# ============================================================
if [[ -n "$JOIN_PEER" ]]; then
    log_step "Joining hive at $JOIN_PEER..."
    curl -s -X POST "http://localhost:$BACKEND_PORT/api/social/peers/announce" \
         -H "Content-Type: application/json" \
         -d "{\"peer_url\": \"$JOIN_PEER\"}" >/dev/null 2>&1 || true
    log_info "Join request sent."
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "============================================================"
echo -e "${GREEN}  HART OS $HART_VERSION installed successfully!${NC}"
echo "============================================================"
echo ""
echo "  Node ID:    ${NODE_ID}..."
echo "  Dashboard:  http://localhost:$BACKEND_PORT"
echo "  Config:     $CONFIG_DIR/hart.env"
echo "  Data:       $DATA_DIR"
echo "  Logs:       journalctl -u hart-backend"
echo ""
echo "  Commands:"
echo "    hart status     — check service status"
echo "    hart health     — node health report"
echo "    hart logs       — view logs"
echo "    hart join URL   — join a hive network"
echo ""
echo "  Edit API keys:  sudo nano $CONFIG_DIR/hart.env"
echo "  Then restart:   sudo systemctl restart hart.target"
echo ""
log_info "Humans are always in control."
