# Configuration

All configuration options for HART OS, organized by category.

---

## Configuration Sources

HART OS reads configuration from three sources (in order of precedence):

1. **Environment variables** -- highest priority, override all other sources
2. **`.env` file** -- loaded at startup, convenient for local development
3. **`config.json`** -- JSON file in the project root for API keys and service configuration

Runtime compute settings can also be updated via the **Settings API** (`PUT /api/settings/compute`).

---

## Core

**No key is required to run.** With none set, chat is served by a local model
through llama.cpp. Every key below only adds a cloud route.

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key for GPT models | (optional) |
| `GROQ_API_KEY` | Groq API key for fast inference | (optional) |
| `GLM_API_KEY` | GLM 5.2 (Zhipu/Z.ai) key, OpenAI-compatible (`ZHIPUAI_API_KEY` also accepted; `GLM_BASE_URL`/`GLM_MODEL` override endpoint/model) | (optional) |
| `LANGCHAIN_API_KEY` | LangChain API key for tracing | (optional) |
| `HEVOLVE_BASE_URL` | Base URL for this node's API | `https://hevolve.ai` |

---

## Compute

These settings control how the node allocates compute resources, gates API costs, and participates in hive workloads.

| Variable | Description | Default |
|----------|-------------|---------|
| `HEVOLVE_COMPUTE_POLICY` | Compute policy: `local_only`, `prefer_local`, `balanced`, `prefer_cloud` | `prefer_local` |
| `HEVOLVE_ALLOW_METERED_HIVE` | Allow metered (paid) API calls for hive tasks from other users | `false` |
| `HEVOLVE_MAX_HIVE_GPU_PCT` | Maximum GPU percentage allocatable to hive tasks | `50` |
| `HEVOLVE_METERED_DAILY_LIMIT` | Daily spending limit (USD) for metered API calls on hive tasks | `0.00` |
| `HEVOLVE_SPARK_PER_USD` | Spark-to-USD conversion rate for budget gating | `1000` |

### Budget Gate

The budget gate (`budget_gate.py`) enforces per-request cost limits:

- **Local models** (LLaMA, Mistral, Phi, Qwen, Groq-hosted) cost **0 Spark**
- **Cloud models** (OpenAI GPT-4, etc.) are metered **per 1K tokens**
- Requests exceeding the user's Spark balance are rejected

### Compute Escrow

The `ComputeEscrow` table in the database provides persistent tracking of compute debts between nodes, replacing the earlier in-memory `_compute_debts` dictionary.

---

## Security

| Variable | Description | Default |
|----------|-------------|---------|
| `HEVOLVE_ENFORCEMENT_MODE` | Guardrail enforcement: `hard` (block violations) or `soft` (warn only) | `soft` (flat), `hard` (central) |
| `HEVOLVE_DEV_MODE` | Enable dev mode (relaxed security). **Forced off on central.** | `false` |
| `HEVOLVE_NODE_TIER` | Node tier: `central`, `regional`, or `local` | `local` |

!!! warning
    On central nodes, `HEVOLVE_DEV_MODE` is forced off at three enforcement layers regardless of the environment variable value. Do not attempt to override this.

---

## Network

| Variable | Description | Default |
|----------|-------------|---------|
| `HEVOLVE_CENTRAL_URL` | URL of the central instance for state sync | (none) |
| `HEVOLVE_REGIONAL_URL` | This node's advertised URL for peer discovery | (none) |
| `HEVOLVE_REGISTRY_URL` | Dynamic agent registry URL | (none) |

### Transport: WAMP relay & federation

WAMP is the **central relay AND federation transport**. These knobs decide which
router a node talks to, and they are the difference between "federates through the
cloud" and "runs entirely on its own".

| Variable | Description | Default |
|----------|-------------|---------|
| `WAMP_URL` | The crossbar router this node uses. Accepts **either dialect** — a `ws://host:8088/ws` router URL or an `http://host:8088/publish` bridge URL — and `core/wamp_url.py` converts between them, so both the RPC path and the publish path always agree on the host. | `ws://azurekong.hertzai.com:8088/ws` |
| `HART_CROSSBAR_PORT` | Router port, resolved via `core/port_registry.py`. | `8088` |
| `CBURL` | PeerLink's own router override (`core/peer_link/telemetry.py`, `nat.py`). **A second naming scheme** — see the note below. | `ws://aws_rasa.hertzai.com:8088/ws` |
| `CBREALM` | WAMP realm for the PeerLink leg. | `realm1` |
| `HEVOLVE_CENTRAL_DB_URL` | Central DB base URL for cross-device reads. **`''` (empty) disables** cross-device merge rather than falling back. | `https://azurekong.hertzai.com:8443/db` |

