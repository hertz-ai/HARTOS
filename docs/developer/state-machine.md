# ActionState Machine

The ActionState machine (in `lifecycle_hooks.py`) tracks the lifecycle of every action in the CREATE/REUSE pipeline. State changes auto-sync to SmartLedger for persistence.

## States

```
ASSIGNED ──> IN_PROGRESS ──> STATUS_VERIFICATION_REQUESTED ──> COMPLETED
                                       │                          │
                                       ├──> PENDING               ├──> TERMINATED
                                       │                          │
                                       └──> ERROR ────────────────┘
                                              │
                                              ├──> FALLBACK_REQUESTED ──> FALLBACK_RECEIVED ──> IN_PROGRESS
                                              │
                                              └──> RECIPE_REQUESTED ──> RECIPE_RECEIVED
```

## State Definitions

| State | Value | Description |
|-------|-------|-------------|
| `ASSIGNED` | `assigned` | Action received from decomposition array |
| `IN_PROGRESS` | `in_progress` | Action execution underway |
| `STATUS_VERIFICATION_REQUESTED` | `status_verification_requested` | Status check sent to verifier |
| `COMPLETED` | `completed` | Action performed successfully and verified |
| `PENDING` | `pending` | Action pending completion by verifier |
| `ERROR` | `error` | Action error or JSON parse error |
| `FALLBACK_REQUESTED` | `fallback_requested` | Fallback strategy requested from user |
| `FALLBACK_RECEIVED` | `fallback_received` | Fallback response received |
| `RECIPE_REQUESTED` | `recipe_requested` | Recipe JSON creation requested from AI |
| `RECIPE_RECEIVED` | `recipe_received` | Recipe JSON received with status done |
| `TERMINATED` | `terminated` | Action passed to chat instructor, terminate issued |
| `EXECUTING_MOTION` | `executing_motion` | Physical action executing via WorldModelBridge |
| `SENSOR_CONFIRM` | `sensor_confirm` | Waiting for sensor confirmation of physical outcome |

## Ledger Sync

State changes auto-sync to SmartLedger via `_auto_sync_to_ledger()`. The mapping:

| ActionState | LedgerTaskStatus |
|-------------|------------------|
| ASSIGNED | PENDING |
| IN_PROGRESS | IN_PROGRESS |
| STATUS_VERIFICATION_REQUESTED | VALIDATING |
| COMPLETED | COMPLETED |
| PENDING | BLOCKED |
| ERROR | FAILED |
| FALLBACK_REQUESTED | BLOCKED |
| FALLBACK_RECEIVED | IN_PROGRESS |
| RECIPE_REQUESTED | IN_PROGRESS |
| RECIPE_RECEIVED | COMPLETED |
| TERMINATED | COMPLETED |

## Registration

Ledgers must be registered for auto-sync to work:

```python
from lifecycle_hooks import register_ledger_for_session

register_ledger_for_session(user_prompt_key, ledger)
```

The `user_prompt` key is typically `f"{user_id}_{prompt_id}"`.

## FlowState

In addition to per-action states, the `FlowState` enum tracks flow-level lifecycle:

| State | Description |
|-------|-------------|
| `DEPENDENCY_ANALYSIS` | Analyzing dependencies between actions |
| `TOPOLOGICAL_SORT` | Sorting actions by dependency order |
| `SCHEDULED_JOBS_CREATION` | Creating scheduled execution jobs |
| `FLOW_RECIPE_CREATION` | Generating the flow recipe |

## See Also

- [../architecture/smart-ledger.md](../architecture/smart-ledger.md) -- SmartLedger details
- [architecture.md](architecture.md) -- System architecture
