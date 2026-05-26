# Downloads

All builds are automated, signed, and verified. Pick your platform.

## What you're downloading

There are two things, one name per role:

- **Nunba** — the unified client. Same experience on Windows, macOS, Linux,
  Android, web, watch, and inside HART OS itself. One identity, every device.
- **HART OS** — the backend OS + agentic runtime.  The network backbone is a
  Mixture-of-Experts (MoE) of open and closed models orchestrated by an
  agentic hive mind that learns and infers simultaneously — not a static
  inference endpoint.  Runs on your laptop (flat), your LAN (regional), or
  in the cloud (central) and federates over PeerLink across deployments.

You only need Nunba on each device you use; HART OS is the substrate Nunba
talks to (locally bundled or remote).

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

### Rolling latest (rebuilt on every push to `main`)

| Variant | Best for | Download |
|---------|----------|----------|
| **Server** | Headless servers, Raspberry Pi, IoT hubs | [ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso) · [torrent](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso.torrent) · [sha256](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso.sha256) |
| **Desktop** | Workstations, dev machines (GNOME desktop) | [ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso) · [torrent](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso.torrent) · [sha256](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso.sha256) |
| **Desktop** | Workstations, dev machines (GNOME desktop) | [ISO](https://docs.hevolve.ai/binaries/hart-os-1.0.0-desktop-x86_64-linux.iso) · [torrent](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso.torrent) · [sha256](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso.sha256) · [parts on GitHub Releases](https://github.com/hertz-ai/HARTOS/releases/latest) (4 × 1.9 GiB + reassemble.sh, for users who can't reach docs.hevolve.ai) |
| **Edge** | Minimal observer nodes, embedded | [ISO](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso) · [torrent](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso.torrent) · [sha256](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso.sha256) |

Each push to `main` publishes a new `nightly-{SHA}` release tagged as "latest" — the links above always resolve to the newest build. Older nightlies are auto-pruned (most recent 3 kept). Verify with the companion `.sha256`. For signed stable ISOs browse the [release archive](https://github.com/hertz-ai/HARTOS/releases).

Torrents are web-seeded via GitHub CDN.

**Desktop ISO is larger than GitHub's 2 GB per-asset limit** and therefore ships split into `…iso.part-00`, `…iso.part-01`, … Download every `part-*` alongside the `.sha256` and a companion `…iso.reassemble.sh`, then run the script (or `cat *.part-* > …iso`) to stitch them and verify the checksum.

## Nunba — Native Agentic Client

Nunba is the HART agentic client — same experience on HART OS and as a native app on Windows, macOS, or Linux. Think of it as a cross-platform agentic shell: chat, agents, communities, and compute controls, backed by the HART runtime running locally or remotely.
## Nunba — Unified Agentic Client (Desktop)

Nunba is the unified HART client — Windows, macOS, Linux desktop builds
here; Android + web + iOS sections below; on HART OS itself Nunba is
pre-installed.  One client identity across every device, backed by the
HART runtime (the MoE-of-models hive mind) running locally or remotely.

| Target | Delivery | Notes |
|--------|----------|-------|
| **HART OS (native)** | Pre-installed | Systemd user service `hart-nunba` starts on boot (port 5000) and renders inside the LiquidUI Glass Shell. Enable via `hart.nunba.enable = true` in the desktop/phone configs (default). Registered in the HART AppRegistry (group: System) so launchers and the agentic runtime dispatch to it. |
| **Windows** | [Nunba Installer](https://github.com/hertz-ai/Nunba/releases/latest) _(built by the [Nunba repo's `Build & Sign Installers`](https://github.com/hertz-ai/Nunba/actions) workflow)_ | Windows 10/11, x64. Azure Trusted Signing. |
| **macOS** | [Nunba.dmg](https://github.com/hertz-ai/Nunba/releases/latest) | macOS 13+ (Apple Silicon native). Notarized. |
| **Linux (AppImage)** | [AppImage](https://github.com/hertz-ai/Nunba/releases/latest) | Any distro, x86_64. `chmod +x` and run. |
| **Linux (.deb)** | [.deb package](https://github.com/hertz-ai/Nunba/releases/latest) | Debian/Ubuntu. `sudo dpkg -i nunba_*.deb` |

On HART OS Nunba binds to the local `hart_intelligence_entry.py` runtime (port 6777). On other OSes it bundles the same runtime or points at a remote HART node via `NUNBA_BACKEND_URL`.

## Hevolve Droid (Android)

Your phone becomes a remote to your private AI mesh.
| **Windows** | [Nunba_Setup.exe](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.exe) _(built by the [Nunba repo's `Build & Sign Installers`](https://github.com/hertz-ai/Nunba/actions) workflow)_ | Windows 10/11, x64. Azure Trusted Signing. |
| **macOS** | [Nunba_Setup.dmg](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.dmg) | macOS 13+ (Apple Silicon native). Notarized. |
| **Linux (AppImage)** | [Nunba-x86_64.AppImage](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba-x86_64.AppImage) | Any distro, x86_64. `chmod +x` and run. |
| **Linux (.deb)** | [.deb package (release page)](https://github.com/hertz-ai/Nunba/releases/latest) | Debian/Ubuntu. `sudo dpkg -i nunba_*.deb`. Filename is version-pinned (`nunba_<version>_amd64.deb`); browse the release page to grab the latest. |

On HART OS Nunba binds to the local `hart_intelligence_entry.py` runtime (port 6777). On other OSes it bundles the same runtime or points at a remote HART node via `NUNBA_BACKEND_URL`.

## Nunba (Android)

Same Nunba client, on your phone — your phone becomes a remote to your
private AI mesh.

| Platform | Download |
|----------|----------|
| **Android (Play Store)** | [Google Play](https://play.google.com/store/apps/details?id=com.hertzai.hevolve) |
| **Android (APK, team only)** | [Direct APK](https://github.com/hertz-ai/Hevolve_React_Native/releases/latest/download/Hevolve.apk) — _`hertz-ai/Hevolve_React_Native` is a private repo; the asset link requires a GitHub session. Public users should install via Play Store above._ |

## Hevolve Web

Access from any browser — no install needed.
> The underlying Play Store package ID (`com.hertzai.hevolve`), the GitHub
> repo (`Hevolve_React_Native`), and the APK filename (`Hevolve.apk`) still
> reflect the project's prior internal name.  User-facing branding is
> "Nunba" everywhere; the asset-rename across Play Store metadata + repo +
> CI artifacts is a coordinated rollout that lands separately so existing
> install URLs don't break for current users.

## Nunba (Web)

Same Nunba client, in the browser — no install needed.

| Platform | Link |
|----------|------|
| **Web App** | [hevolve.ai](https://hevolve.ai/) |

> The web client is hosted at `hevolve.ai` for legacy SEO continuity; the
> in-app branding is "Nunba".

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
| **HART OS** (backend) | [GitHub Releases](https://github.com/hertz-ai/HARTOS/releases) |
| **Nunba** (desktop / web client) | [GitHub Releases](https://github.com/hertz-ai/Nunba/releases) |
| **Nunba (Android)** | [GitHub Releases](https://github.com/hertz-ai/Hevolve_React_Native/releases) — repo name still reflects the prior internal name |
| **Nunba (iOS)** | TestFlight / App Store ([source](https://github.com/hertz-ai/nunba-ios)) |
