# Downloads

All builds are automated, signed, and verified. Pick your platform.

## HART OS (Full Operating System)

Boot from USB/SD card or run in a VM. Includes everything.

| Variant | Best for | Download |
|---------|----------|----------|
| **Server** | Headless servers, Raspberry Pi, IoT hubs | [Latest ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso) |
| **Desktop** | Workstations, dev machines (GNOME desktop) | [Latest ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso) |
| **Edge** | Minimal observer nodes, embedded | [Latest ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso) |

Torrents available alongside each ISO (web-seeded via GitHub CDN).

## Nunba (Companion App)

Runs on your existing OS. Connects to HART OS backend.

| Platform | Download |
|----------|----------|
| **Windows** | [Nunba Installer](https://github.com/hertz-ai/HARTOS/releases/latest) |
| **macOS** | [Nunba.dmg](https://github.com/hertz-ai/HARTOS/releases/latest) |
| **Linux** | [AppImage](https://github.com/hertz-ai/HARTOS/releases/latest) |

## Hevolve Droid (Android)

Your phone becomes a remote to your private AI mesh.

| Platform | Download |
|----------|----------|
| **Android** | [Hevolve APK](https://github.com/hertz-ai/HARTOS/releases/latest) |

## pip install (Backend only)

```bash
pip install -r requirements.txt
python hart_intelligence_entry.py
```

## Docker

```bash
docker compose -f deploy/cloud/docker-compose.yml up
```

## Verify Downloads

Every release is Ed25519 signed by the master key.

```bash
# Check SHA-256
sha256sum -c hart-os-*.sha256

# Verify release signature
python -c "
from security.master_key import verify_release_manifest
import json
m = json.load(open('release_manifest.json'))
print('VALID' if verify_release_manifest(m) else 'INVALID')
"
```

## All Releases

[View all releases on GitHub](https://github.com/hertz-ai/HARTOS/releases)
