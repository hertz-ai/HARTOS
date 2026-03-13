# Settings API

The settings API configures compute policies, provider identity, and node behavior. All endpoints are under `/api/settings/compute`.

## GET /api/settings/compute

Returns the merged compute configuration (env > DB > defaults).

**Access:** Any authenticated node operator.

### Response

```json
{
  "node_id": "my-node-001",
  "compute_policy": "local_preferred",
  "hive_compute_policy": "local_preferred",
  "max_hive_gpu_pct": 50,
  "allow_metered_for_hive": false,
  "metered_daily_limit_usd": 0.0,
  "offered_gpu_hours_per_day": 8.0,
  "accept_thought_experiments": true,
  "accept_frontier_training": false,
  "auto_settle": true,
  "min_settlement_spark": 10,
  "electricity_rate_kwh": 0.12,
  "cause_alignment": "democratize_compute"
}
```

## PUT /api/settings/compute

Updates compute configuration. Single endpoint that routes fields to the correct table.

**Access:** Node operator or admin. Tier-aware.

### Request

```json
{
  "compute_policy": "local_only",
  "max_hive_gpu_pct": 30,
  "allow_metered_for_hive": true,
  "metered_daily_limit_usd": 5.0,
  "cause_alignment": "frontier_training",
  "electricity_rate_kwh": 0.10
}
```

### Field Routing

| Field | Table | Gossipped? |
|-------|-------|------------|
| `compute_policy` | NodeComputeConfig | No |
| `hive_compute_policy` | NodeComputeConfig | No |
| `max_hive_gpu_pct` | NodeComputeConfig | No |
| `allow_metered_for_hive` | NodeComputeConfig | No |
| `metered_daily_limit_usd` | NodeComputeConfig | No |
| `offered_gpu_hours_per_day` | NodeComputeConfig | No |
| `accept_thought_experiments` | NodeComputeConfig | No |
| `accept_frontier_training` | NodeComputeConfig | No |
| `auto_settle` | NodeComputeConfig | No |
| `min_settlement_spark` | NodeComputeConfig | No |
| `cause_alignment` | PeerNode | Yes |
| `electricity_rate_kwh` | PeerNode | Yes |

### Tier Restriction

Central nodes (`HEVOLVE_NODE_TIER=central`) cannot set `allow_metered_for_hive: true`. This returns:

```json
{"error": "Central nodes cannot enable metered APIs for hive"}
```

Status: 403

### Response

```json
{
  "updated": true,
  "node_id": "my-node-001",
  "policy_fields": ["compute_policy", "max_hive_gpu_pct"],
  "peer_fields": ["cause_alignment"]
}
```

## GET /api/settings/compute/provider

Provider contribution dashboard.

**Access:** Node operator (own node). Admin sees all nodes.

### Response

```json
{
  "node_id": "my-node-001",
  "node_tier": "regional",
  "contribution": {
    "score": 42.5,
    "breakdown": { "..." : "..." }
  },
  "compute_stats": {
    "gpu_hours_served": 156.3,
    "total_inferences": 12847,
    "energy_kwh_contributed": 26.57,
    "metered_api_costs_absorbed": 4.82
  },
  "provider_identity": {
    "cause_alignment": "democratize_compute",
    "electricity_rate_kwh": 0.12
  },
  "pending_settlements": {
    "count": 7,
    "total_usd": 1.23
  },
  "reward_summary": {
    "total_spark_earned": 482,
    "total_rewards": 23
  }
}
```

## POST /api/settings/compute/provider/join

Simple provider onboarding. Creates `NodeComputeConfig` and sets `PeerNode` identity with sensible defaults.

**Access:** Any node.

### Request

```json
{
  "cause_alignment": "democratize_compute",
  "electricity_rate_kwh": 0.12,
  "offered_gpu_hours_per_day": 8,
  "compute_policy": "local_preferred"
}
```

All fields are optional. Defaults:

- `cause_alignment`: `"democratize_compute"`
- `compute_policy`: `"local_preferred"`
- `accept_thought_experiments`: `true`

### Response

```json
{
  "joined": true,
  "node_id": "my-node-001",
  "config": { "..." : "..." }
}
```

## See Also

- [core.md](core.md) -- Core endpoints
- [../provider/compute-config.md](../provider/compute-config.md) -- Provider configuration guide
