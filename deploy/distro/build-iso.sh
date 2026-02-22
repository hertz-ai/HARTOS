#!/usr/bin/env bash
# ============================================================
# HART OS ISO Builder
#
# Builds a bootable HART OS ISO based on Ubuntu Server 22.04 LTS.
# Uses live-build for ISO customization.
#
# Prerequisites:
#   sudo apt install live-build syslinux-utils xorriso
#
# Usage:
#   sudo bash build-iso.sh [OPTIONS]
#
# Options:
#   --variant NAME   Build variant: server|desktop|edge (default: server)
#   --version X.Y.Z  Version string (default: 1.0.0)
#   --output DIR     Output directory (default: ./dist)
#   --no-clean       Don't clean build directory after
#   --help           Show this message
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Defaults
VARIANT="server"
HART_VERSION="1.0.0"
OUTPUT_DIR="$REPO_DIR/dist"
NO_CLEAN=false
ARCH="amd64"
CODENAME="jammy"  # Ubuntu 22.04

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${CYAN}[HART OS ISO]${NC} $1"; }
warn() { echo -e "${YELLOW}[HART OS ISO]${NC} $1"; }

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --variant) VARIANT="$2"; shift 2 ;;
        --version) HART_VERSION="$2"; shift 2 ;;
        --output)  OUTPUT_DIR="$2"; shift 2 ;;
        --no-clean) NO_CLEAN=true; shift ;;
        --help|-h)
            head -16 "$0" | tail -11
            exit 0
            ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

ISO_NAME="hart-os-${HART_VERSION}-${VARIANT}-${ARCH}"
BUILD_DIR="/tmp/hart-iso-build-$$"

log "Building HART OS ISO: $ISO_NAME"
log "  Variant: $VARIANT"
log "  Version: $HART_VERSION"
log "  Base: Ubuntu $CODENAME ($ARCH)"

# ─── Prerequisites ───
for cmd in lb xorriso; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Missing: $cmd"
        echo "Install: sudo apt install live-build xorriso syslinux-utils"
        exit 1
    fi
done

# ─── Setup build directory ───
log "Setting up build directory..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# ─── Configure live-build ───
log "Configuring live-build..."
lb config \
    --distribution "$CODENAME" \
    --architectures "$ARCH" \
    --linux-flavours generic \
    --bootappend-live "boot=live hostname=hart-node username=hart" \
    --apt-recommends false \
    --binary-images iso-hybrid \
    --debian-installer live \
    --memtest none

