# UI Specification: Compute Settings Page

**CONTRACT** for the Nunba (bundled) and Hevolve React app (non-bundled) sibling projects.

## API Source

- **Read:** `GET /api/settings/compute`
- **Write:** `PUT /api/settings/compute`
- Both projects use the same API; no separate endpoints.

## Deployment Mode

| Mode | Access |
|------|--------|
| **Nunba (bundled)** | Settings page rendered in-app; API calls to localhost:6777 |
| **Non-bundled (React app)** | Settings page in external React app; API calls to configured HART OS host |

## Wireframe

```
┌─────────────────────────────────────────────────────────────┐
│  COMPUTE SETTINGS                                    [Save] │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ── Model Routing ──────────────────────────────────────    │
│                                                             │
│  Own Task Policy:     [local_preferred ▼]                   │
│  Hive Task Policy:    [local_preferred ▼]                   │
│  Max Hive GPU %:      [====50====] 50%                      │
│                                                             │
│  ── Metered API Policy ─────────────────────────────────    │
│                                                             │
│  Allow Metered for Hive:  [ ] OFF                           │
│  Daily Limit (USD):       [$0.00    ]                       │
│  ⚠ Central nodes cannot enable metered APIs for hive.       │
│                                                             │
│  ── Feature Flags ──────────────────────────────────────    │
│                                                             │
│  Accept Thought Experiments:  [✓] ON                        │
│  Accept Frontier Training:    [ ] OFF                       │
│  Offered GPU Hours/Day:       [8.0     ]                    │
│                                                             │
│  ── Settlement ─────────────────────────────────────────    │
│                                                             │
│  Auto-Settle:           [✓] ON                              │
│  Min Settlement (Spark): [10      ]                         │
│                                                             │
│  ── Provider Identity ──────────────────────────────────    │
│                                                             │
│  Cause Alignment:       [democratize_compute ▼]             │
│  Electricity Rate:      [$0.12 /kWh]                        │
│                                                             │
│  Node ID: my-node-001              Tier: regional           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Field-to-API Mapping

| UI Field | API Field | Type | Validation |
|----------|-----------|------|------------|
| Own Task Policy | `compute_policy` | dropdown | `local_only`, `local_preferred`, `cloud_preferred`, `cloud_only` |
| Hive Task Policy | `hive_compute_policy` | dropdown | Same as above |
| Max Hive GPU % | `max_hive_gpu_pct` | slider | 0-100 |
| Allow Metered for Hive | `allow_metered_for_hive` | toggle | Boolean; disabled on central tier |
| Daily Limit (USD) | `metered_daily_limit_usd` | number | >= 0 |
| Accept Thought Experiments | `accept_thought_experiments` | toggle | Boolean |
| Accept Frontier Training | `accept_frontier_training` | toggle | Boolean |
| Offered GPU Hours/Day | `offered_gpu_hours_per_day` | number | >= 0 |
| Auto-Settle | `auto_settle` | toggle | Boolean |
| Min Settlement (Spark) | `min_settlement_spark` | number | >= 1 |
| Cause Alignment | `cause_alignment` | dropdown | `democratize_compute`, `frontier_training`, `thought_experiments` |
| Electricity Rate | `electricity_rate_kwh` | number | >= 0 |

## Role-Based Visibility

| Role | Behavior |
|------|----------|
| **Admin** | Full read/write access to all settings on all nodes |
| **Operator** | Read/write access to own node's settings |
| **Viewer** | Read-only view of settings; Save button hidden |

## Tier-Based Restrictions

When `node_tier == "central"`:

- The "Allow Metered for Hive" toggle is disabled
- A warning banner is displayed: "Central nodes cannot enable metered APIs for hive tasks"
- PUT request with `allow_metered_for_hive: true` returns 403

## Save Behavior

1. Collect all changed fields
2. `PUT /api/settings/compute` with changed fields only
3. On success: toast "Settings saved" + refresh display
4. On 403: show tier restriction error
5. On 500: show generic error

## Implementation Notes

- Load settings on page mount via `GET /api/settings/compute`
- Node ID and tier are read-only (from response, not editable)
- Provider Identity section fields are gossipped to the network (note this in UI)
- Cache invalidation happens server-side; no client-side cache needed
