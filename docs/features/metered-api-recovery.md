# Metered API Recovery

Tracks and compensates nodes that spend real money on paid cloud APIs while executing tasks on behalf of other users.

## How It Works

1. Every non-local LLM call is recorded in the **MeteredAPIUsage** table with the model name, token count, estimated cost, and `task_source`.
2. The `task_source` field is propagated from the dispatch context and is one of:
   - **own** -- The node's own user initiated the task.
   - **hive** -- The task was dispatched from another node via federation.
   - **idle** -- The task was picked up by the idle compute coding agent.
3. At settlement time, `settle_metered_api_costs()` iterates unsettled hive/idle records and awards Spark to the API-providing node.

## Compensation Rate

```
SPARK_PER_USD = 100
```

For every USD spent on metered APIs for hive or idle tasks, the node receives 100 Spark.

## Daily Limit

The environment variable `HEVOLVE_METERED_DAILY_LIMIT` sets a USD cap on metered API spending for hive tasks per node per day. Once the limit is reached, the node stops accepting hive tasks that would require paid APIs until the next day.

## Distinction from APIUsageLog

| Table | Purpose |
|-------|---------|
| **MeteredAPIUsage** | Internal cost tracking and node compensation. Records every non-local call with task_source. |
| **APIUsageLog** | External commercial billing. Tracks API usage by paying customers of the platform. |

These are separate concerns. MeteredAPIUsage drives the recovery mechanism; APIUsageLog drives the billing system.

## Source Files

- `integrations/agent_engine/budget_gate.py`
- `integrations/agent_engine/revenue_aggregator.py`
- `integrations/social/models.py` (MeteredAPIUsage table)
