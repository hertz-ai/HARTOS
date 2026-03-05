#!/bin/bash
set -euo pipefail

# Source nix for OVMF path
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh 2>/dev/null || true

OVMF_DIR="/nix/store/qi61gzzxdcgqxrag7kc4qaj7937jm58i-OVMF-202602-fd/FV"
ISO="/mnt/c/Users/sathi/PycharmProjects/HARTOS/nixos/result/iso/hart-os-1.0.0-server-x86_64-linux.iso"

# Copy OVMF_VARS to a writable location (QEMU needs to write to it)
VARS_COPY="/tmp/hart-ovmf-vars.fd"
cp "$OVMF_DIR/OVMF_VARS.fd" "$VARS_COPY"

echo "=== Booting HART OS Server ISO in QEMU ==="
echo "  ISO: $ISO"
echo "  OVMF: $OVMF_DIR"
echo "  SSH: localhost:2222 -> VM:22"
echo "  Serial console: stdio"
echo ""

exec qemu-system-x86_64 \
  -machine q35 \
  -m 4096 \
  -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file="$OVMF_DIR/OVMF_CODE.fd" \
  -drive if=pflash,format=raw,file="$VARS_COPY" \
  -cdrom "$ISO" \
  -boot d \
  -nographic \
  -serial mon:stdio \
  -net nic,model=virtio \
  -net user,hostfwd=tcp::2222-:22 \
  -no-reboot
