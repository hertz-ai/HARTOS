# Agent Ledger v2.0 — Integration Guide

How to properly use the Agent Ledger for any agent. Covers the full task lifecycle, what's wired into HARTOS, and what each feature costs in overhead.

---

## 1. Core Concept

The Agent Ledger is a **persistent task tracking system** that gives any agent reliable memory across sessions. Every task has a state, an owner, a heartbeat, an integrity hash, and a complete audit trail.

```
CREATE: User Input → Decompose into Actions → Each Action = 1 Ledger Task
REUSE:  Load Recipe → Each Step = 1 Ledger Task (pre-assigned from recipe)
```

The ledger does NOT control execution — it **observes** execution. The pipeline (`create_recipe.py`, `reuse_recipe.py`) drives the while loop. The ledger records what happened, who owns what, and whether deadlines were met.

---

## 2. Task Lifecycle (15 States)

```
                              ┌──────────────┐
                              │   DEFERRED    │ ←── intentionally postponed
                              │  (undefer)    │
                              └──────┬───────┘
                                     │ undefer
                                     ▼
┌──────────┐   start    ┌──────────────┐   complete   ┌───────────┐
│  PENDING  │──────────→│ IN_PROGRESS   │────────────→│ COMPLETED  │
└──────────┘            └──────┬───────┘              └─────┬─────┘
     │                         │                            │ rollback
     │ delegate                │ block/pause/stop           ▼
     ▼                         ▼                     ┌───────────┐
┌──────────┐            ┌──────────┐                 │ROLLED_BACK│
│DELEGATED │            │ BLOCKED  │                 └───────────┘
│          │            │ PAUSED   │
│          │            │USER_STOP │
└──────────┘            └────┬─────┘
                             │ resume
                             ▼
                        ┌──────────┐
                        │ RESUMING │──→ IN_PROGRESS
                        └──────────┘

Terminal states: COMPLETED, FAILED, CANCELLED, TERMINATED, SKIPPED, NOT_APPLICABLE, ROLLED_BACK
```

### State Transition Rules (enforced by `_validate_transition`)

| From | Allowed To |
|------|-----------|
| PENDING | IN_PROGRESS, PAUSED, CANCELLED, SKIPPED, NOT_APPLICABLE, DEFERRED, DELEGATED |
| DEFERRED | PENDING, IN_PROGRESS, CANCELLED, SKIPPED, NOT_APPLICABLE |
| IN_PROGRESS | COMPLETED, FAILED, PAUSED, USER_STOPPED, BLOCKED, TERMINATED, NOT_APPLICABLE, DELEGATED |
| DELEGATED | COMPLETED, FAILED, IN_PROGRESS, CANCELLED, BLOCKED |
| PAUSED | RESUMING, CANCELLED, TERMINATED, NOT_APPLICABLE, SKIPPED, DEFERRED |
| USER_STOPPED | RESUMING, CANCELLED, TERMINATED, NOT_APPLICABLE, SKIPPED, DEFERRED |
| BLOCKED | PENDING, RESUMING, FAILED, CANCELLED, NOT_APPLICABLE, DEFERRED |
| RESUMING | IN_PROGRESS, PAUSED, FAILED |
| COMPLETED | ROLLED_BACK (special case) |
| Any terminal | Nothing (terminal is final) |

Invalid transitions are **rejected** with a warning log and return `False`. They never crash.

---

## 3. How HARTOS Wires the Ledger

### 3.1 Automatic — You Get This for Free

These features work automatically via `lifecycle_hooks.py`. No code needed from the agent.

#### ActionState → LedgerTaskStatus Mapping

Every `safe_set_state()` call in the pipeline triggers `_auto_sync_to_ledger()` which maps:

| ActionState | Ledger Status | Blocked Reason |
|------------|---------------|----------------|
| ASSIGNED | PENDING | — |
| IN_PROGRESS | IN_PROGRESS | — |
| STATUS_VERIFICATION_REQUESTED | IN_PROGRESS | — |
| COMPLETED | COMPLETED | — |
| PENDING (stuck) | BLOCKED | dependency |
| ERROR | FAILED | — |
| FALLBACK_REQUESTED | BLOCKED | input_required |
| FALLBACK_RECEIVED | IN_PROGRESS | — (cleared) |
| RECIPE_REQUESTED | IN_PROGRESS | — |
| RECIPE_RECEIVED | COMPLETED | — |
| TERMINATED | COMPLETED | — |
| EXECUTING_MOTION | IN_PROGRESS | — |
| SENSOR_CONFIRM | IN_PROGRESS | — |
| PREVIEW_PENDING | BLOCKED | approval_required |
| PREVIEW_APPROVED | IN_PROGRESS | — (cleared) |

