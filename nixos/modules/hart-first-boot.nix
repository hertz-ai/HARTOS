{ config, lib, pkgs, ... }:

# HART OS First Boot Module
# Runs once after initial install:
#   1. Generate Ed25519 node keypair
#   2. Detect hardware and classify tier
#   3. Initialize database
#   4. Write boot audit entry (signed)
#
# Ported from deploy/distro/first-boot/hart-first-boot.sh

let
  cfg = config.hart;

  # Python with cryptography for Ed25519 keygen
  pythonWithCrypto = pkgs.python310.withPackages (ps: [ ps.cryptography ]);

  firstBootScript = pkgs.writeShellScript "hart-first-boot" ''
    set -euo pipefail

    MARKER="${cfg.dataDir}/.first-boot-done"
    DATA_DIR="${cfg.dataDir}"
    LOG="${cfg.logDir}/first-boot.log"

    mkdir -p "$(dirname "$LOG")"
    exec > >(tee -a "$LOG") 2>&1

    # Skip if already completed
    if [[ -f "$MARKER" ]]; then
      echo "[HART OS] First boot already completed. Skipping."
      exit 0
    fi

    echo "============================================================"
    echo "  HART OS First Boot Setup"
    echo "============================================================"
    echo ""

    # ─── Step 1: Generate Ed25519 node keypair ───
    echo "[1/4] Generating node identity..."

    if [[ ! -f "$DATA_DIR/node_private.key" ]]; then
      ${pythonWithCrypto}/bin/python3 -c "
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
      chown hart:hart "$DATA_DIR/node_private.key" "$DATA_DIR/node_public.key"
      # Immutable flag: even root cannot modify the private key
      ${pkgs.e2fsprogs}/bin/chattr +i "$DATA_DIR/node_private.key" 2>/dev/null || true
    fi

    NODE_ID=$(${pkgs.xxd}/bin/xxd -p "$DATA_DIR/node_public.key" | tr -d '\n' | head -c 16)
    echo "  Node ID: ''${NODE_ID}..."

    # ─── Step 2: Detect hardware and classify tier ───
    echo "[2/4] Detecting hardware..."

    CPU_CORES=$(${pkgs.coreutils}/bin/nproc)
    RAM_KB=$(${pkgs.gnugrep}/bin/grep MemTotal /proc/meminfo | ${pkgs.gawk}/bin/awk '{print $2}')
    RAM_GB=$((RAM_KB / 1048576))

    GPU="none"
    GPU_COUNT=0
    if command -v nvidia-smi &>/dev/null; then
      GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1) || true
      GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l) || GPU_COUNT=0
    fi

    # Tier classification (matches security/system_requirements.py)
    TIER="OBSERVER"
    [[ $RAM_GB -ge 4 && $CPU_CORES -ge 2 ]] && TIER="STANDARD"
    [[ $RAM_GB -ge 8 && $CPU_CORES -ge 4 ]] && TIER="PERFORMANCE"
    [[ $RAM_GB -ge 16 && $CPU_CORES -ge 8 && $GPU_COUNT -ge 1 ]] && TIER="COMPUTE_HOST"

    echo "  CPU: ''${CPU_CORES} cores"
    echo "  RAM: ''${RAM_GB}GB"
    echo "  GPU: ''${GPU:-none} (''${GPU_COUNT} device(s))"
    echo "  Tier: ''${TIER}"
    echo "  Variant: ${cfg.variant}"

    # Write tier for other services to read
    echo "$TIER" > "$DATA_DIR/capability_tier"
    chown hart:hart "$DATA_DIR/capability_tier"

    # ─── Step 3: Initialize database ───
    echo "[3/4] Initializing database..."

    # Database init happens at backend startup via SQLAlchemy create_all + migrations
    # Just ensure the env var points to the right path
    echo "  Database will be initialized on first backend start."

    # ─── Step 4: Boot audit ───
    echo "[4/4] Writing boot audit entry..."

    TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    ENTRY="''${TIMESTAMP} | ''${NODE_ID} | ''${TIER} | ${cfg.variant}"

    # Sign with Ed25519 private key
    SIGNATURE=$(${pythonWithCrypto}/bin/python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

entry = '''$ENTRY'''
with open('$DATA_DIR/node_private.key', 'rb') as f:
    key_bytes = f.read()

private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
signature = private_key.sign(entry.encode('utf-8'))
print(signature.hex())
" 2>>"$LOG") || SIGNATURE="UNSIGNED"

    FULL_ENTRY="''${ENTRY} | ''${SIGNATURE}"
    echo "$FULL_ENTRY" >> "$DATA_DIR/boot_audit.log"
    chown hart:hart "$DATA_DIR/boot_audit.log"
    chmod 644 "$DATA_DIR/boot_audit.log"
    ${pkgs.e2fsprogs}/bin/chattr +a "$DATA_DIR/boot_audit.log" 2>/dev/null || true

    echo "[BootAudit] Entry written and log set to append-only."

    # ─── Mark completion ───
    touch "$MARKER"
    chown hart:hart "$MARKER"

    # ─── Welcome message ───
    IP=$(hostname -I 2>/dev/null | ${pkgs.gawk}/bin/awk '{print $1}')
    echo ""
    echo "============================================================"
    echo "  HART OS first boot complete!"
    echo ""
    echo "  Node ID:     ''${NODE_ID}..."
    echo "  Tier:        ''${TIER}"
    echo "  Dashboard:   http://''${IP:-localhost}:${toString cfg.ports.backend}"
    echo "  CLI:         hart status"
    echo ""
    echo "  Humans are always in control."
    echo "============================================================"
  '';
in
{
  config = lib.mkIf cfg.enable {

    systemd.services.hart-first-boot = {
      description = "HART OS First Boot Setup";
      after = [ "network-online.target" "local-fs.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      unitConfig = {
        ConditionPathExists = "!${cfg.dataDir}/.first-boot-done";
      };

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = firstBootScript;

        # Runs as root (needs chattr, chown, hardware detection)
        # The script creates files owned by hart user
      };
    };
  };
}
