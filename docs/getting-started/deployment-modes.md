# Deployment Modes

HART OS supports three deployment tiers that determine how nodes discover each other, synchronize state, and delegate tasks. Each tier can run bare-metal (Python) or via Docker.

---

## Overview

| Tier | Nodes | Use Case |
|------|-------|----------|
| **Flat** | Single node, no federation | Development, standalone operation |
| **Regional** | Syncs to central, participates in hive | Production clusters, community nodes |
| **Central** | Master node, hardened security | Network coordination, trust anchor |

---

## Flat Mode (Default)

The simplest deployment. A single HART OS node operates independently with no federation or state synchronization.

**Bare-metal:**
```bash
# No special configuration needed -- flat is the default
python langchain_gpt_api.py
```

**Docker:**
```bash
scripts/start_docker.sh --tier flat
```

**Minimal `.env`:**
```
OPENAI_API_KEY=sk-...
```

**Characteristics:**

- All agents run locally
- No gossip protocol or peer discovery
- Ledger state is local only
- Suitable for development, testing, and single-user deployments

---

## Regional Mode

A regional node participates in the hive -- it synchronizes state with central, discovers peers via gossip, and can accept or delegate tasks.

**Certificate requirement:** Regional nodes need a delegated certificate from central to join the network. This is a 3-tier chain: **central -> regional -> local** (via `security/key_delegation.py`).

### How to become a regional node

1. **Generate your node keypair** -- happens automatically on first boot (`security/node_integrity.py`)
2. **Register with central** -- your node sends its public key + FQDN to central's `/register` endpoint
3. **Challenge-response** -- central sends a signed challenge, your node signs it with its private key
4. **Certificate issued** -- central issues a 7-day provisional certificate (auto-renews)
5. **Set `HART_NODE_KEY`** -- a shared secret for federation HMAC (provided by central admin)

This is handled automatically by `key_delegation.py`'s `DnsKeyVerifier.handle_register()` flow. The regional operator contacts the central admin to receive their `HART_NODE_KEY` for federation.

**Bare-metal:**
```bash
export HEVOLVE_CENTRAL_URL=https://central.hevolve.ai
export HEVOLVE_NODE_TIER=regional

scripts/start_regional.sh --host http://your-llm-server:8080/v1
```

**Docker:**
```bash
scripts/start_docker.sh --tier regional
```

**`.env` for regional:**
```
HEVOLVE_NODE_TIER=regional
HEVOLVE_CENTRAL_URL=https://central.hevolve.ai
HEVOLVE_LLM_ENDPOINT_URL=http://your-llm-server:8080/v1
HEVOLVE_LLM_MODEL_NAME=Qwen3-VL-4B-Instruct
HART_NODE_KEY=your-federation-key        # From central admin
ENABLE_FEDERATION=true
```

**Characteristics:**

- Discovers peers through gossip protocol
- Syncs ledger state to central on a schedule
- Can delegate tasks to peers or accept delegated tasks
- Operates in degraded mode if central is unreachable
- Certificate chain: central -> regional -> local (3-tier delegation via `security/key_delegation.py`)
- Certificate auto-renews every 7 days

**Configuration:**

| Variable | Description |
|----------|-------------|
| `HEVOLVE_CENTRAL_URL` | URL of the central instance to sync with |
| `HEVOLVE_REGIONAL_URL` | This node's advertised URL for peer discovery |
| `HEVOLVE_NODE_TIER` | Set to `regional` |
| `HEVOLVE_LLM_ENDPOINT_URL` | Regional LLM server (llama.cpp, vLLM, etc.) |
| `HART_NODE_KEY` | Shared secret for federation HMAC signing (from central admin) |
| `ENABLE_FEDERATION` | Set to `true` to participate in hive |
| `HEVOLVE_REGISTRY_URL` | Optional registry for dynamic agent discovery |

---

## Central Mode

The central node is the authoritative coordinator for the network. It runs with hardened security defaults and serves as the trust anchor for the certificate chain.

**Central-only resources:**

