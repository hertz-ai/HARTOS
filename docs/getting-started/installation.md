# HART OS Installation Guide

Complete guide to installing and deploying HART OS on any platform.

---

## Quick Start (3 Minutes)

The fastest path to a running HART OS instance:

```bash
# 1. Python 3.10 virtual environment
python3.10 -m venv venv310
source venv310/bin/activate          # Linux/macOS
# venv310\Scripts\activate.bat       # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure API keys (minimum: one LLM provider)
cp .env.example .env
# Edit .env — set OPENAI_API_KEY or GROQ_API_KEY

# 4. Start
python hart_intelligence_entry.py

# 5. Verify
curl http://localhost:6777/status
```

---

## System Requirements

### Hardware Tiers

HART OS auto-detects hardware at first boot and classifies the node:

| Tier | RAM | CPU | GPU | Services Enabled |
|------|-----|-----|-----|------------------|
| **OBSERVER** | < 4 GB | < 2 cores | -- | Backend + Discovery only |
| **STANDARD** | 4+ GB | 2+ cores | -- | + Agent Daemon |
| **PERFORMANCE** | 8+ GB | 4+ cores | Optional | + Vision + LLM (if models present) |
| **COMPUTE_HOST** | 16+ GB | 8+ cores | 1+ GPU | All services + model hosting |

Minimum: **4 GB RAM, 10 GB disk, Python 3.10**.

### Supported Platforms

| Platform | Architecture | Method |
|----------|-------------|--------|
| Ubuntu 22.04+ / Debian 12+ | x86_64, aarch64 | Automated installer |
| NixOS | x86_64, aarch64 | Flake (ISO, VM, cloud images) |
| Any Linux | x86_64, aarch64 | Docker, manual |
| Windows 10/11 | x86_64 | Manual (Python venv) |
| macOS 12+ | x86_64, arm64 | Manual (Python venv) |
| Raspberry Pi 4/5 | aarch64 | NixOS SD card image |
| PinePhone / PinePhone Pro | aarch64 | NixOS phone variant |

### Network Ports

HART OS uses two port sets depending on deployment mode:

| Service | App Mode (default) | OS Mode (NixOS) | Protocol | Required |
|---------|-------------------|-----------------|----------|----------|
| Backend API | **6777** | **677** | TCP/HTTP | Yes |
| Peer discovery | **6780** | **678** | UDP | Clustering only |
| Vision (MiniCPM) | **9891** | **989** | TCP/HTTP | Optional |
| WebSocket | **5460** | **546** | TCP/WS | Optional |
| Local LLM | **8080** | **808** | TCP/HTTP | Optional |
| Diarization | **8004** | **800** | TCP/HTTP | Optional |
| DLNA stream | **8554** | **855** | TCP/HTTP | Optional |
| Mesh WireGuard | **6795** | **679** | UDP | Clustering only |
| Mesh relay | **6796** | **680** | TCP | Clustering only |

