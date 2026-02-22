#!/usr/bin/env bash
# ============================================================
# HART OS Flash Tool
#
# Flash HART OS to any target device — PCs, phones, RPi, USB
#
# Usage:
#   hart-flash --target /dev/sdb --variant server
#   hart-flash --target /dev/mmcblk0 --variant server --arch aarch64
#   hart-flash --target /dev/mmcblk0 --variant phone
#   hart-flash --variant server --format qcow2 --output hart.qcow2
#   hart-flash --variant server --format docker --output hart-server
#
# Supported formats:
#   iso, raw, sd, qcow2, vmware, vbox, docker, amazon, gce, azure
#
# ============================================================

set -euo pipefail

# ── Defaults ──
VARIANT="server"
ARCH=""
TARGET=""
FORMAT=""
OUTPUT=""
FLAKE_DIR=""
NO_CONFIRM=""

# ── Colors ──
CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ── Detect flake directory ──
find_flake_dir() {
    # Try relative to script location
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ -f "${script_dir}/../flake.nix" ]]; then
        echo "${script_dir}/.."
    elif [[ -f "${script_dir}/../../nixos/flake.nix" ]]; then
        echo "${script_dir}/../../nixos"
    elif [[ -f "./nixos/flake.nix" ]]; then
        echo "./nixos"
    elif [[ -f "./flake.nix" ]]; then
        echo "."
    else
        echo ""
    fi
}

# ── Usage ──
usage() {
    echo ""
    echo -e "${CYAN}  HART OS Flash Tool${NC}"
    echo "  =================="
    echo ""
    echo "  Usage: hart-flash [OPTIONS]"
    echo ""
    echo "  Options:"
    echo "    --target DEVICE     Target block device (e.g., /dev/sdb, /dev/mmcblk0)"
    echo "    --variant VARIANT   OS variant: server, desktop, edge, phone (default: server)"
    echo "    --arch ARCH         Architecture: x86_64, aarch64 (auto-detected)"
    echo "    --format FORMAT     Image format: iso, raw, sd, qcow2, vmware, vbox, docker, amazon, gce, azure"
    echo "    --output PATH       Output file (for non-device targets)"
    echo "    --flake-dir DIR     Path to nixos/ flake directory"
    echo "    --yes               Skip confirmation prompt"
    echo "    --list              List all available build targets"
    echo "    --help              Show this help"
    echo ""
    echo "  Examples:"
    echo "    # Flash desktop to USB drive"
    echo "    hart-flash --target /dev/sdb --variant desktop"
    echo ""
    echo "    # Flash to Raspberry Pi SD card"
    echo "    hart-flash --target /dev/mmcblk0 --variant server --arch aarch64"
    echo ""
    echo "    # Flash PinePhone SD card"
    echo "    hart-flash --target /dev/mmcblk0 --variant phone"
    echo ""
    echo "    # Build QEMU image"
    echo "    hart-flash --variant server --format qcow2 --output hart-server.qcow2"
    echo ""
    echo "    # Build Docker image"
    echo "    hart-flash --variant server --format docker"
    echo ""
}

# ── List available targets ──
list_targets() {
    echo ""
    echo -e "${CYAN}  Available HART OS Build Targets${NC}"
    echo "  ==============================="
    echo ""
    echo "  PC / Laptop (x86_64):"
    echo "    iso-server, iso-desktop, iso-edge"
    echo "    raw-server, raw-desktop, raw-edge"
    echo ""
    echo "  Raspberry Pi (aarch64):"
    echo "    sd-server-arm, sd-desktop-arm"
    echo ""
    echo "  Phone (aarch64):"
    echo "    sd-phone"
    echo ""
    echo "  Virtual Machines:"
    echo "    qcow2-server, qcow2-desktop"
    echo "    vmware-server, vmware-desktop"
    echo "    vbox-server, vbox-desktop"
    echo ""
    echo "  Cloud:"
    echo "    amazon-server, gce-server, azure-server"
    echo ""
    echo "  Container:"
    echo "    docker-server"
    echo ""
}