- **Master key** (`/etc/hevolve/master_private_key.hex`) -- Ed25519 signing key for releases, certificates, and kill switch. Never in repo, never accessible to AI.
- **Release manifest** (`release_manifest.json`) -- Signed file hash manifest from CI/CD. Boot integrity verification checks installed code against this.
- **Cloud database** (`HEVOLVE_DB_URL`) -- MySQL for persistent state across restarts.

**Bare-metal:**
```bash
scripts/start_cloud.sh
```

**Docker:**
```bash
scripts/start_docker.sh --tier central
```

**`.env` for central:**
```
HEVOLVE_NODE_TIER=central
HEVOLVE_ENFORCEMENT_MODE=hard
HEVOLVE_DEV_MODE=false
OPENAI_API_KEY=sk-...
HEVOLVE_DB_URL=mysql+pymysql://user:pass@host/dbname
ENABLE_FEDERATION=true
```

**Characteristics:**

- Authoritative state for the network
- Hardened security (dev mode forced off, rate limiting, TLS checks)
- Issues certificates to regional and local nodes
- Rate limit: 30 requests/minute on `/chat`
- Secret validation and DB encryption checks at boot
- `HEVOLVE_DEV_MODE` is forced off on central (enforced at three layers: `__init__.py`, `_validate_startup`, and `start_cloud.sh`)

**Security hardening on central:**

- `verify_tier_authorization()` called at boot
- Dev mode cannot be enabled (triple-enforced)
- `HEVOLVE_ENFORCEMENT_MODE=hard` is the default
- TLS verification enforced on all outbound connections
- Secret validation at startup
- Master key stored outside repo at `/etc/hevolve/master_private_key.hex`

---

## Docker Script (`scripts/start_docker.sh`)

One script for all tiers. Handles build, run, stop, logs, and health checks.

```bash
scripts/start_docker.sh                      # Build + run (tier from .env or flat)
scripts/start_docker.sh --tier central       # Central deployment
scripts/start_docker.sh --tier regional      # Regional deployment
scripts/start_docker.sh build                # Build only
scripts/start_docker.sh run                  # Run only
scripts/start_docker.sh stop                 # Stop + remove
scripts/start_docker.sh restart              # Stop + run (no rebuild)
scripts/start_docker.sh logs                 # Tail logs
scripts/start_docker.sh status               # Container status + health
```

The script:
- Resolves all paths relative to repo root (`.env`, `logs/`, `output_images/`, `Dockerfile`)
- Auto-detects `sudo` requirement
- Loads master key from `/etc/hevolve/` only for central tier
- Mounts release manifest only if present
- Runs health check after startup
- Prints helpful `.env` template if missing

---

## Certificate Chain (Trust Delegation)

```
Central (master key)
  └── issues cert to Regional (7-day validity, auto-renews)
        └── issues cert to Local (3-day validity, auto-renews)
```

- **Central -> Regional**: Regional operator contacts central admin, receives `HART_NODE_KEY`. Node auto-registers via DNS challenge-response (`key_delegation.py`).
- **Regional -> Local**: Local nodes register with their regional host using the same challenge-response flow.
- **No master key needed**: Regional and local nodes never touch the master key. They receive delegated certificates signed by their parent in the chain.

---

## Nunba Bundled Mode

Nunba is the end-user distribution of HART OS. When bundled mode is active, the runtime uses user-local data paths and activates sensible defaults.

```bash
export NUNBA_BUNDLED=true

python langchain_gpt_api.py
```

**Characteristics:**

- Database stored at `~/Documents/Nunba/data/`
- Full agent suite enabled with defaults
- Designed for non-technical end users
- Can operate in flat mode or connect to the network as a regional node

---

## Choosing a Mode

```
Development / Testing          -->  Flat (default)
Production single-user         -->  Flat or Nunba bundled
Community node (join hive)     -->  Regional
Production multi-node cluster  -->  Regional + Central
End-user distribution          -->  Nunba bundled
Network coordinator            -->  Central (one per network)
```

---

## Next Steps

- [Configuration Reference](configuration.md) -- all environment variables for each mode
- [Federation & Gossip](../features/federation.md) -- how nodes discover and communicate
- [Security Model](../developer/security.md) -- certificate chain and trust anchors
