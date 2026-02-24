# Revenue Model

HART OS distributes all revenue using a fixed 90/9/1 split.

## Split Ratios

| Pool | Share | Distribution Method |
|------|-------|---------------------|
| **User Pool** | 90% | Proportional to each node's `contribution_score` (see [contribution-scoring.md](contribution-scoring.md)). |
| **Infrastructure Pool** | 9% | Split across regional and central infrastructure nodes. |
| **Central** | 1% | Flat, unconditional allocation to the central instance. |

## Canonical Constants

Defined in `integrations/agent_engine/revenue_aggregator.py`:

```python
REVENUE_SPLIT_USERS   = 0.90
REVENUE_SPLIT_INFRA   = 0.09
REVENUE_SPLIT_CENTRAL = 0.01
```

These constants are imported by `ad_service.py`, `hosting_reward_service.py`, and `finance_tools.py` (with try/except fallback to the same values).

## Revenue Sources

| Source | Description |
|--------|-------------|
| **Ad Revenue (Spark)** | In-platform advertising; revenue measured in Spark and split at impression time. |
| **API Revenue** | Commercial API access billed to external consumers via APIUsageLog. |
| **Hosting Rewards** | Rewards for nodes that contribute compute, storage, or bandwidth to the network. |

## Single Source of Truth

All revenue queries go through `query_revenue_streams()` in `revenue_aggregator.py`. Do not query revenue tables directly.

## Source Files

- `integrations/agent_engine/revenue_aggregator.py`
- `integrations/social/services.py` (ad_service, hosting_reward_service)
- `integrations/agent_engine/finance_tools.py`
