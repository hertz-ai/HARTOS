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

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key for GPT models | (required) |
| `GROQ_API_KEY` | Groq API key for fast inference | (optional) |
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

---

## Features

| Variable | Description | Default |
|----------|-------------|---------|
| `HEVOLVE_AGENT_ENGINE_ENABLED` | Enable the unified agent goal engine | `true` |
| `HEVOLVE_CODING_AGENT_ENABLED` | Enable the idle-compute coding agent | `false` |
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
