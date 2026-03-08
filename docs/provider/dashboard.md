# Provider Dashboard

The provider dashboard shows your contribution to the HART OS compute network: how much you have contributed, what you have earned, and what is pending settlement.

## API Endpoint

### GET /api/settings/compute/provider

Returns a comprehensive view of your node's contribution.

**Access:** Node operator (sees own node). Admin sees all nodes.

### Response

```json
{
  "node_id": "my-node-001",
  "node_tier": "regional",
  "contribution": {
    "score": 42.5,
    "breakdown": {
      "uptime_weight": 15.0,
      "gpu_hours_weight": 12.0,
      "inference_weight": 8.5,
      "energy_weight": 4.0,
      "api_cost_weight": 3.0
    }
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
    "total_rewards": 23,
    "last_reward_at": "2026-02-20T14:30:00Z"
  }
}
```

## Data Sources

### Contribution Score

Computed by `HostingRewardService.compute_contribution_score()`. Factors:

- **Uptime:** How long the node has been active and healthy
- **GPU hours:** Total GPU time donated to hive/idle tasks
- **Inferences:** Number of inference requests served for others
- **Energy:** kWh of electricity contributed
- **API costs:** USD of metered API costs absorbed for hive tasks

### Compute Stats

Updated by `aggregate_compute_stats()` approximately every 50 seconds. Sources:

- `MeteredAPIUsage` records for hive/idle tasks
- GPU hour estimation: ~1 GPU-second per 1K tokens
- Energy estimation: ModelRegistry if available, else 170W TDP default

### Pending Settlements

Count and total USD of `MeteredAPIUsage` records with `settlement_status = 'pending'`. These are costs you have absorbed for hive tasks that have not yet been converted to Spark rewards.

### Reward Summary

From `HostingRewardService.get_reward_summary()`: total Spark earned, reward count, and timestamp of last reward.

## Deployment Mode

The same endpoint works for all deployment modes:

- **Flat:** Single-node standalone
- **Regional:** Part of a regional cluster
- **Central:** The central coordinator node

## See Also

- [settlement.md](settlement.md) -- How pending settlements become Spark
- [compute-config.md](compute-config.md) -- Adjust your contribution settings