**OS mode** activates automatically on NixOS (`/etc/os-release` contains `ID=hart-os`) or when `HART_OS_MODE=true`. Privileged ports (<1024) are used so that user-space ports (1024-65535) remain available for user applications. Individual ports can be overridden via environment variables regardless of mode (see [Configuration Reference](#configuration-reference)).

---

## Method 1: Linux Automated Installer

The recommended method for Ubuntu/Debian production servers.

```bash
sudo bash deploy/linux/install.sh
```

### What It Does

1. Creates `hart:hart` system user and group
2. Installs to `/opt/hart` with Python 3.10 venv
3. Generates Ed25519 node keypair at `/var/lib/hart/`
4. Detects GPU (NVIDIA, integrated)
5. Installs systemd services (backend, discovery, vision, LLM, agent daemon)
6. Configures UFW firewall rules
7. Installs `hart` CLI to `/usr/local/bin/hart`
8. Classifies hardware tier (OBSERVER → COMPUTE_HOST)

### Options

```bash
sudo bash deploy/linux/install.sh [OPTIONS]

  --dry-run       Check prerequisites only, don't install
  --join-peer URL Auto-join an existing hive after install
  --port N        Override backend port (default: 6777)
  --no-vision     Skip MiniCPM vision service
  --no-llm        Skip llama.cpp local inference
  --from-iso      Called from ISO autoinstall (skip user prompts)
  --uninstall     Remove HART OS completely
```

### Post-Install Configuration

```bash
# Edit environment
sudo nano /etc/hart/hart.env

# Start all services
sudo systemctl start hart.target

# Check status
sudo systemctl status hart.target
hart status

# View logs
journalctl -u hart-backend -f
```

### Directory Layout

```
/opt/hart/              # Application code + venv
/etc/hart/              # Configuration (hart.env)
/var/lib/hart/          # Data (database, keys, models)
/var/log/hart/          # Logs
/usr/local/bin/hart     # CLI symlink
```

### Uninstall

```bash
sudo bash deploy/linux/install.sh --uninstall
# Data directory (/var/lib/hart) is preserved — remove manually if desired
```

---

## Method 2: Docker Compose

### Development

```bash
cd deploy/cloud/

# Copy and edit environment
cp .env.example .env
# Edit .env — set API keys

# Start (backend + PostgreSQL + Redis)
docker-compose up -d

# Verify
curl http://localhost:8000/status
docker-compose logs -f backend
```

### Production

```bash
# Production adds: resource limits, logging, restart policies
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Production overrides:
- Backend: 2 CPU, 4 GB RAM limit, Gunicorn (4 workers)
- PostgreSQL: 1 CPU, 2 GB RAM, 200 connections, 256 MB shared_buffers
- Redis: 0.5 CPU, 768 MB RAM, AOF persistence, LRU eviction
- Logging: JSON-file driver, 100 MB rotation, 5 files max
- Non-root user (`appuser`), read-only filesystem support

### Distributed (3-Node Cluster)

```bash
cd deploy/distributed/
docker-compose -f docker-compose.distributed.yml up -d
```

Starts: 1 central node (port 6777) + 2 worker nodes (6778, 6779) + shared Redis.

### nginx Reverse Proxy (Optional)

```bash
# Add nginx to the compose stack
docker-compose --profile with-nginx up -d
```

Provides: rate limiting (10 req/s), gzip compression, least-connection load balancing.

---

## Method 3: NixOS (Full OS)

HART OS as a complete operating system — declarative, reproducible, atomic.

### Variants

| Variant | Description | GUI |
|---------|-------------|-----|
| `server` | Headless, all AI services, GPU compute | No |
| `desktop` | GNOME desktop, full toolkit, LiquidUI | Yes |
| `edge` | Minimal services, low-resource | No |
| `phone` | PinePhone / PinePhone Pro mobile | Yes |

### Build & Test in a VM (Sandbox)

```bash
cd nixos/

# Boot a complete HART OS VM — zero risk to host
nix run .#vm-server          # headless
nix run .#vm-desktop         # with GUI
nix run .#vm-edge            # minimal
```

Everything runs in QEMU, destroyed on exit.

### Build Installable Images

```bash
# ISO (bootable USB / optical)
nix build .#iso-server
nix build .#iso-desktop

# Raw EFI disk image (dd to SSD/NVMe)
nix build .#raw-server

# SD card (Raspberry Pi)
nix build .#sd-server-arm
nix build .#sd-desktop-arm

# PinePhone
nix build .#sd-phone
```

### Virtual Machine Images

```bash
# VirtualBox (.vdi) — import into VBox
nix build .#vbox-desktop

# VMware (.vmdk) — import into ESXi / Workstation
nix build .#vmware-desktop

# QCOW2 — KVM / Proxmox / libvirt
nix build .#qcow2-server
```

### Cloud Images

```bash
# Amazon AMI
nix build .#amazon-server

# Google Compute Engine
nix build .#gce-server

# Azure VHD
nix build .#azure-server

# Docker (OCI container)
nix build .#docker-server
```

### NixOS Modules Reference

All modules live in `nixos/modules/` and are toggled per-variant:

| Module | Purpose |
|--------|---------|
| `hart-base.nix` | Core setup, user, directories |
| `hart-backend.nix` | Flask/Waitress service (port 6777) |
| `hart-agent.nix` | Agent execution daemon |
| `hart-llm.nix` | Local LLM inference (llama.cpp) |
| `hart-vision.nix` | Vision service (MiniCPM sidecar) |
| `hart-kernel.nix` | Unified kernel: Android/Windows binary support, GPU, Landlock |
| `hart-ai-runtime.nix` | GPU scheduling, world model |
| `hart-compute-mesh.nix` | Distributed compute mesh |
| `hart-liquid-ui.nix` | LiquidUI Glass Shell (desktop only) |
| `hart-app-bridge.nix` | Android/Windows app subsystems |
| `hart-peripheral-bridge.nix` | USB/IP, Bluetooth, Gamepad forwarding |
| `hart-dlna.nix` | DLNA screen casting |
| `hart-sandbox.nix` | Agent sandboxing (cgroups v2 + Landlock) |
| `hart-discovery.nix` | P2P gossip peer discovery |
| `hart-first-boot.nix` | First-boot hardware detection + tier setup |

---

## Method 4: Bootable ISO (Ubuntu-Based)

Build a standalone bootable image from Ubuntu 22.04 LTS:

```bash
# Build ISO (requires live-build)
sudo bash deploy/distro/build-iso.sh --variant desktop

# Output: dist/hart-os-1.0.0-desktop-amd64.iso
# Includes: SHA-256 checksums
```

### Variants

```bash
sudo bash deploy/distro/build-iso.sh --variant server    # headless
sudo bash deploy/distro/build-iso.sh --variant desktop   # GNOME
sudo bash deploy/distro/build-iso.sh --variant edge      # minimal
```

### First Boot

After installing from ISO, HART OS runs a one-time setup (`hart-first-boot.service`):

1. **Generates Ed25519 node keypair** — immutable after creation (`chattr +i`)
2. **Detects hardware** — CPU cores, RAM, GPU (nvidia-smi)
3. **Classifies tier** — OBSERVER / STANDARD / PERFORMANCE / COMPUTE_HOST
4. **Enables/disables services** per tier
5. **Downloads default model** (TinyLlama 1.1B GGUF) for COMPUTE_HOST tier
6. **Runs database migrations**
7. **Generates boot audit report**

### Unattended Install

For automated deployments, use the autoinstall configs:

```
deploy/distro/autoinstall/
  user-data      # Ubuntu Subiquity provisioning
  meta-data
  vendor-data
```

---

## Method 5: Cloud / Remote Server

### Startup Script

```bash
# Linux
bash scripts/start_cloud.sh \
  --api-key "$OPENAI_API_KEY" \
  --model gpt-4.1-mini

# Windows
scripts\start_cloud.bat
```

The cloud script sets: `HEVOLVE_NODE_TIER=central`, `HEVOLVE_ENFORCEMENT_MODE=hard`, `HEVOLVE_DEV_MODE=false`.

### Production Docker

```bash
docker build -f deploy/cloud/Dockerfile.prod -t hart-os:prod .

docker run -d \
  --name hart \
  -p 8000:8000 \
  -e OPENAI_API_KEY=your-key \
  -v hart-data:/var/lib/hart \
  --read-only \
  hart-os:prod
```

Production Dockerfile features:
- Multi-stage build (builder + runtime)
- Python 3.11-slim base
- Non-root user (`appuser`)
- Gunicorn: 4 workers, 2 threads, 120s timeout
- Health check: `GET /health` every 30s
- HevolveAI source protection (compiled + stripped)

---

## Method 6: Windows / macOS (Manual)

```bash
# 1. Install Python 3.10
# Download from python.org or use pyenv/brew

# 2. Create virtual environment
python3.10 -m venv venv310
source venv310/bin/activate          # macOS
# venv310\Scripts\activate.bat       # Windows

# 3. Install
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — set OPENAI_API_KEY or GROQ_API_KEY

# 5. Optional: config.json for additional APIs
cat > config.json << 'EOF'
{
  "OPENAI_API_KEY": "your-key",
  "GROQ_API_KEY": "your-key",
  "GOOGLE_CSE_ID": "",
  "GOOGLE_API_KEY": "",
  "NEWS_API_KEY": "",
  "SERPAPI_API_KEY": ""
}
EOF

# 6. Start
python hart_intelligence_entry.py

# 7. Verify
curl http://localhost:6777/status
```

### Install CLI (Optional)

```bash
pip install -e .
hart status
hart chat "Hello, HART OS"
```

### Install Extras

```bash
pip install -e ".[remote-desktop]"   # mss, websockets, av, pynput
pip install -e ".[telegram]"         # python-telegram-bot
pip install -e ".[discord]"          # discord.py
pip install -e ".[torch]"           # PyTorch + torchvision
pip install -e ".[dev]"             # pytest, black, flake8, mypy
pip install -e ".[all]"             # Everything
```

---

## Configuration Reference

### Environment Variables (.env)

**Core:**

| Variable | Default | Description |
|----------|---------|-------------|
| `HEVOLVE_MASTER_KEY` | -- | Secrets vault encryption key |
| `HEVOLVE_API_KEY` | -- | API authentication key |
| `HEVOLVE_DB_PATH` | `hevolve_database.db` | SQLite database path |
| `HEVOLVE_ENV` | `development` | `development` or `production` |
| `HEVOLVE_DEV_MODE` | `true` | Dev mode (false in production) |
| `HEVOLVE_ENFORCEMENT_MODE` | `off` | `off`, `soft`, `hard` |

**Ports:**

| Variable | Default | Description |
|----------|---------|-------------|
| `HARTOS_BACKEND_PORT` | `6777` | Flask API |
| `HART_DISCOVERY_PORT` | `6780` | Peer gossip (UDP) |
| `HART_VISION_PORT` | `9891` | Vision service |
| `HART_WS_PORT` | `5460` | WebSocket |
| `HART_LLM_PORT` | `8080` | Local LLM |

**LLM Providers (at least one required):**

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key |
| `GROQ_API_KEY` | Groq API key (fast inference) |
| `LANGCHAIN_API_KEY` | LangChain / LangSmith tracing |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI endpoint |
| `AZURE_OPENAI_ENDPOINT` | Azure endpoint URL |

**Search & External APIs (all optional):**

| Variable | Description |
|----------|-------------|
| `GOOGLE_CSE_ID` | Google Custom Search Engine ID |
| `GOOGLE_API_KEY` | Google API key |
| `NEWS_API_KEY` | NewsAPI.org key |
| `SERPAPI_API_KEY` | SerpAPI key |

**Clustering:**

| Variable | Default | Description |
|----------|---------|-------------|
| `HEVOLVE_NODE_TIER` | `flat` | `flat`, `regional`, `central` |
| `HEVOLVE_CENTRAL_URL` | -- | Central node URL |
| `HART_JOIN_PEER` | -- | Peer to auto-join at startup |
| `HART_GOSSIP_INTERVAL` | `60` | Gossip interval (seconds) |
| `HEVOLVE_SEED_PEERS` | -- | Comma-separated seed peer URLs |

**Security:**

| Variable | Default | Description |
|----------|---------|-------------|
| `SOCIAL_SECRET_KEY` | -- | JWT secret (required for social features) |
| `SOCIAL_DB_KEY` | -- | SQLCipher encryption key |
| `TLS_CERT_PATH` | -- | TLS certificate path |
| `TLS_KEY_PATH` | -- | TLS private key path |
| `CORS_ORIGINS` | `http://localhost:3000` | Allowed CORS origins |

**LLM Inference:**

| Variable | Default | Description |
|----------|---------|-------------|
| `HART_LLM_THREADS` | `4` | llama.cpp thread count |
| `HART_LLM_CTX_SIZE` | `4096` | Context window size |
| `HART_LLM_MODEL_PATH` | `/opt/hart/models/default.gguf` | Model file path |

### Critical Pinned Versions

| Package | Version | Reason |
|---------|---------|--------|
| `langchain` | 0.0.230 | Monolithic pre-split API |
| `pydantic` | 1.10.9 | Requires Python 3.10 (incompatible with 3.12+) |
| `chromadb` | 0.3.23 | Vector store compatibility |
| `autogen-agentchat` | 0.2.37 | Multi-agent framework |

---

## Boot, BIOS & Virtualization

### NixOS (Declarative Boot)

NixOS handles boot configuration declaratively:

- **Bootloader**: GRUB 2 or systemd-boot (configured per variant)
- **EFI**: Raw EFI images (`nix build .#raw-server`) partition correctly for UEFI
- **Disk layout**: Managed by NixOS installation (`nixos-install`)
- **Kernel**: `hart-kernel.nix` — unified kernel with extensions for:
  - Android binary support (binder + ashmem kernel modules)
  - Windows binary support (binfmt_misc + Wine PE dispatch)
  - AI compute (NVIDIA/AMD/Intel GPU, Transparent Huge Pages)
  - Agent sandboxing (cgroups v2, Landlock LSM, Seccomp-BPF)

### VM Images

Pre-built VM images are self-contained — no BIOS configuration needed:

| Format | Boots With | Import Into |
|--------|-----------|-------------|
| QCOW2 | SeaBIOS/UEFI | QEMU, KVM, Proxmox, libvirt |
| VMDK | BIOS/UEFI | VMware ESXi, Workstation, Fusion |
| VDI | BIOS/UEFI | VirtualBox |
| ISO | GRUB 2 | Any VM or bare metal |

### ISO Boot

The ISO includes custom GRUB configuration:
- Branding: `deploy/distro/branding/grub/hart-grub.cfg`
- Plymouth boot splash: `deploy/distro/branding/plymouth/hart-theme/`
- Boot menu: Install, Live, Safe Mode (minimal services)

### Raspberry Pi / ARM

```bash
# Build SD card image
nix build .#sd-server-arm

# Flash to SD card
dd if=result/sd-image/*.img of=/dev/sdX bs=4M status=progress
```

Hardware profile: `nixos/hardware/raspberry-pi.nix` (kernel, bootloader, device tree).

---

## Safe Mode & Recovery

### NixOS Generations (Atomic Rollback)

Every NixOS configuration change creates a new **generation** — a complete, bootable system snapshot.

```bash
# List all generations
nix-env --list-generations -p /nix/var/nix/profiles/system

# Roll back to previous generation
sudo nixos-rebuild switch --rollback

# Boot any generation from GRUB menu at startup
```

This means:
- Every upgrade is atomic (succeeds completely or not at all)
- You can always boot the previous working system from GRUB
- No "bricked" state is possible — there's always a working generation

### Upgrade Pipeline (Automated Rollback)

The upgrade orchestrator (`integrations/agent_engine/upgrade_orchestrator.py`) runs a 7-stage pipeline:

```
BUILD → TEST → AUDIT → BENCHMARK → SIGN → CANARY → DEPLOY
```

- **BENCHMARK**: `is_upgrade_safe()` blocks upgrades that regress performance by > 5%
- **CANARY**: Progressive rollout — auto-reverts on failure
- **ROLLBACK**: `rollback(reason)` at any stage reverts and broadcasts to peers

### Manual Recovery

```bash
# NixOS: boot previous generation from GRUB menu
# Select "HART OS — Configuration N (previous)" at boot

# Linux (systemd): restart services
sudo systemctl restart hart.target

# Docker: restart containers
docker-compose restart
docker-compose down && docker-compose up -d

# Emergency: start backend only (minimal services)
python hart_intelligence_entry.py
```

---

## Data Consistency & Persistence

### Data Stores

| Store | Location | Purpose |
|-------|----------|---------|
| **SQLite** | `agent_data/hevolve_database.db` | Primary database (agents, sessions, social) |
| **Agent Ledgers** | `agent_data/ledger_*.json` | Per-task state machines (cross-session recovery) |
| **Recipes** | `prompts/{id}_{flow}_recipe.json` | Trained agent recipes |
| **Baselines** | `agent_data/baselines/` | Agent performance snapshots |
| **Resonance** | `agent_data/resonance/` | User resonance profiles |
| **Node Keys** | `/var/lib/hart/node_*.key` | Ed25519 identity (immutable) |
| **Redis** | `localhost:6379` | Sessions, rate limiting (optional) |

### Persistence by Deployment Method

| Method | Data Survives Restart | Backup Strategy |
|--------|----------------------|-----------------|
| **Linux (systemd)** | Yes — `/var/lib/hart/` | rsync / borg cron |
| **Docker** | Yes — named volumes | `docker cp` / volume backup |
| **NixOS** | Yes — `/var/lib/hart/` | NixOS generations + rsync |
| **Manual (venv)** | Yes — `agent_data/` | Git / manual copy |

### Agent Ledger Recovery

Agent ledgers use a state machine that persists across sessions:

```
ASSIGNED → IN_PROGRESS → STATUS_VERIFICATION_REQUESTED → COMPLETED/ERROR → TERMINATED
```

If the process crashes mid-task, the ledger preserves the last committed state. On restart, tasks resume from their last checkpoint.

### Immutable Audit Log

All critical operations are recorded in a SHA-256 hash-chain audit log (`security/immutable_audit_log.py`). Each entry links to the previous via cryptographic hash — tampering is detectable.

---

## Error Recovery & Restore Points

### Recovery Matrix

| Failure | Recovery |
|---------|----------|
| Service crash | systemd auto-restart (3 retries / 60s) |
| Bad upgrade | NixOS rollback (GRUB menu) or `nixos-rebuild switch --rollback` |
| Automated upgrade failure | Upgrade pipeline auto-rollback at any stage |
| Database corruption | SQLite WAL mode + backup restore |
| Agent task failure | Ledger state machine resumes from last checkpoint |
| Tampered files | Runtime monitor + immutable audit log detects changes |
| Network partition | Regional nodes operate independently, reconcile on reconnect |
| Node failure (cluster) | Gossip protocol detects missing node, redistributes tasks |

### Recommended Backup Strategy

```bash
# Daily backup of agent data + database
rsync -avz /var/lib/hart/ /backup/hart/$(date +%Y%m%d)/

# Or with borg (deduplication)
borg create /backup/hart::$(date +%Y%m%d) /var/lib/hart/

# Docker volumes
docker run --rm -v hart-data:/data -v /backup:/backup \
  alpine tar czf /backup/hart-$(date +%Y%m%d).tar.gz /data
```

---

## Security Hardening (Production)

### Linux (systemd)

The installer configures:
- `ProtectSystem=strict` — read-only filesystem
- `NoNewPrivileges=yes` — no privilege escalation
- `PrivateTmp=yes` — isolated temp directory
- UFW firewall rules (`deploy/linux/firewall/hart-ufw.profile`)
- Fail2ban rate limiting (`deploy/linux/fail2ban/hart-api-filter.conf`)
- Kernel sysctl tuning (`deploy/distro/kernel/99-hart-sysctl.conf`)

### Docker (Production)

- Non-root user (`appuser`)
- Read-only filesystem (`--read-only`)
- Resource limits (CPU, memory)
- Health checks every 30s

### Environment Variables

```bash
# Production defaults
HEVOLVE_ENFORCEMENT_MODE=hard    # Strict security enforcement
HEVOLVE_DEV_MODE=false           # Disable dev shortcuts
HEVOLVE_ENV=production           # Production mode
```

### NixOS (Kernel-Level)

- Agent sandboxing: cgroups v2, Landlock LSM, Seccomp-BPF
- Agent slice: 80% max memory, 4096 max threads
- GPU device permissions restricted to `hart` group
- Immutable node keypair (`chattr +i`)

---

## Clustering (Multi-Node)

### Join an Existing Hive

```bash
# At install time
sudo bash deploy/linux/install.sh --join-peer http://central:6777

# Or via environment
export HART_JOIN_PEER=http://central:6777
export HEVOLVE_NODE_TIER=regional
python hart_intelligence_entry.py
```

### Gossip Discovery

Nodes discover each other via UDP gossip on port 6780:
- Default interval: 60 seconds
- Configurable: `HART_GOSSIP_INTERVAL`
- Seed peers: `HEVOLVE_SEED_PEERS=http://node1:6777,http://node2:6777`

### Certificate Chain

```
Central (master key signs)
  → Regional (central-issued certificate)
    → Local (regional-issued certificate)
```

Managed by `security/key_delegation.py`. Domain-based provisional authorization for `*.hevolve.ai` / `*.hertzai.com`.

### Node Watchdog

Background daemon (`security/node_watchdog.py`) monitors:
- Heartbeat protocol — detects offline nodes
- Frozen-thread detection — restarts stuck agents
- Service health checks — auto-restarts failed services

---

## CLI Reference

After installing (`pip install -e .`), the `hart` CLI is available:

```bash
hart chat "What is the weather?"     # Chat with agent
hart code "Fix the login bug"        # Coding agent
hart agent list                      # List agents
hart status                          # System status
hart remote-desktop status           # Remote desktop status
hart remote-desktop host             # Start hosting
hart remote-desktop connect ID       # Connect to device
hart recipe list                     # List trained recipes
hart -p "deploy to staging"          # Headless mode (AI agent compatible)
```

Full subcommands: `chat`, `code`, `social`, `agent`, `expert`, `pay`, `mcp`, `compute`, `channel`, `a2a`, `skill`, `voice`, `vision`, `desktop`, `remote`, `screenshot`, `tools`, `recipe`, `status`, `repomap`, `schedule`, `zeroshot`.

---

## GPU Setup (Optional)

### NVIDIA

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

### AMD (ROCm)

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm5.6
```

### Verify

```python
from integrations.service_tools.vram_manager import detect_gpu
print(detect_gpu())
```

---

## Running Tests

```bash
# All tests
pytest tests/ -v --noconftest

# Unit tests only
pytest tests/unit/ -v --noconftest

# Specific suites
pytest tests/unit/test_agent_creation.py -v --noconftest
pytest tests/unit/test_recipe_generation.py -v --noconftest
pytest tests/unit/test_reuse_mode.py -v --noconftest
pytest tests/unit/test_remote_desktop_*.py -v --noconftest

# Standalone suites
python tests/standalone/test_master_suite.py
python tests/standalone/test_autonomous_agent_suite.py
```

Use `--noconftest` to avoid fixture conflicts. Use `-p no:capture` for federation tests.

---

## Uninstall

```bash
# Linux (automated)
sudo bash deploy/linux/install.sh --uninstall
# Note: /var/lib/hart/ is preserved — remove manually if desired

# Docker
docker-compose down -v

# NixOS
# Remove hart modules from configuration.nix, then:
sudo nixos-rebuild switch

# Manual (venv)
deactivate
rm -rf venv310/
# Optionally: rm -rf agent_data/
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Port 6777 in use | `lsof -i :6777` to find process, or `--port N` to change |
| Python version error | Must use Python 3.10 (pydantic 1.10.9 requires it) |
| `ModuleNotFoundError: autogen` | `pip install autogen-agentchat==0.2.37` |
| pydantic v2 conflict | Remove `pydantic_core` if present: `pip uninstall pydantic_core` |
| No GPU detected | Install CUDA toolkit, then reinstall torch with CUDA |
| API key missing | Local-only mode works without cloud keys (uses budget_gate local models) |
| Test fixture errors | Use `--noconftest` flag |
| Vision service won't start | Check model at `HART_LLM_MODEL_PATH` or `/opt/hart/models/minicpm/` |
| Docker: read-only FS error | Set `HEVOLVE_TAMPER_CHECK_SKIP=true` with `--read-only` |
| NixOS build fails | Ensure flake inputs are fetched: `nix flake update` |

### Logs

```bash
# Linux (systemd)
journalctl -u hart-backend -f
journalctl -u hart.target --since "1 hour ago"

# Docker
docker-compose logs -f backend

# Manual
# Logs print to stdout — redirect as needed
python hart_intelligence_entry.py 2>&1 | tee hart.log
```

---

## Next Steps

- [Deployment Modes](deployment-modes.md) — flat, regional, central configurations
- [Configuration Reference](configuration.md) — detailed settings documentation
