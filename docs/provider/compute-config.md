# Compute Configuration

Configure how your node routes models, handles hive tasks, and manages metered API costs.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HEVOLVE_NODE_ID` | `local` | Unique node identifier |
| `HEVOLVE_NODE_TIER` | `flat` | Topology mode: `flat`, `regional`, `central` |
| `HEVOLVE_COMPUTE_POLICY` | `local_preferred` | Default model routing policy |
| `HEVOLVE_SPARK_PER_USD` | `100` | Spark-to-USD conversion rate for settlements |

## API Endpoints

### GET /api/settings/compute

Returns the merged configuration (env > DB > defaults).

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

### PUT /api/settings/compute

Updates configuration. Fields are routed to the correct table automatically:

- **Policy fields** go to `NodeComputeConfig` (local-only, not gossipped)
- **Provider identity** goes to `PeerNode` (gossipped to the network)

```bash
curl -X PUT http://localhost:6777/api/settings/compute \
  -H "Content-Type: application/json" \
  -d '{
    "compute_policy": "local_only",
    "max_hive_gpu_pct": 30,
    "cause_alignment": "frontier_training"
  }'
```

**Tier restriction:** Central nodes cannot set `allow_metered_for_hive: true` (returns 403).

## NodeComputeConfig Table

Per-node local policy (not gossipped to the network).

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `node_id` | String | -- | Unique node identifier |
| `compute_policy` | String | `local_preferred` | Model routing for own tasks |
| `hive_compute_policy` | String | `local_preferred` | Model routing for hive tasks |
| `max_hive_gpu_pct` | Integer | `50` | Max GPU % allocated to hive |
| `allow_metered_for_hive` | Boolean | `false` | Opt-in to use paid APIs for hive tasks |
| `metered_daily_limit_usd` | Float | `0.0` | Daily USD cap for metered APIs |
| `offered_gpu_hours_per_day` | Float | `0.0` | Advertised GPU hours available |
| `accept_thought_experiments` | Boolean | `true` | Accept thought experiment tasks |
| `accept_frontier_training` | Boolean | `false` | Accept frontier training tasks |
| `auto_settle` | Boolean | `true` | Auto-settle metered costs to Spark |
| `min_settlement_spark` | Integer | `10` | Minimum Spark for settlement |

## Provider Identity (PeerNode)

Identity fields live on `PeerNode` and are gossipped to the network.

| Column | Type | Description |
|--------|------|-------------|
| `cause_alignment` | String | Why this node contributes (see [cause.md](cause.md)) |
| `electricity_rate_kwh` | Float | Operator's electricity cost (USD per kWh) |
| `gpu_hours_served` | Float | Cumulative GPU hours served (aggregated) |
| `total_inferences` | Integer | Cumulative inference count |
| `energy_kwh_contributed` | Float | Cumulative energy contributed |
| `metered_api_costs_absorbed` | Float | Cumulative USD absorbed for hive/idle tasks |

## Compute Policy Values

| Policy | Behavior |
|--------|----------|
| `local_only` | Never use cloud APIs; local models only |
| `local_preferred` | Use local models first; fall back to cloud if needed |
| `cloud_preferred` | Prefer cloud APIs; use local as fallback |
| `cloud_only` | Cloud APIs only (requires API keys) |

## See Also

- [joining.md](joining.md) -- Initial setup
- [settlement.md](settlement.md) -- Cost recovery mechanics
