# Revenue API

The revenue pipeline tracks platform income, applies the 90/9/1 split, and auto-funds trading goals.

## GET /api/revenue/dashboard

Returns a comprehensive revenue dashboard.

**Source:** `revenue_aggregator.py` via `RevenueAggregator.get_dashboard()`.

### Response

```json
{
  "revenue": {
    "period_days": 30,
    "api_revenue": 150.0,
    "ad_revenue": 75.0,
    "hosting_payouts": 20.0,
    "total_gross": 225.0,
    "user_pool_share": 202.5,
    "infra_pool_share": 20.25,
    "central_share": 2.25,
    "platform_share": 22.5
  },
  "trading": {
    "funded": false,
    "platform_excess": 2.5,
    "threshold": 1000
  },
  "compute_borrowing": {
    "active_escrows": 3,
    "total_spark_escrowed": 150
  }
}
```

## Revenue Split Model (90/9/1)

All gross revenue is split according to hardcoded constants in `revenue_aggregator.py`:

| Pool | Percentage | Constant | Distribution |
|------|-----------|----------|--------------|
| User Pool | 90% | `REVENUE_SPLIT_USERS` | Proportional to `contribution_score` |
| Infrastructure Pool | 9% | `REVENUE_SPLIT_INFRA` | Regional + central, proportional to compute spent |
| Central | 1% | `REVENUE_SPLIT_CENTRAL` | Flat unconditional take |

## Revenue Sources

### API Revenue

Tracked via `APIUsageLog` table. Commercial customers pay credits for API usage. Queried from `APIUsageLog.cost_credits`.

### Ad Revenue

Tracked via `AdUnit` table. Advertisers spend Spark on ad placements. Queried from `AdUnit.spent_spark`.

### Hosting Payouts

Outgoing payments to node operators via `HostingReward` table. Subtracted from gross to compute net revenue.

## Auto-Funding Trading Goals

When platform excess (infra + central share minus hosting payouts) exceeds `HEVOLVE_REVENUE_FUNDING_THRESHOLD` (default: 1000 Spark):

1. 10% of excess is allocated to a paper trading goal
2. `GoalManager.create_goal()` creates a `trading` goal
3. Strategy: `long_term`, paper trading only
4. Live trading requires a constitutional vote

## Query Function

`query_revenue_streams(db, period_days)` is the single source of truth for revenue data. Used by both `RevenueAggregator.get_revenue_streams()` and `finance_tools.get_financial_health()`.

## See Also

- [agent-engine.md](agent-engine.md) -- Agent engine overview
- [../provider/settlement.md](../provider/settlement.md) -- Provider compensation
