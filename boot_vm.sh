#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════
# HART OS — VM Boot Script (WSL2 + QEMU/KVM)
# ═══════════════════════════════════════════════════════════════
#
# Usage (from Git Bash on Windows):
#   MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu-22.04 -u root -- \
#     /bin/bash /mnt/c/Users/sathi/PycharmProjects/HARTOS/boot_vm.sh
#
# Must run as root (for /dev/kvm chmod). The VM runs as sathish.
#
# Prerequisites:
#   - nix_build.sh completed successfully (ISO at nixos/result/iso/)
#   - QEMU installed in WSL2 (qemu-system-x86_64)
#   - /dev/kvm available (WSL2 with nested virtualization)
#
# Ports:
#   - SSH: localhost:2222 -> VM:22
#
# To connect after boot:
#   ssh -p 2222 nixos@localhost  (set password first via serial console)
#
# Serial console controls:
#   Ctrl+A X  = quit QEMU
#   Ctrl+A H  = help

REPO="/mnt/c/Users/sathi/PycharmProjects/HARTOS"
ISO="$REPO/nixos/result/iso/hart-os-1.0.0-server-x86_64-linux.iso"
OVMF_VARS="/home/sathish/hart-ovmf-vars.fd"

# ─── Source Nix (for OVMF) ───
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh 2>/dev/null || true

# ─── Verify ISO ───
if [ ! -f "$ISO" ]; then
    echo "ERROR: ISO not found at $ISO"
    echo "Run nix_build.sh first."
    exit 1
fi
echo "ISO: $(ls -lh "$ISO" | awk '{print $5}')"

# ─── KVM permissions ───
chmod 666 /dev/kvm 2>/dev/null || {
    echo "ERROR: /dev/kvm not available. Enable nested virtualization in WSL2."
    exit 1
}
echo "KVM: ready"

# ─── Get OVMF firmware ───
OVMF_DIR=$(nix build nixpkgs#OVMF.fd --no-link --print-out-paths 2>/dev/null | tail -1)
if [ -z "$OVMF_DIR" ] || [ ! -d "$OVMF_DIR" ]; then
    echo "ERROR: Could not fetch OVMF firmware"
    exit 1
fi
cp "$OVMF_DIR/FV/OVMF_VARS.fd" "$OVMF_VARS"
chmod 666 "$OVMF_VARS"
echo "OVMF: $OVMF_DIR"

# ─── Kill any existing QEMU on port 2222 ───
if ss -tlnp 2>/dev/null | grep -q ':2222'; then
    echo "Killing existing QEMU on port 2222..."
    kill "$(lsof -t -i:2222)" 2>/dev/null || true
    sleep 2
fi

# ─── Boot VM ───
echo ""
echo "=== Booting HART OS VM ==="
echo "  SSH: ssh -p 2222 nixos@localhost (after setting password)"
echo "  Serial console active. Ctrl+A X to quit."
echo ""

exec su -l sathish -c "qemu-system-x86_64 \
    -enable-kvm \
    -machine q35 \
    -cpu host \
    -m 4096 \
    -smp 4 \
    -drive if=pflash,format=raw,readonly=on,file=$OVMF_DIR/FV/OVMF_CODE.fd \
    -drive if=pflash,format=raw,file=$OVMF_VARS \
    -cdrom $ISO \
    -boot d \
    -nographic \
    -serial mon:stdio \
    -net nic,model=virtio \
    -net user,hostfwd=tcp::2222-:22 \
    -no-reboot"
