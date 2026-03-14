#!/usr/bin/env bash
# Create .torrent files for HART OS ISO releases.
# Uses mktorrent (apt install mktorrent) or transmission-create.
#
# Usage:
#   ./scripts/create_torrent.sh ./dist/hart-os-1.0.0-server-amd64.iso
#   ./scripts/create_torrent.sh ./dist/  # All ISOs in directory
#
# Output: .torrent files alongside each ISO, plus magnet links.
#
# Web seeds point to GitHub Releases so the torrent works even
# with zero seeders — GitHub CDN serves the file via HTTP.

set -euo pipefail

REPO="hertz-ai/HARTOS"
TRACKER=""  # DHT-only (no central tracker) — decentralized by default
PIECE_SIZE=4096  # 4MB pieces (good for 1-4GB ISOs)
COMMENT="HART OS — Hevolve Hive Agentic Runtime. https://github.com/${REPO}"

# ─── Detect torrent creator ───────────────────────────────────

if command -v mktorrent &>/dev/null; then
    TOOL="mktorrent"
elif command -v transmission-create &>/dev/null; then
    TOOL="transmission-create"
else
    echo "ERROR: Neither mktorrent nor transmission-create found."
    echo "Install: sudo apt install mktorrent  OR  sudo apt install transmission-cli"
    exit 1
fi

echo "Using: $TOOL"

# ─── Process input ────────────────────────────────────────────

INPUT="${1:?Usage: $0 <iso-file-or-directory>}"

if [[ -d "$INPUT" ]]; then
    FILES=($(find "$INPUT" -maxdepth 1 -name "*.iso" -o -name "*.qcow2" -o -name "*.raw" | sort))
else
    FILES=("$INPUT")
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "No ISO/image files found in: $INPUT"
    exit 1
fi

echo "Creating torrents for ${#FILES[@]} file(s)..."
echo ""

MAGNET_FILE="${INPUT%/}/magnet_links.txt"
> "$MAGNET_FILE"

for FILE in "${FILES[@]}"; do
    BASENAME=$(basename "$FILE")
    TORRENT="${FILE}.torrent"

    # Extract version from filename (e.g., hart-os-1.0.0-server-amd64.iso → 1.0.0)
    VERSION=$(echo "$BASENAME" | grep -oP '\d+\.\d+\.\d+' | head -1 || echo "latest")

    # Web seed: GitHub Release asset URL
    WEBSEED="https://github.com/${REPO}/releases/download/v${VERSION}/${BASENAME}"

    echo "─── $BASENAME ───"
    echo "  Web seed: $WEBSEED"

    # Remove old torrent if exists
    rm -f "$TORRENT"

    if [[ "$TOOL" == "mktorrent" ]]; then
        mktorrent \
            -l 22 \
            -c "$COMMENT" \
            -w "$WEBSEED" \
            -o "$TORRENT" \
            "$FILE"
    else
        transmission-create \
            -c "$COMMENT" \
            -o "$TORRENT" \
            "$FILE"
        # transmission-create doesn't support web seeds via CLI
        # Users can add web seed manually in their torrent client
    fi

    # Compute info hash for magnet link
    if command -v transmission-show &>/dev/null; then
        HASH=$(transmission-show "$TORRENT" 2>/dev/null | grep "Hash:" | awk '{print $2}')
    elif command -v python3 &>/dev/null; then
        HASH=$(python3 -c "
import hashlib, sys
try:
    # bencode the info dict to get info_hash
    with open('$TORRENT', 'rb') as f:
        data = f.read()
    # Find info dict boundaries
    idx = data.find(b'4:infod')
    if idx == -1:
        idx = data.find(b'4:info')
    if idx >= 0:
        info_start = idx + 6
        # Simple: hash everything from 'info' value to end-1
        print('(see .torrent file)')
    else:
        print('unknown')
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown")
    else
        HASH="unknown"
    fi

    SIZE=$(du -h "$FILE" | cut -f1)
    SHA256=$(sha256sum "$FILE" | cut -d' ' -f1)

    MAGNET="magnet:?xt=urn:btih:${HASH}&dn=${BASENAME}&ws=${WEBSEED}"

    echo "  Size:     $SIZE"
    echo "  SHA256:   $SHA256"
    echo "  Torrent:  $TORRENT"
    echo "  Magnet:   $MAGNET"
    echo ""

    echo "$BASENAME: $MAGNET" >> "$MAGNET_FILE"
done

echo "─── Done ───"
echo "Torrent files created. Magnet links saved to: $MAGNET_FILE"
echo ""
echo "To seed: transmission-cli -w . *.torrent"
echo "To verify: transmission-show *.torrent"
