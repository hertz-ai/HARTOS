#!/usr/bin/env bash
# ============================================================
# HART OS Bundle Builder
# Creates a signed tar.gz bundle for remote deployment.
#
# Usage:
#   bash build_bundle.sh [--version X.Y.Z]
#
# Output:
#   hart-os-{version}-{arch}.tar.gz
#   hart-os-{version}-{arch}.tar.gz.sha256
# ============================================================

set -euo pipefail

VERSION="${1:---version}"
if [[ "$VERSION" == "--version" ]]; then
    VERSION="${2:-1.0.0}"
fi

ARCH=$(uname -m)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
BUILD_DIR="/tmp/hart-os-build-$$"
BUNDLE_NAME="hart-os-${VERSION}-${ARCH}"

echo "[HART OS] Building bundle: $BUNDLE_NAME"

# Clean previous builds
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/$BUNDLE_NAME"

# Copy application code (excluding dev artifacts)
rsync -a \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='venv*' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='tests/' \
    --exclude='agent_data/*.db' \
    --exclude='agent_data/baselines/' \
    --exclude='agent_data/lightning_traces/' \
    --exclude='.env' \
    --exclude='*.egg-info' \
    --exclude='.idea/' \
    --exclude='.pycharm_plugin/' \
    --exclude='autogen-*/' \
    --exclude='docs/' \
    --exclude='regression_*.txt' \
    --exclude='test_*.txt' \
    --exclude='*.bat' \
    "$REPO_DIR/" "$BUILD_DIR/$BUNDLE_NAME/"

# Ensure deploy files are included
cp "$REPO_DIR/deploy/linux/install.sh" "$BUILD_DIR/$BUNDLE_NAME/deploy/linux/"
cp "$REPO_DIR/requirements.txt" "$BUILD_DIR/$BUNDLE_NAME/"

# Create manifest
echo "[HART OS] Computing manifest..."
cd "$BUILD_DIR"
find "$BUNDLE_NAME" -type f | sort | xargs sha256sum > "${BUNDLE_NAME}.manifest"
mv "${BUNDLE_NAME}.manifest" "$BUILD_DIR/$BUNDLE_NAME/"

# Create tarball
echo "[HART OS] Creating tarball..."
tar czf "${BUNDLE_NAME}.tar.gz" "$BUNDLE_NAME/"

# Compute checksum
sha256sum "${BUNDLE_NAME}.tar.gz" > "${BUNDLE_NAME}.tar.gz.sha256"

# Move to repo output directory
mkdir -p "$REPO_DIR/dist"
mv "${BUNDLE_NAME}.tar.gz" "$REPO_DIR/dist/"
mv "${BUNDLE_NAME}.tar.gz.sha256" "$REPO_DIR/dist/"

# Clean up
rm -rf "$BUILD_DIR"

echo "[HART OS] Bundle created:"
echo "  $REPO_DIR/dist/${BUNDLE_NAME}.tar.gz"
echo "  $REPO_DIR/dist/${BUNDLE_NAME}.tar.gz.sha256"
BUNDLE_SIZE=$(du -h "$REPO_DIR/dist/${BUNDLE_NAME}.tar.gz" | cut -f1)
echo "  Size: $BUNDLE_SIZE"