# ── Parse arguments ──
while [[ $# -gt 0 ]]; do
    case $1 in
        --target)   TARGET="$2"; shift 2 ;;
        --variant)  VARIANT="$2"; shift 2 ;;
        --arch)     ARCH="$2"; shift 2 ;;
        --format)   FORMAT="$2"; shift 2 ;;
        --output)   OUTPUT="$2"; shift 2 ;;
        --flake-dir) FLAKE_DIR="$2"; shift 2 ;;
        --yes)      NO_CONFIRM="yes"; shift ;;
        --list)     list_targets; exit 0 ;;
        --help|-h)  usage; exit 0 ;;
        *)          echo -e "${RED}Unknown option: $1${NC}"; usage; exit 1 ;;
    esac
done

# ── Find flake ──
if [[ -z "$FLAKE_DIR" ]]; then
    FLAKE_DIR=$(find_flake_dir)
fi

if [[ -z "$FLAKE_DIR" ]] || [[ ! -f "${FLAKE_DIR}/flake.nix" ]]; then
    echo -e "${RED}ERROR: Cannot find flake.nix. Use --flake-dir to specify.${NC}"
    exit 1
fi

# ── Auto-detect architecture ──
if [[ -z "$ARCH" ]]; then
    if [[ "$VARIANT" == "phone" ]]; then
        ARCH="aarch64"
    else
        ARCH=$(uname -m)
        [[ "$ARCH" == "x86_64" ]] && ARCH="x86_64"
        [[ "$ARCH" == "aarch64" ]] && ARCH="aarch64"
    fi
fi

# ── Determine Nix build target ──
determine_target() {
    # If explicit format given, use it
    if [[ -n "$FORMAT" ]]; then
        case "$FORMAT" in
            iso)     echo "iso-${VARIANT}" ;;
            raw)     echo "raw-${VARIANT}" ;;
            sd)
                if [[ "$VARIANT" == "phone" ]]; then
                    echo "sd-phone"
                else
                    echo "sd-${VARIANT}-arm"
                fi
                ;;
            qcow2)   echo "qcow2-${VARIANT}" ;;
            vmware)   echo "vmware-${VARIANT}" ;;
            vbox)     echo "vbox-${VARIANT}" ;;
            docker)   echo "docker-${VARIANT}" ;;
            amazon)   echo "amazon-${VARIANT}" ;;
            gce)      echo "gce-${VARIANT}" ;;
            azure)    echo "azure-${VARIANT}" ;;
            *)        echo ""; return 1 ;;
        esac
        return 0
    fi

    # Auto-detect from target device
    if [[ -n "$TARGET" ]]; then
        case "$TARGET" in
            /dev/mmcblk*)
                # SD card → RPi or phone
                if [[ "$VARIANT" == "phone" ]]; then
                    echo "sd-phone"
                else
                    echo "sd-${VARIANT}-arm"
                fi
                ;;
            /dev/sd*|/dev/nvme*)
                # USB/SSD/NVMe → raw disk image
                echo "raw-${VARIANT}"
                ;;
            *)
                echo "raw-${VARIANT}"
                ;;
        esac
        return 0
    fi

    # No target, no format → default to ISO
    echo "iso-${VARIANT}"
}

NIX_TARGET=$(determine_target)
if [[ -z "$NIX_TARGET" ]]; then
    echo -e "${RED}ERROR: Unknown format '${FORMAT}'${NC}"
    echo "  Supported: iso, raw, sd, qcow2, vmware, vbox, docker, amazon, gce, azure"
    exit 1
fi

