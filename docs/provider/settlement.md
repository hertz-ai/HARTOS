# Settlement: How Compensation Works

When your node serves hive or idle tasks that consume paid API credits, you are compensated in Spark (the platform's internal currency).

## Settlement Flow

```
1. Hive/idle task runs on your node
2. MeteredAPIUsage record created (model, tokens, USD cost)
3. settle_metered_api_costs() runs periodically
4. USD cost converted to Spark at SPARK_PER_USD rate
5. Spark credited to operator's wallet via ResonanceService.award_spark()
6. MeteredAPIUsage.settlement_status → 'settled'
```

## MeteredAPIUsage Records

Every metered API call for non-own tasks creates a record:

| Field | Description |
|-------|-------------|
| `node_id` | Node that absorbed the cost |
| `operator_id` | Node operator's user ID |
| `model_id` | Model used (e.g., `gpt-4`, `claude-3`) |
| `task_source` | `own`, `hive`, or `idle` (only hive/idle are settled) |
| `tokens_in` / `tokens_out` | Token counts |
| `actual_usd_cost` | Real USD cost of the API call |
| `settlement_status` | `pending` or `settled` |

## Settlement Mechanics

The `settle_metered_api_costs()` function (in `revenue_aggregator.py`):

1. Queries all `MeteredAPIUsage` records with `settlement_status = 'pending'`
2. Converts each record's `actual_usd_cost` to Spark: `spark = max(1, int(usd * SPARK_PER_USD))`
3. Awards Spark to the operator's `ResonanceWallet`
4. Marks records as `settled`

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `HEVOLVE_SPARK_PER_USD` | `100` | Spark awarded per USD of API cost absorbed |
| `auto_settle` | `true` | Automatically settle pending costs |
| `min_settlement_spark` | `10` | Minimum Spark threshold before settlement runs |

## Controlling Exposure

To limit how much USD your node absorbs for hive tasks:

```bash
curl -X PUT http://localhost:6777/api/settings/compute \
  -H "Content-Type: application/json" \
  -d '{
    "allow_metered_for_hive": true,
    "metered_daily_limit_usd": 5.00
  }'
```

- `allow_metered_for_hive: false` (default) -- Your node never uses paid APIs for hive tasks
- `metered_daily_limit_usd: 5.00` -- Cap daily metered API spending at $5
- `compute_policy: local_only` -- Force local models only; zero API cost

## Viewing Pending Settlements

```bash
curl http://localhost:6777/api/settings/compute/provider
```

The `pending_settlements` section shows count and total USD awaiting settlement.

## See Also

- [compute-config.md](compute-config.md) -- Configure metered API limits
- [dashboard.md](dashboard.md) -- View reward history