#### Ownership (claim/release)

- **Claimed** automatically when task enters IN_PROGRESS — sets `owner_node_id` (hostname), `owner_user_id`, `owner_prompt_id`
- **Released** automatically when task enters any terminal state
- **Time recorded** — elapsed seconds from `started_at` to terminal state stored in `time_spent_s`
- **Cannot double-claim** — `task.claim()` returns False if already owned

#### Heartbeat

- Updated on **every** state transition via `_auto_sync_to_ledger()`
- Updated on **every while loop iteration** in `create_recipe.py` and `reuse_recipe.py`
- Stored in `task.last_heartbeat_at` (ISO timestamp)
- Check staleness: `task.is_heartbeat_stale()` — returns True if no heartbeat for 3× `heartbeat_interval_s` (default 90s)

#### SLA Breach Detection + Notification

- Checked on every state transition
- If `task.is_sla_breached()` (deadline passed OR elapsed > `sla_target_s`):
  - `task.sla_breached` flag set to True (idempotent)
  - Status message posted: "SLA breached — requesting status update from agent"
  - EventBus event emitted: `task.sla_breached` with `action: 'status_request'`
  - Advisory only — **never blocks** execution
- Agent daemon (`agent_daemon.py`) can listen for `task.sla_breached` events to prompt the agent

#### Budget Enforcement

- Checked on **every while loop iteration** in `create_recipe.py` and `reuse_recipe.py`
- If `task.is_budget_exhausted()` (spark_spent >= spark_budget OR time_spent_s >= time_budget_s OR elapsed >= timeout_s):
  - Status message posted: "Budget exhausted — aborting"
  - ActionState set to ERROR
  - While loop **breaks** — execution stops
- This is the only ledger feature that **actively stops** execution

#### Integrity Verification (LLM Hallucination Defense)

- Every task gets `seal_integrity()` at creation in `create_ledger_from_actions()`
- SHA-256 hash of core fields (task_id, description, type, prerequisites, context, priority)
- Before accepting LLM completion claims, pipeline checks:
  1. LLM-claimed action_id matches pipeline's `current_action_id`
  2. Task exists in ledger
  3. Task is not already in a terminal state
  4. `task.verify_integrity()` — hash hasn't changed
- If any check fails → LLM claim rejected, agent told to continue working

#### Audit Trail

- Every state transition logged to `security/immutable_audit_log.py` (SHA-256 hash chain)
- Every state transition broadcast to EventBus (`action_state.changed`)

### 3.2 Manual — Call These When Needed

These require explicit calls from your agent code.

#### Block for User Input

When your agent needs user consent before proceeding:

```python
from lifecycle_hooks import block_for_user_input, resume_from_user_input

# Agent needs user approval (e.g., destructive action)
block_for_user_input(user_prompt, current_action_id, "Need user to approve file deletion")
# Task transitions: IN_PROGRESS → BLOCKED (blocked_reason='input_required')

# Later, when user responds:
resume_from_user_input(user_prompt, current_action_id, "User approved deletion")
# Task transitions: BLOCKED → RESUMING → IN_PROGRESS
```

#### Progress Reporting

```python
task = ledger.get_task("action_1")
task.update_progress(45.0, checkpoint="Data extraction complete")
task.post_status("Processing 1500 records", progress_pct=60.0, metadata={"records": 1500})
```

`post_status()` is bounded — keeps last 50 messages to prevent unbounded growth.

#### Delegation

```python
# Delegate to another agent (e.g., expert agent)
ledger.delegate_task("action_3", to_agent_id="coding_expert", delegation_type="escalation")
# Task: IN_PROGRESS → DELEGATED

# When delegate finishes:
ledger.complete_delegation("action_3", result={"code": "..."})
# Task: DELEGATED → COMPLETED

# Or reclaim if delegation failed:
ledger.reclaim_delegation("action_3", reason="Expert unavailable")
# Task: DELEGATED → IN_PROGRESS
```