# ── Build ──
echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  HART OS Flash Tool${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""
echo -e "  Variant:    ${GREEN}${VARIANT}${NC}"
echo -e "  Arch:       ${ARCH}"
echo -e "  Target:     ${TARGET:-"(file output)"}"
echo -e "  Format:     ${NIX_TARGET}"
echo -e "  Flake:      ${FLAKE_DIR}"
echo ""

echo -e "${CYAN}  Building HART OS image...${NC}"
echo ""

BUILD_OUTPUT=$(nix build "${FLAKE_DIR}#${NIX_TARGET}" --no-link --print-out-paths 2>&1)
BUILD_EXIT=$?

if [[ $BUILD_EXIT -ne 0 ]]; then
    echo -e "${RED}ERROR: Build failed:${NC}"
    echo "$BUILD_OUTPUT"
    exit 1
fi

IMAGE_DIR="$BUILD_OUTPUT"
echo -e "  ${GREEN}Build complete!${NC}"
echo -e "  Output: ${IMAGE_DIR}"
echo ""

# ── Find the actual image file ──
IMAGE_FILE=""
for ext in iso img raw qcow2 vmdk vdi vhd tar.xz tar.gz; do
    found=$(find "$IMAGE_DIR" -name "*.${ext}" 2>/dev/null | head -1)
    if [[ -n "$found" ]]; then
        IMAGE_FILE="$found"
        break
    fi
done

if [[ -z "$IMAGE_FILE" ]]; then
    echo -e "${YELLOW}  Image built at: ${IMAGE_DIR}${NC}"
    echo "  (No single image file found — check directory contents)"
    ls -lh "$IMAGE_DIR"/ 2>/dev/null || ls -lh "$IMAGE_DIR"
    exit 0
fi

IMAGE_SIZE=$(du -sh "$IMAGE_FILE" | cut -f1)
echo -e "  Image: $(basename "$IMAGE_FILE") (${IMAGE_SIZE})"

# ── Copy to output file (if --output specified, no device) ──
if [[ -n "$OUTPUT" ]] && [[ -z "$TARGET" ]]; then
    echo ""
    echo -e "  ${CYAN}Copying to ${OUTPUT}...${NC}"
    cp "$IMAGE_FILE" "$OUTPUT"
    echo -e "  ${GREEN}Done! Image saved to: ${OUTPUT}${NC}"
    exit 0
fi

# ── Flash to device ──
if [[ -n "$TARGET" ]]; then
    # Safety check
    if [[ ! -b "$TARGET" ]]; then
        echo -e "${RED}ERROR: ${TARGET} is not a block device${NC}"
        exit 1
    fi

    # Show device info
    echo ""
    echo -e "${YELLOW}  WARNING: This will ERASE ALL DATA on ${TARGET}${NC}"
    lsblk "$TARGET" 2>/dev/null || true
    echo ""

    if [[ "$NO_CONFIRM" != "yes" ]]; then
        read -p "  Type 'yes' to proceed: " CONFIRM
        if [[ "$CONFIRM" != "yes" ]]; then
            echo "  Aborted."
            exit 1
        fi
    fi

    echo ""
    echo -e "  ${CYAN}Flashing to ${TARGET}...${NC}"
    echo ""

    # Unmount any mounted partitions
    umount "${TARGET}"* 2>/dev/null || true

    # Flash with progress
    dd if="$IMAGE_FILE" of="$TARGET" bs=4M status=progress conv=fsync

    # Sync
    sync

    echo ""
    echo -e "  ${GREEN}============================================================${NC}"
    echo -e "  ${GREEN}  HART OS flashed successfully to ${TARGET}${NC}"
    echo -e "  ${GREEN}============================================================${NC}"
    echo ""
    echo "  Remove the device and boot from it."
    echo "  Default login: hart-admin / hart"
    echo ""
else
    # No target device — just report the built image
    echo ""
    echo -e "  ${GREEN}Image ready: ${IMAGE_FILE}${NC}"
    echo ""
    echo "  To flash to a device:"
    echo "    sudo dd if=${IMAGE_FILE} of=/dev/sdX bs=4M status=progress conv=fsync"
    echo ""
fi
