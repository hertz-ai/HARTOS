# Downloads

All builds are automated, signed, and verified. Pick your platform.

## Quick Install (Any Platform)

Download the installer for your OS — it auto-detects your platform and installs the right thing.

| Platform | Download | Size |
|----------|----------|------|
| **Windows** | [hevolve-install.exe](https://github.com/hertz-ai/HARTOS/releases/latest/download/hevolve-install.exe) | ~5 MB |
| **macOS** | [hevolve-install-macos](https://github.com/hertz-ai/HARTOS/releases/latest/download/hevolve-install-macos) | ~5 MB |
| **Linux** | [hevolve-install-linux](https://github.com/hertz-ai/HARTOS/releases/latest/download/hevolve-install-linux) | ~5 MB |

Double-click (Windows) or `chmod +x && ./hevolve-install-*` (macOS/Linux). Choose: Nunba app, HART OS ISO, or pip install.

---

## HART OS (Full Operating System)

Boot from USB/SD card or run in a VM. Includes everything.

| Variant | Best for | Download |
|---------|----------|----------|
| **Server** | Headless servers, Raspberry Pi, IoT hubs | [Latest ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso) |
| **Desktop** | Workstations, dev machines (GNOME desktop) | [Latest ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso) |
| **Edge** | Minimal observer nodes, embedded | [Latest ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso) |

Torrents available alongside each ISO (web-seeded via GitHub CDN).

## Nunba (Companion App)

Runs on your existing OS. Connects to HART OS backend or runs standalone with local AI.

| Platform | Download | Notes |
|----------|----------|-------|
| **Windows** | [Nunba Installer](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.exe) | Windows 10/11, x64. Azure Trusted Signing. |
| **macOS** | [Nunba.dmg](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.dmg) | macOS 13+ (Apple Silicon native). Notarized. |
| **Linux** | [AppImage](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba-x86_64.AppImage) | Any distro, x86_64. `chmod +x` and run. |
| **Linux (.deb)** | [.deb package](https://github.com/hertz-ai/Nunba/releases/latest) | Debian/Ubuntu. `sudo dpkg -i nunba_*.deb` |

## Hevolve Droid (Android)

Your phone becomes a remote to your private AI mesh.

| Platform | Download |
|----------|----------|
| **Android (Play Store)** | [Google Play](https://play.google.com/store/apps/details?id=com.hertzai.hevolve) |
| **Android (APK)** | [Direct APK](https://github.com/hertz-ai/Hevolve_React_Native/releases/latest/download/Hevolve.apk) |

## Hevolve Web

Access from any browser — no install needed.

| Platform | Link |
|----------|------|
| **Web App** | [hevolve.ai](https://hevolve.ai/) |

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

| Project | Releases |
|---------|----------|
| **HART OS** | [GitHub Releases](https://github.com/hertz-ai/HARTOS/releases) |
| **Nunba** | [GitHub Releases](https://github.com/hertz-ai/Nunba/releases) |
| **Hevolve Droid** | [GitHub Releases](https://github.com/hertz-ai/Hevolve_React_Native/releases) |