#### Defer

```python
# Postpone a task for later
ledger.defer_task("action_5", reason="API rate limited", until="2026-03-12T10:00:00")
# Task: PENDING → DEFERRED

# Later, bring it back
ledger.undefer_task("action_5")
# Task: DEFERRED → PENDING
```

#### Rollback

```python
# Undo a completed task
ledger.rollback_task("action_2", reason="Output was incorrect")
# Task: COMPLETED → ROLLED_BACK (original_result preserved)
```

---

## 4. BLOCKED State — Decision Authority

Three things can BLOCK a task. Each uses a different `BlockedReason`:

| Source | When | BlockedReason | Who Decides |
|--------|------|---------------|-------------|
| Recipe config | `can_perform_without_user_input: "no"` on action | `input_required` | Decided at CREATE time when recipe is built |
| Action classifier | Destructive op detected (ActionState.PREVIEW_PENDING) | `approval_required` | Runtime — `security/action_classifier.py` |
| Agent itself | Agent calls `send_message_to_user` and flow stops | `input_required` | Runtime — LLM decides it needs user input |
| Prerequisites | Prerequisite task not completed | `dependency` | Automatic — ledger dependency graph |
| Resource | GPU/VRAM unavailable | `resource_unavailable` | Runtime — compute check |
| Rate limit | API rate limited | `rate_limited` | Runtime — rate limiter |
| External | Waiting for external service response | `external_service` | Runtime — agent decision |
| Manual | User/admin manually blocked | `manual_block` | Explicit user action |

### How BLOCKED Tasks Get Resumed

1. **Dependency completed** → Auto-resume via `_handle_task_completion()`. When a prerequisite completes, all blocked dependents are checked. If `blocked_by` list is empty, task auto-resumes to IN_PROGRESS.

2. **User responds** → `resume_from_user_input()` or ActionState.PREVIEW_APPROVED. Agent daemon sends HITL notifications for `approval_required` blocked tasks.

3. **Resource available** → Agent checks `task.can_run_on(capabilities)` and calls `ledger.resume_task()`.

4. **Manual** → `ledger.resume_task(task_id, reason="Manually resumed by admin")`.

---

## 5. SLA and Deadlines

### Setting SLA

```python
task = Task("action_1", "Process data", TaskType.PRE_ASSIGNED)

# Option A: Relative — complete within 300 seconds of starting
task.sla_target_s = 300.0

# Option B: Absolute — complete before this deadline
task.deadline = "2026-03-11T18:00:00"

# Option C: Hard timeout — budget enforcement kills execution
task.timeout_s = 600.0  # 10 minutes max

# These are independent. SLA is advisory. timeout_s is enforced.
```

### SLA Breach Notification Flow

```
Task starts (IN_PROGRESS)
    │
    ├── heartbeat() every loop iteration
    │
    ├── SLA check on every state transition
    │   └── elapsed > sla_target_s OR now > deadline?
    │       ├── YES → task.sla_breached = True
    │       │        → post_status("SLA breached — requesting status update")
    │       │        → emit_event('task.sla_breached', {action: 'status_request'})
    │       │        → Agent daemon picks up event → can prompt agent for status
    │       │        → Execution continues (advisory only)
    │       └── NO  → continue normally
    │
    ├── Budget check every loop iteration
    │   └── time_spent_s >= time_budget_s OR spark_spent >= spark_budget?
    │       ├── YES → ERROR state → while loop breaks → execution stops
    │       └── NO  → continue
    │
    └── Terminal state → release ownership, record time_spent_s
```

**Key distinction:**
- `sla_target_s` / `deadline` → Advisory. Flags breach, emits event, never blocks.
- `timeout_s` / `time_budget_s` / `spark_budget` → Enforced. Breaks the execution loop.

---

## 6. Ownership Model

Three separate fields, not one:

