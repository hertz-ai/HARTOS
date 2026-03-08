# Deployment Modes

HART OS supports three deployment modes that determine how nodes discover each other, synchronize state, and delegate tasks.

---

## Overview

| Mode | Nodes | Use Case |
|------|-------|----------|
| **Flat** | Single node, no federation | Development, standalone operation |
| **Regional** | Syncs to a central instance | Production clusters, geographic distribution |
| **Central** | Master node, hardened security | Network coordination, authoritative state |

---

## Flat Mode (Default)

The simplest deployment. A single HART OS node operates independently with no federation or state synchronization.

```bash
# No special configuration needed -- flat is the default
python langchain_gpt_api.py
```

**Characteristics:**

- All agents run locally
- No gossip protocol or peer discovery
- Ledger state is local only
- Suitable for development, testing, and single-user deployments

---

## Regional Mode

A regional node synchronizes its state with a central instance. It can operate independently when the central node is unreachable, then reconcile when connectivity is restored.

```bash
# Set the central URL for synchronization
export HEVOLVE_CENTRAL_URL=https://central.hevolve.ai
export HEVOLVE_NODE_TIER=regional

python langchain_gpt_api.py
```

**Characteristics:**

- Discovers peers through gossip protocol
- Syncs ledger state to central on a schedule
- Can delegate tasks to peers or accept delegated tasks
- Operates in degraded mode if central is unreachable
- Certificate chain: central -> regional -> local (3-tier delegation via `security/key_delegation.py`)

**Configuration:**

| Variable | Description |
|----------|-------------|
| `HEVOLVE_CENTRAL_URL` | URL of the central instance to sync with |
| `HEVOLVE_REGIONAL_URL` | This node's advertised URL for peer discovery |
| `HEVOLVE_NODE_TIER` | Set to `regional` |
| `HEVOLVE_REGISTRY_URL` | Optional registry for dynamic agent discovery |

---

## Central Mode

The central node is the authoritative coordinator for the network. It runs with hardened security defaults and serves as the trust anchor for the certificate chain.

```bash
export HEVOLVE_NODE_TIER=central
export HEVOLVE_ENFORCEMENT_MODE=hard

python langchain_gpt_api.py
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
- `HEVOLVE_ENFORCEMENT_MODE=hard` is the default in `start_cloud.sh`
- TLS verification enforced on all outbound connections
- Secret validation at startup

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
Development / Testing     -->  Flat (default)
Production single-user    -->  Flat or Nunba bundled
Production multi-node     -->  Regional + Central
End-user distribution     -->  Nunba bundled
```

---

## Next Steps

- [Configuration Reference](configuration.md) -- all environment variables for each mode
- [Federation & Gossip](../features/federation.md) -- how nodes discover and communicate
- [Security Model](../developer/security.md) -- certificate chain and trust anchors
