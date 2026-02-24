# UI Specification: Provider Contribution Dashboard

**CONTRACT** for the Nunba (bundled) and Hevolve React app (non-bundled) sibling projects.

## API Source

- **Read:** `GET /api/settings/compute/provider`
- Same API for both Nunba and non-bundled deployments.

## Wireframe

```
┌─────────────────────────────────────────────────────────────┐
│  PROVIDER DASHBOARD                     Node: my-node-001   │
│                                         Tier: regional      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ── Contribution Score ─────────────────────────────────    │
│                                                             │
│  ┌──────────────────────────────────┐                       │
│  │        SCORE: 42.5               │                       │
│  │  ████████████████░░░░░░░░░░░░░░  │                       │
│  └──────────────────────────────────┘                       │
│                                                             │
│  Breakdown:                                                 │
│  ├── Uptime           15.0                                  │
│  ├── GPU Hours        12.0                                  │
│  ├── Inferences        8.5                                  │
│  ├── Energy            4.0                                  │
│  └── API Costs         3.0                                  │
│                                                             │
│  ── Compute Stats ──────────────────────────────────────    │
│                                                             │
│  GPU Hours Served:          156.3 hrs                       │
│  Total Inferences:        12,847                            │
│  Energy Contributed:        26.6 kWh                        │
│  API Costs Absorbed:       $4.82                            │
│                                                             │
│  [Chart: Compute stats over time - line/bar chart]          │
│                                                             │
│  ── Pending Settlements ────────────────────────────────    │
│                                                             │
│  ┌──────────┬──────────┬───────────┬────────────┐           │
│  │ Date     │ Model    │ USD Cost  │ Status     │           │
│  ├──────────┼──────────┼───────────┼────────────┤           │
│  │ Feb 20   │ gpt-4    │ $0.23     │ pending    │           │
│  │ Feb 20   │ claude-3 │ $0.18     │ pending    │           │
│  │ Feb 19   │ gpt-4    │ $0.31     │ settled    │           │
│  └──────────┴──────────┴───────────┴────────────┘           │
│                                                             │
│  Pending: 7 records, $1.23 total                            │
│                                                             │
│  ── Reward History ─────────────────────────────────────    │
│                                                             │
│  Total Spark Earned:     482 ⚡                              │
│  Total Rewards:           23                                │
│  Last Reward:            Feb 20, 2026 14:30                 │
│                                                             │
│  ── Provider Identity ──────────────────────────────────    │
│                                                             │
│  Cause:       democratize_compute                           │
│  Electricity: $0.12 /kWh                                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Response-to-UI Mapping

| UI Section | API Field Path | Display |
|------------|---------------|---------|
| Score card | `contribution.score` | Large number + progress bar |
| Score breakdown | `contribution.breakdown.*` | Stacked bar or list |
| GPU Hours | `compute_stats.gpu_hours_served` | Number + "hrs" |
| Inferences | `compute_stats.total_inferences` | Formatted integer |
| Energy | `compute_stats.energy_kwh_contributed` | Number + "kWh" |
| API Costs | `compute_stats.metered_api_costs_absorbed` | "$" + number |
| Pending count | `pending_settlements.count` | Integer |
| Pending USD | `pending_settlements.total_usd` | "$" + number |
| Spark earned | `reward_summary.total_spark_earned` | Number + spark icon |
| Reward count | `reward_summary.total_rewards` | Integer |
| Last reward | `reward_summary.last_reward_at` | Formatted datetime |
| Cause | `provider_identity.cause_alignment` | String |
| Electricity | `provider_identity.electricity_rate_kwh` | "$" + number + "/kWh" |

## Role-Based Visibility

| Role | Behavior |
|------|----------|
| **Admin** | Sees dashboard for all nodes; node selector dropdown at top |
| **Operator** | Sees own node's dashboard only |

## Compute Stats Chart

Recommended: line chart showing GPU hours, inferences, and energy over the last 7/30 days. Data source requires client-side aggregation from the cumulative totals (or a future time-series endpoint).

## Refresh

- Auto-refresh every 60 seconds (compute stats update every ~50s server-side)
- Manual refresh button in top-right corner

## Error States

- **404 "Node not registered":** Show onboarding prompt with link to join endpoint
- **500:** Show generic error with retry button

## Implementation Notes

- Dashboard is read-only; no write operations
- Settlements table may need pagination for nodes with many records
- The compute stats chart is aspirational; initial implementation can show current totals only
- Both Nunba and React app render identical layouts from the same API response