| Field | Purpose | Set By |
|-------|---------|--------|
| `owner_node_id` | Which machine runs this task | `platform.node()` — auto on IN_PROGRESS |
| `owner_user_id` | Which user's session owns this | Extracted from `user_prompt` (format: `{user_id}_{prompt_id}`) |
| `owner_prompt_id` | Which prompt/session context | Extracted from `user_prompt` |

### Ownership Lifecycle

```
PENDING (no owner)
    │ → IN_PROGRESS
    ▼
task.claim(node_id, user_id, prompt_id)
    │ → ownership_history records "claimed"
    │
    │ ... task executes ...
    │
    │ → COMPLETED/FAILED/CANCELLED/TERMINATED
    ▼
task.release()
    │ → ownership_history records "released"
    │ → time_spent_s recorded
    │ → owner fields cleared
```

### Transfer (for hive distribution)

```python
task.transfer(node_id="peer_node_2", user_id="remote_user")
# Atomic: release old → claim new, recorded in ownership_history
```

### Double-Claim Protection

`task.claim()` returns `False` if already owned. Prevents two nodes from grabbing the same task. In a distributed setup, use `DistributedTaskLock` (Redis) for proper locking.

---

## 7. Locality and Sensitivity

Controls whether a task can leave the local node:

```python
task = Task("action_1", "Process medical records", TaskType.PRE_ASSIGNED)
task.locality = TaskLocality.LOCAL_ONLY.value     # Never leaves this node
task.sensitivity = TaskSensitivity.CONFIDENTIAL.value  # Only trusted nodes
```

| Locality | Meaning |
|----------|---------|
| LOCAL_ONLY | Must stay on this node |
| REGIONAL | Can run within same region (shared Redis cluster) |
| GLOBAL | Can run on any hive node (default) |

| Sensitivity | Meaning |
|-------------|---------|
| PUBLIC | Data can be shared freely (default) |
| INTERNAL | Stays within the org/hive |
| CONFIDENTIAL | Only on trusted nodes |
| SECRET | Never leaves originating node |

`task.can_distribute()` → False only if LOCAL_ONLY or SECRET. Everything else is distributable.

---

## 8. Integrity — Corruption Detection

```python
# At creation (automatic in create_ledger_from_actions):
task.seal_integrity()
# Computes SHA-256 of (task_id, description, type, prerequisites, context, priority)
# Stored in task.data_hash

# Before accepting LLM claims (automatic in pipeline):
if not task.verify_integrity():
    # Hash mismatch — task data was corrupted/tampered
    # Reject LLM claim, log warning
```

**What this catches:**
- LLM trying to modify task description to change its own instructions
- Corrupted JSON files
- Race conditions where two processes modify the same task