**Run your own server.** Every one of these is independent, and the override always
wins over the default:

```bash
# This node runs its OWN router (Nunba ships one: wamp_router.py on :8088).
# Both the RPC path and the publish bridge follow — no split brain.
WAMP_URL=ws://127.0.0.1:8088/ws

# Or relay through a regional host instead of central.
WAMP_URL=ws://regional-3.lan:8088/ws

# Private central DB, independently of whatever the router is doing.
HEVOLVE_CENTRAL_DB_URL=https://my-private-cloud:8443/db

# Or no central reads at all.
HEVOLVE_CENTRAL_DB_URL=
```

They are **deliberately separate knobs**: a node may run its own router while still
reading the central DB, or the reverse. There is intentionally no single `HART_HOST`
that moves everything — that would trade the flexibility for tidiness.

**Where the default hostname comes from.** `core/constants.py:CENTRAL_HOST` is the
one literal (`azurekong.hertzai.com`); `wamp_url` and `config_cache` compose their
URLs from it plus their own port. It is a *default*, not a mandate — set the
variables above and the constant is never consulted.

`aws_rasa.hertzai.com` is a **legacy alias for the same machine** (both resolve to
`106.51.181.24` and serve identical `/ws` and `/publish`). It still appears in ~14
files and in `CBURL`'s default; prefer `azurekong`, which is what the run scripts
export.

> **Known rough edge.** `CBURL`/`CBREALM` are a second naming scheme for the same
> thing as `WAMP_URL`, and PeerLink reads them directly rather than through
> `core/wamp_url.py`. Setting `WAMP_URL` alone will **not** move PeerLink's relay.
> Tracked in `docs/architecture/HARTOS_PARALLEL_PATH_AUDIT.md`; until it is
> unified, set both if you are moving a node off central.

---

## Features

| Variable | Description | Default |
|----------|-------------|---------|
| `HEVOLVE_AGENT_ENGINE_ENABLED` | Enable the unified agent goal engine | `true` |
| `HEVOLVE_CODING_AGENT_ENABLED` | Enable the idle-compute coding agent (also drains self_heal goal queue from error_advice; safe to leave on — daemon early-returns when no idle agents opted in) | `true` |
| `HEVOLVE_AUTO_DISCOVERY` | Enable automatic peer discovery via gossip | `true` |

---

## Nunba Bundled

| Variable | Description | Default |
|----------|-------------|---------|
| `NUNBA_BUNDLED` | Enable Nunba bundled mode | `false` |

When `NUNBA_BUNDLED=true`:

- Database path: `~/Documents/Nunba/data/`
- Full agent suite enabled with sensible defaults
- Designed for end-user distribution

---

## config.json

The `config.json` file holds API keys for external services. Create it in the project root:

```json
{
  "OPENAI_API_KEY": "sk-...",
  "GROQ_API_KEY": "gsk_...",
  "GOOGLE_CSE_ID": "your-custom-search-engine-id",
  "GOOGLE_API_KEY": "your-google-api-key",
  "NEWS_API_KEY": "your-newsapi-key",
  "SERPAPI_API_KEY": "your-serpapi-key"
}
```

---

## Runtime Settings API

Compute settings can be updated at runtime without restarting the server (use `http://localhost:6777` if self-hosted):

```bash
curl -X PUT https://hevolve.ai/api/settings/compute \
  -H "Content-Type: application/json" \
  -d '{
    "compute_policy": "prefer_local",
    "allow_metered_hive": false,
    "max_hive_gpu_pct": 50,
    "metered_daily_limit": 5.00
  }'
```

See [Settings API](../api/settings.md) for the full endpoint reference.

---

## Next Steps

- [Deployment Modes](deployment-modes.md) -- how configuration varies by mode
- [Budget Gating](../features/budget-gating.md) -- how Spark costs are enforced
- [Compute Policies](../features/compute-policies.md) -- local vs. cloud inference routing
