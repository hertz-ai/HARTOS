#!/usr/bin/env bash
# Build HART OS installer for all platforms
# Produces: hevolve-install.exe, hevolve-install-macos, hevolve-install-linux
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p dist

echo "Building HART OS installer..."

GOOS=windows GOARCH=amd64 go build -ldflags="-s -w" -o dist/hevolve-install.exe .
echo "  Windows: dist/hevolve-install.exe"

GOOS=darwin GOARCH=arm64 go build -ldflags="-s -w" -o dist/hevolve-install-macos .
echo "  macOS:   dist/hevolve-install-macos"

GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o dist/hevolve-install-linux .
echo "  Linux:   dist/hevolve-install-linux"

ls -lh dist/
echo "Done. Upload to GitHub release or docs.hevolve.ai/downloads/"