**What this does NOT catch:**
- Status changes (timestamps, state_history not hashed — they're supposed to change)
- Result modifications (result is added after creation)

---

## 9. Storage Backends

| Backend | When to Use | Overhead |
|---------|------------|----------|
| `JSONBackend` | Development, single-node | ~1-5ms per save, file I/O |
| `InMemoryBackend` | Testing | ~0ms, no persistence |
| `RedisBackend` | Production, multi-node | ~0.1-0.5ms, requires Redis |
| `MongoDBBackend` | Large scale, complex queries | ~1-3ms, requires MongoDB |
| `PostgreSQLBackend` | Enterprise, ACID guarantees | ~0.5-2ms, requires PostgreSQL |

```python
# Auto-detect best available backend:
from agent_ledger import create_production_ledger
ledger = create_production_ledger(agent_id="my_agent", session_id="session_1")
# Tries Redis → MongoDB → PostgreSQL → JSON fallback
```

---

## 10. Distributed Features (Optional, Require Redis)

These are NOT required for single-node operation. Only enable if you run multi-node hive.

### PubSub — Cross-Node Notifications

```python
import redis
r = redis.Redis()
ledger.enable_pubsub(r)
# Now task_completed, task_delegated events broadcast to all nodes
```

### Heartbeat — Agent Liveness

```python
ledger.enable_heartbeat(r, host_info={"hostname": "node1", "gpu": True})
# Background thread pings Redis every 30s
# Other nodes can check: is this agent alive?
```

### Distributed Lock — Prevent Double-Claim

```python
from agent_ledger import DistributedTaskLock
lock = DistributedTaskLock(r, "my_agent")
if lock.acquire_task_lock("action_5"):
    # Safe to claim and execute
    task.claim(node_id="this_node")
    # ... execute ...
    lock.release_task_lock("action_5")
```

### Result Verification — Cross-Node Validation

```python
from agent_ledger import TaskVerification
hash = TaskVerification.compute_result_hash(result)
# Store hash, other nodes can verify result wasn't tampered
```

---

## 11. What NOT to Do

### Don't set SLA on every task

SLA tracking has overhead (datetime comparison on every state transition). Only set `sla_target_s` or `deadline` on tasks where the deadline actually matters.

### Don't use PubSub without Redis

`enable_pubsub()` requires a Redis connection. If Redis is down, PubSub silently fails. Don't rely on it for critical coordination — use it for notifications only.

### Don't seal_integrity() on frequently modified tasks

`seal_integrity()` hashes core fields. If you modify `context` after sealing, `verify_integrity()` will return False. Only seal at creation (this is done automatically in `create_ledger_from_actions()`). Don't re-seal mid-execution unless you intentionally want to update the baseline.

### Don't block without a resume path

Every `block_for_user_input()` must have a corresponding `resume_from_user_input()`. Blocked tasks with no resume path stay blocked forever. The agent daemon checks for stale blocked tasks but can't auto-resume without user input.

### Don't fight the state machine

The transition rules exist to prevent impossible states. If `_validate_transition` rejects your transition, the state machine is telling you something. Don't try to force it — find the valid path.

```
# Wrong: trying to go FAILED → IN_PROGRESS
task.start()  # Returns False — FAILED is terminal

# Right: use ActionState retry (lifecycle_hooks layer)
# ActionState ERROR → IN_PROGRESS triggers a new ledger task or retry
```

---

## 12. Quick Reference — Complete API

### Task Methods

| Method | Transition | Returns |
|--------|-----------|---------|
| `task.start(reason)` | PENDING → IN_PROGRESS | bool |
| `task.complete(result, reason)` | IN_PROGRESS → COMPLETED | bool |
| `task.fail(error, reason)` | IN_PROGRESS/BLOCKED → FAILED | bool |
| `task.pause(reason)` | IN_PROGRESS → PAUSED | bool |
| `task.user_stop(reason)` | IN_PROGRESS → USER_STOPPED | bool |
| `task.block(reason)` | IN_PROGRESS → BLOCKED | bool |
| `task.resume(reason)` | PAUSED/BLOCKED/USER_STOPPED → RESUMING → IN_PROGRESS | bool |
| `task.cancel(reason)` | Most states → CANCELLED | bool |
| `task.terminate(reason)` | Most states → TERMINATED | bool |
| `task.skip(reason)` | Most states → SKIPPED | bool |
| `task.mark_not_applicable(reason)` | Most states → NOT_APPLICABLE | bool |
| `task.defer(reason, until)` | PENDING/PAUSED/BLOCKED → DEFERRED | bool |
| `task.undefer(reason)` | DEFERRED → PENDING | bool |
| `task.delegate(to_agent_id, type, reason)` | PENDING/IN_PROGRESS → DELEGATED | bool |
| `task.complete_delegation(result, reason)` | DELEGATED → COMPLETED | bool |
| `task.reclaim_delegation(reason)` | DELEGATED → IN_PROGRESS | bool |
| `task.rollback(reason)` | COMPLETED → ROLLED_BACK | bool |

### Task Properties

| Property/Method | What It Checks |
|----------------|---------------|
| `task.is_owned` | Has an owner (node_id or user_id set) |
| `task.is_terminal()` | In COMPLETED/FAILED/CANCELLED/TERMINATED/SKIPPED/NOT_APPLICABLE/ROLLED_BACK |
| `task.is_resumable()` | In PAUSED/USER_STOPPED/BLOCKED |
| `task.is_blocked()` | Has entries in `blocked_by` list |
| `task.is_budget_exhausted()` | spark_spent >= spark_budget OR time exceeded |
| `task.is_sla_breached()` | Elapsed > sla_target_s OR now > deadline |
| `task.is_stuck(threshold_s)` | In same state longer than threshold (default 300s) |
| `task.is_heartbeat_stale(threshold_s)` | No heartbeat for 3× heartbeat_interval_s |
| `task.can_distribute()` | Not LOCAL_ONLY and not SECRET |
| `task.can_run_on(capabilities)` | Compute requirements met |
| `task.verify_integrity()` | SHA-256 hash matches stored hash |

### Observability

| Method | What It Does |
|--------|-------------|
| `task.heartbeat(message)` | Updates `last_heartbeat_at` timestamp |
| `task.post_status(msg, progress_pct, metadata)` | Appends structured status message (bounded to 50) |
| `task.get_latest_status()` | Returns most recent status message |
| `task.update_progress(pct, checkpoint)` | Updates progress % and optional checkpoint |
| `task.record_spend(spark, time_s)` | Accumulates resource spend |
| `task.get_state_history()` | Returns complete state transition audit trail |
| `task.get_current_state_duration()` | Seconds in current state |

### SmartLedger Methods

| Method | What It Does |
|--------|-------------|
| `ledger.add_task(task)` | Add task, auto-saves |
| `ledger.get_task(task_id)` | Get task by ID |
| `ledger.get_next_task()` | Highest priority ready task |
| `ledger.get_ready_tasks()` | All tasks with prerequisites met |
| `ledger.get_progress_summary()` | Counts by status + progress % |
| `ledger.get_tasks_by_status(status)` | Filter by status |
| `ledger.get_active_tasks()` | IN_PROGRESS, RESUMING, DELEGATED |
| `ledger.get_paused_tasks()` | PAUSED, USER_STOPPED, BLOCKED |
| `ledger.get_resumable_tasks()` | Tasks that can be resumed |
| `ledger.cancel_task(id, cascade)` | Cancel + optionally cascade to dependents |
| `ledger.pause_all_active_tasks(reason)` | Bulk pause |
| `ledger.resume_all_paused_tasks(reason)` | Bulk resume |
| `ledger.create_parent_child_task(parent_id, desc)` | Create child task under parent |
| `ledger.create_sequential_tasks(descriptions)` | Auto-chain with prerequisites |
| `ledger.get_task_hierarchy()` | Full tree view |
| `ledger.get_dependency_status(task_id)` | Blocked by, dependents, ready state |

### lifecycle_hooks.py Helpers

| Function | When to Use |
|----------|------------|
| `register_ledger_for_session(user_prompt, ledger)` | At session start — enables auto-sync |
| `block_for_user_input(user_prompt, action_id, reason)` | When agent needs user consent |
| `resume_from_user_input(user_prompt, action_id, reason)` | When user responds |
| `safe_set_state(user_prompt, action_id, state, reason)` | Every state change — auto-syncs to ledger |

---

## 13. Minimal Integration Example

For any agent framework (AutoGen, LangChain, CrewAI, custom):

```python
from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, create_ledger_from_actions
from lifecycle_hooks import register_ledger_for_session, safe_set_state, ActionState

# 1. Create ledger from your workflow actions
actions = [
    {"action_id": 1, "description": "Gather requirements", "prerequisites": []},
    {"action_id": 2, "description": "Write code", "prerequisites": [1]},
    {"action_id": 3, "description": "Run tests", "prerequisites": [2]},
]
ledger = create_ledger_from_actions(user_id=42, prompt_id=99, actions=actions)

# 2. Register for auto-sync
user_prompt = "42_99"
register_ledger_for_session(user_prompt, ledger)

# 3. Execute — state transitions auto-sync to ledger
for action in actions:
    action_id = action["action_id"]

    safe_set_state(user_prompt, action_id, ActionState.IN_PROGRESS)
    # Ledger: PENDING → IN_PROGRESS, ownership claimed, heartbeat updated

    try:
        result = execute_action(action)  # Your execution logic
        safe_set_state(user_prompt, action_id, ActionState.COMPLETED)
        # Ledger: IN_PROGRESS → COMPLETED, ownership released, time recorded
    except Exception as e:
        safe_set_state(user_prompt, action_id, ActionState.ERROR, str(e))
        # Ledger: IN_PROGRESS → FAILED

# 4. Check progress
summary = ledger.get_progress_summary()
# {'total': 3, 'completed': 3, 'progress': '100.0%', ...}
```

That's it. Ownership, heartbeat, SLA, integrity, audit trail — all automatic.
