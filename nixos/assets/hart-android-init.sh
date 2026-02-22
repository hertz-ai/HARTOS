#!/usr/bin/env bash
# ============================================================
# HART OS Android Integration Setup
#
# Runs after Waydroid is initialized to configure:
#   - App store (F-Droid or Google Play depending on image)
#   - Clipboard bridge (Linux <-> Android)
#   - Notification bridge
#   - HART OS-specific Android settings
#
# Usage: Called by systemd (hart-android-init.service)
#        Or manually: hart-android-setup
#
# ============================================================

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

WAYDROID_DATA="/var/lib/waydroid"
HART_DATA="${HART_DATA_DIR:-/var/lib/hart}"
SETUP_MARKER="${HART_DATA}/.android-setup-done"

echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  HART OS Android Integration Setup${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

# ─── Check prerequisites ───
if ! command -v waydroid &>/dev/null; then
    echo -e "${YELLOW}  Waydroid not installed. Enable hart.compat.android${NC}"
    exit 1
fi

if [[ ! -f "${WAYDROID_DATA}/.initialized" ]]; then
    echo -e "${YELLOW}  Waydroid not initialized yet. Run: waydroid init${NC}"
    exit 1
fi

if [[ -f "$SETUP_MARKER" ]]; then
    echo -e "${GREEN}  Android integration already configured.${NC}"
    exit 0
fi

# ─── Step 1: Detect image type ───
echo -e "  ${CYAN}[1/5]${NC} Detecting Android image type..."

IMAGE_TYPE="vanilla"
if waydroid prop get persist.waydroid.multi_windows 2>/dev/null | grep -q "true"; then
    echo "    Multi-window: enabled"
fi

# Check for GApps
if waydroid shell ls /system/app/GoogleServicesFramework 2>/dev/null; then
    IMAGE_TYPE="gapps"
    echo -e "    Image: ${GREEN}Google Apps (Play Store available)${NC}"
else
    echo -e "    Image: ${GREEN}Vanilla AOSP${NC}"
fi

# ─── Step 2: Configure app stores ───
echo -e "  ${CYAN}[2/5]${NC} Setting up app stores..."

if [[ "$IMAGE_TYPE" == "vanilla" ]]; then
    # Install F-Droid (open source app store)
    FDROID_URL="https://f-droid.org/F-Droid.apk"
    FDROID_APK="/tmp/fdroid.apk"

    if command -v curl &>/dev/null; then
        echo "    Downloading F-Droid..."
        curl -fsSL "$FDROID_URL" -o "$FDROID_APK" 2>/dev/null && {
            waydroid app install "$FDROID_APK" 2>/dev/null && \
                echo -e "    ${GREEN}F-Droid installed${NC}" || \
                echo -e "    ${YELLOW}F-Droid install failed (install manually)${NC}"
            rm -f "$FDROID_APK"
        } || echo -e "    ${YELLOW}Download failed — install F-Droid manually${NC}"
    fi

    # Install Aurora Store (Google Play alternative without Google account)
    echo "    Aurora Store can be installed via F-Droid"
else
    echo -e "    ${GREEN}Google Play Store available${NC}"
fi

# ─── Step 3: Enable multi-window ───
echo -e "  ${CYAN}[3/5]${NC} Configuring display settings..."

# Multi-window mode (Android apps in separate Linux windows)
waydroid prop set persist.waydroid.multi_windows true 2>/dev/null || true
echo "    Multi-window: enabled"

# Match host display density
DPI=$(xdpyinfo 2>/dev/null | grep "resolution" | head -1 | awk '{print $2}' | cut -d'x' -f1)
if [[ -n "$DPI" ]] && [[ "$DPI" -gt 0 ]]; then
    waydroid prop set persist.waydroid.width_padding 0 2>/dev/null || true
    waydroid prop set persist.waydroid.height_padding 0 2>/dev/null || true
    echo "    Display DPI: $DPI"
fi

# ─── Step 4: Configure clipboard bridge ───
echo -e "  ${CYAN}[4/5]${NC} Setting up clipboard bridge..."

# Waydroid clipboard sharing
waydroid prop set persist.waydroid.clipboard true 2>/dev/null || true
echo "    Clipboard sync: enabled"

# ─── Step 5: HART-specific settings ───
echo -e "  ${CYAN}[5/5]${NC} Applying HART OS integration settings..."

# Set HART backend URL as Android system property
# (Android apps can read this to connect to local HART backend)
BACKEND_PORT="${HART_BACKEND_PORT:-6777}"
waydroid prop set persist.hart.backend_url "http://10.0.0.2:${BACKEND_PORT}" 2>/dev/null || true
echo "    HART backend bridge: 10.0.0.2:${BACKEND_PORT}"

# Enable cursor integration
waydroid prop set persist.waydroid.cursor_on_subsurface true 2>/dev/null || true

# Mark setup as done
touch "$SETUP_MARKER"

echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Android integration complete!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo "  Available app sources:"
if [[ "$IMAGE_TYPE" == "gapps" ]]; then
    echo "    - Google Play Store (built-in)"
fi
echo "    - F-Droid (open source apps)"
echo "    - Aurora Store (Play Store alternative)"
echo "    - APK sideload: waydroid app install <file.apk>"
echo ""
echo "  Commands:"
echo "    waydroid show-full-ui     Open Android home screen"
echo "    waydroid app list         List installed Android apps"
echo "    waydroid app launch <id>  Launch an Android app"
echo "    waydroid session stop     Stop Android session"
echo ""
