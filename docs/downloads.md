# Download

Pick your device and grab it. Every build is automated, Ed25519-signed, and checksum-verified.

---

## Get the app — Nunba

The client you actually use. Same experience on every device, one identity. **Most people only need this.**

| Your device | Download |
|----------|----------|
| **Windows** | [**Nunba_Setup.exe**](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.exe) |
| **macOS** | [**Nunba_Setup.dmg**](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba_Setup.dmg) |
| **Linux** | [**AppImage**](https://github.com/hertz-ai/Nunba/releases/latest/download/Nunba-x86_64.AppImage) · [.deb](https://github.com/hertz-ai/Nunba/releases/latest) |
| **Android** | [**Google Play**](https://play.google.com/store/apps/details?id=com.hertzai.hevolve) |
| **Web** | [**hevolve.ai**](https://hevolve.ai/) — no install |
| **iOS** | [TestFlight / App Store](https://github.com/hertz-ai/nunba-ios) |

!!! tip "Not sure which file? Use the universal installer"
    It auto-detects your OS and installs the right thing:
    [Windows](https://github.com/hertz-ai/HARTOS/releases/latest/download/hevolve-install.exe) ·
    [macOS](https://github.com/hertz-ai/HARTOS/releases/latest/download/hevolve-install-macos) ·
    [Linux](https://github.com/hertz-ai/HARTOS/releases/latest/download/hevolve-install-linux)

---

## Get the full OS — HART OS

Boot from a USB stick, SD card, or VM. The complete agentic runtime + hive. Rebuilt on every push to `main`.

| Variant | Best for | Download |
|---------|----------|----------|
| **Desktop** | Workstations, dev machines | [**ISO**](https://docs.hevolve.ai/binaries/hart-os-1.0.0-desktop-x86_64-linux.iso) · [torrent](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso.torrent) · [sha256](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-desktop-x86_64-linux.iso.sha256) |
| **Server** | Headless, Raspberry Pi, IoT hubs | [**ISO**](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso) · [torrent](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso.torrent) · [sha256](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-server-x86_64-linux.iso.sha256) |
| **Edge** | Minimal observer / embedded nodes | [**ISO**](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso) · [torrent](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso.torrent) · [sha256](https://github.com/hertz-ai/HARTOS/releases/latest/download/hart-os-1.0.0-edge-x86_64-linux.iso.sha256) |

!!! tip "Easiest way to flash"
    `python scripts/hart_usb_flasher.py --gui` — fetches the ISO, writes it to the stick, and verifies the boot signature in one step.

---

??? question "Nunba vs HART OS — which do I need?"
    - **Nunba** is the client you use — Windows, macOS, Linux, Android, web, and inside HART OS itself. One identity, every device. Most people only need this.
    - **HART OS** is the backend OS + agentic runtime Nunba talks to: a Mixture-of-Experts hive mind that learns and infers at the same time. Runs on your laptop (flat), your LAN (regional), or the cloud (central), and federates over PeerLink.

    On HART OS itself, Nunba is pre-installed (systemd user service `hart-nunba`, port 5000, rendered in the LiquidUI Glass Shell). Everywhere else, Nunba bundles the runtime locally or points at a remote node via `NUNBA_BACKEND_URL`.

??? note "Flash HART OS to a USB stick"
    The desktop ISO is larger than GitHub's 2 GB per-asset limit, so it ships split into `…iso.part-00`, `…part-01`, … with a companion `…reassemble.sh`. The flasher handles all of that for you:

    ```bash
    # 1. List removable disks to find your device id (e.g. #1)
    python scripts/hart_usb_flasher.py --list

    # 2. Flash the latest desktop ISO to disk #1  (DESTRUCTIVE — wipes the stick)
    python scripts/hart_usb_flasher.py --tag <nightly-tag> --device 1 --mode download --yes
    ```

    - `--tag` is the release tag (the newest is marked **Latest** on the [releases page](https://github.com/hertz-ai/HARTOS/releases)).
    - Prefer a picker? `--gui`. Tight on scratch disk? `--mode stream`. Only removable disks are offered; a system disk needs `--allow-system --yes`.
    - **Windows:** if you hit `exclusive open failed (err 32)`, eject/re-insert the stick so Windows releases the lock, then re-run.

    Can't reach `docs.hevolve.ai` for the desktop ISO? Grab the split `part-*` files + `.sha256` + `reassemble.sh` from [GitHub Releases](https://github.com/hertz-ai/HARTOS/releases) and run `bash …reassemble.sh` (or `cat *.part-* > …iso`), then verify the checksum.

??? note "Backend only — pip or Docker"
    ```bash
    # pip
    pip install -r requirements.txt
    python hart_intelligence_entry.py

    # Docker
    docker compose -f deploy/cloud/docker-compose.yml up
    ```

??? note "Verify a download"
    Every release is Ed25519-signed by the master key.

    ```bash
    # Check SHA-256
    sha256sum -c hart-os-*.sha256

    # Verify the release signature
    python -c "
    from security.master_key import verify_release_manifest
    import json
    m = json.load(open('release_manifest.json'))
    print('VALID' if verify_release_manifest(m) else 'INVALID')
    "
    ```

---

## All releases

| Project | Releases |
|---------|----------|
| **HART OS** (backend / ISOs) | [GitHub Releases](https://github.com/hertz-ai/HARTOS/releases) |
| **Nunba** (desktop / web) | [GitHub Releases](https://github.com/hertz-ai/Nunba/releases) |
| **Nunba** (Android) | [GitHub Releases](https://github.com/hertz-ai/Hevolve_React_Native/releases) |
| **Nunba** (iOS) | [TestFlight / App Store](https://github.com/hertz-ai/nunba-ios) |

Each push to `main` publishes a `nightly-{SHA}` release tagged **latest** (the links above always resolve to the newest build; the most recent 3 nightlies are kept). For signed stable builds, browse the [release archive](https://github.com/hertz-ai/HARTOS/releases).