# ─── Parse variant config (INI files) ───
VARIANT_CONF="$SCRIPT_DIR/variants/hart-os-${VARIANT}.conf"
VARIANT_EXCLUDES=""
VARIANT_EXTRA_PKGS=""
if [[ -f "$VARIANT_CONF" ]]; then
    log "Reading variant config: $VARIANT_CONF"
    # Parse include/exclude lines from [packages] section
    in_packages=false
    while IFS= read -r line; do
        [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
        if [[ "$line" =~ ^\[packages\] ]]; then in_packages=true; continue; fi
        if [[ "$line" =~ ^\[.*\] ]]; then in_packages=false; continue; fi
        if $in_packages; then
            if [[ "$line" =~ ^include\ *=\ *(.+) ]]; then
                VARIANT_EXTRA_PKGS="$VARIANT_EXTRA_PKGS ${BASH_REMATCH[1]}"
            elif [[ "$line" =~ ^exclude\ *=\ *(.+) ]]; then
                VARIANT_EXCLUDES="$VARIANT_EXCLUDES ${BASH_REMATCH[1]}"
            fi
        fi
    done < "$VARIANT_CONF"
fi

# ─── Add packages ───
log "Adding packages..."
mkdir -p config/package-lists

# Core packages (all variants)
cat > config/package-lists/hart-core.list.chroot <<EOF
python3.10
python3.10-venv
python3.10-dev
python3-pip
python3-pil
ufw
git
curl
rsync
htop
xxd
openssh-server
EOF

# Desktop variant: add desktop environment + desktop integration
if [[ "$VARIANT" == "desktop" ]]; then
    cat > config/package-lists/hart-desktop.list.chroot <<EOF
ubuntu-desktop-minimal
python3-dbus
python3-gi
gir1.2-glib-2.0
policykit-1
gnome-shell-extensions
firefox
EOF
fi

# Edge variant: strip unnecessary packages
if [[ "$VARIANT" == "edge" ]]; then
    mkdir -p config/package-lists
    cat > config/package-lists/hart-edge-remove.list.chroot <<EOF
!man-db
!manpages
!snapd
!cloud-init
!landscape-common
EOF
fi

# Add any extra packages from variant config
if [[ -n "$VARIANT_EXTRA_PKGS" ]]; then
    echo "$VARIANT_EXTRA_PKGS" | tr ' ' '\n' | sort -u \
        > config/package-lists/hart-variant-extra.list.chroot
fi

# ─── Add HART OS code ───
log "Adding HART OS application code..."
mkdir -p config/includes.chroot/opt/hart

# Copy application (excluding dev artifacts)
rsync -a \
    --exclude='.git' --exclude='__pycache__' --exclude='venv*' \
    --exclude='*.pyc' --exclude='tests/' --exclude='agent_data/*.db' \
    --exclude='.env' --exclude='*.egg-info' --exclude='.idea' \
    --exclude='autogen-*' --exclude='docs/' \
    "$REPO_DIR/" config/includes.chroot/opt/hart/

# ─── Add branding ───
log "Adding branding..."

# /etc/os-release
mkdir -p config/includes.chroot/etc
cp "$SCRIPT_DIR/branding/hart-os-release" config/includes.chroot/etc/os-release

# /etc/issue
cp "$SCRIPT_DIR/branding/hart-issue" config/includes.chroot/etc/issue

# MOTD
mkdir -p config/includes.chroot/etc/update-motd.d
cp "$SCRIPT_DIR/branding/hart-motd.sh" config/includes.chroot/etc/update-motd.d/99-hart
chmod +x config/includes.chroot/etc/update-motd.d/99-hart

# Plymouth theme
if [[ -d "$SCRIPT_DIR/branding/plymouth" ]]; then
    mkdir -p config/includes.chroot/usr/share/plymouth/themes
    cp -r "$SCRIPT_DIR/branding/plymouth/hart-theme" \
        config/includes.chroot/usr/share/plymouth/themes/
    # Generate logo if missing (requires Pillow in build env or fallback)
    if [[ ! -f config/includes.chroot/usr/share/plymouth/themes/hart-theme/hart-logo.png ]]; then
        python3 "$SCRIPT_DIR/branding/plymouth/hart-theme/generate-logo.py" 2>/dev/null || \
            log "  Warning: Could not generate Plymouth logo (install Pillow)"
        if [[ -f "$SCRIPT_DIR/branding/plymouth/hart-theme/hart-logo.png" ]]; then
            cp "$SCRIPT_DIR/branding/plymouth/hart-theme/hart-logo.png" \
                config/includes.chroot/usr/share/plymouth/themes/hart-theme/
        fi
    fi
fi

# GRUB
if [[ -f "$SCRIPT_DIR/branding/grub/hart-grub.cfg" ]]; then
    mkdir -p config/includes.chroot/etc/default/grub.d
    cp "$SCRIPT_DIR/branding/grub/hart-grub.cfg" \
        config/includes.chroot/etc/default/grub.d/hart.cfg
fi

# ─── Add system configs ───
log "Adding system configuration..."

# systemd units
mkdir -p config/includes.chroot/etc/systemd/system
cp "$REPO_DIR/deploy/linux/systemd/"* config/includes.chroot/etc/systemd/system/

# First-boot service
cp "$SCRIPT_DIR/first-boot/hart-first-boot.sh" config/includes.chroot/opt/hart/deploy/distro/first-boot/ 2>/dev/null || true
cp "$SCRIPT_DIR/first-boot/hart-first-boot.service" config/includes.chroot/etc/systemd/system/

# Kernel tuning
mkdir -p config/includes.chroot/etc/sysctl.d
mkdir -p config/includes.chroot/etc/security/limits.d
cp "$SCRIPT_DIR/kernel/99-hart-sysctl.conf" config/includes.chroot/etc/sysctl.d/
cp "$SCRIPT_DIR/kernel/hart-limits.conf" config/includes.chroot/etc/security/limits.d/

# Firewall profile
mkdir -p config/includes.chroot/etc/ufw/applications.d
cp "$REPO_DIR/deploy/linux/firewall/hart-ufw.profile" \
    config/includes.chroot/etc/ufw/applications.d/hart

# CLI tool
mkdir -p config/includes.chroot/usr/local/bin
cp "$REPO_DIR/deploy/linux/hart-cli.py" config/includes.chroot/usr/local/bin/hart
chmod +x config/includes.chroot/usr/local/bin/hart

# Environment template
mkdir -p config/includes.chroot/etc/hart
cp "$REPO_DIR/deploy/linux/hart.env.template" config/includes.chroot/etc/hart/hart.env

# Write variant to /etc/hart/variant
echo "$VARIANT" > config/includes.chroot/etc/hart/variant

# ─── Add autoinstall for subiquity ───
log "Adding autoinstall configuration..."
mkdir -p config/includes.chroot/autoinstall
cp "$SCRIPT_DIR/autoinstall/user-data" config/includes.chroot/autoinstall/
cp "$SCRIPT_DIR/autoinstall/meta-data" config/includes.chroot/autoinstall/
cp "$SCRIPT_DIR/autoinstall/vendor-data" config/includes.chroot/autoinstall/

# ─── Add hook to enable services ───
mkdir -p config/hooks/live
cat > config/hooks/live/99-hart-enable.hook.chroot <<'HOOKEOF'
#!/bin/bash
set -e

# Enable HART OS services
systemctl enable hart.target || true
systemctl enable hart-first-boot.service || true

# Create hart user if not exists
getent group hart >/dev/null || groupadd --system hart
getent passwd hart >/dev/null || useradd --system --gid hart --home-dir /var/lib/hart --shell /usr/sbin/nologin hart

# Create directories
mkdir -p /var/lib/hart /var/log/hart /opt/hart/agent_data /opt/hart/models
chown -R hart:hart /var/lib/hart /var/log/hart

# Setup venv and install dependencies (FAIL if pip install fails)
if command -v python3.10 &>/dev/null; then
    python3.10 -m venv /opt/hart/venv
    /opt/hart/venv/bin/pip install --upgrade pip -q
    /opt/hart/venv/bin/pip install -r /opt/hart/requirements.txt -q
    echo "[HART OS] Python dependencies installed successfully."
else
    echo "[HART OS] ERROR: python3.10 not found in chroot!" >&2
    exit 1
fi

# Plymouth theme activation
if [ -f /usr/share/plymouth/themes/hart-theme/hart-theme.plymouth ]; then
    update-alternatives --install /usr/share/plymouth/themes/default.plymouth \
        default.plymouth /usr/share/plymouth/themes/hart-theme/hart-theme.plymouth 200 || true
    update-initramfs -u 2>/dev/null || true
    echo "[HART OS] Plymouth theme activated."
fi

# D-Bus service install (for desktop variant)
if [ -f /opt/hart/deploy/linux/dbus/com.hart.Agent.conf ]; then
    cp /opt/hart/deploy/linux/dbus/com.hart.Agent.conf /etc/dbus-1/system.d/ || true
    echo "[HART OS] D-Bus policy installed."
fi
HOOKEOF
chmod +x config/hooks/live/99-hart-enable.hook.chroot

# ─── Build ISO ───
log "Building ISO (this may take 15-30 minutes)..."
lb build 2>&1 | tee build.log

# ─── Move output ───
mkdir -p "$OUTPUT_DIR"
ISO_FILE=$(ls *.iso 2>/dev/null | head -1)

if [[ -z "$ISO_FILE" ]]; then
    warn "ISO build failed. Check build.log"
    exit 1
fi

mv "$ISO_FILE" "$OUTPUT_DIR/${ISO_NAME}.iso"

# Compute checksum
cd "$OUTPUT_DIR"
sha256sum "${ISO_NAME}.iso" > "${ISO_NAME}.iso.sha256"

# ─── Clean up ───
if ! $NO_CLEAN; then
    rm -rf "$BUILD_DIR"
fi

# ─── Summary ───
ISO_SIZE=$(du -h "$OUTPUT_DIR/${ISO_NAME}.iso" | cut -f1)
log ""
log "============================================================"
log "  HART OS ISO built successfully!"
log "============================================================"
log ""
log "  ISO:      $OUTPUT_DIR/${ISO_NAME}.iso"
log "  Checksum: $OUTPUT_DIR/${ISO_NAME}.iso.sha256"
log "  Size:     $ISO_SIZE"
log "  Variant:  $VARIANT"
log ""
log "  Test with QEMU:"
log "    qemu-system-x86_64 -m 4096 -cdrom $OUTPUT_DIR/${ISO_NAME}.iso -boot d"
log ""
