# Task Delegation Flow

HART OS decomposes user prompts into a hierarchical task tree. Each level adds specificity until tasks are small enough for individual execution.

## Decomposition Hierarchy

```
User Prompt
├── Flow 1 (Persona A)
│   ├── Action 1
│   ├── Action 2
│   └── Action 3
└── Flow 2 (Persona B)
    ├── Action 1
    └── Action 2
```

### Flows

A flow represents a distinct persona or approach to part of the problem. Each flow has:

- A persona (e.g., "Research Analyst", "Code Writer")
- A set of ordered actions
- Its own recipe file when saved

### Actions

An action is the smallest executable unit. Each action goes through the ActionState machine (ASSIGNED -> IN_PROGRESS -> COMPLETED). Actions within a flow may execute sequentially or in parallel depending on dependency analysis.

## Dispatch Pipeline

### Step 1: Goal Submission

User prompt arrives at `/chat`. If the task requires decomposition:

```python
# In dispatch.py
tasks = _decompose_goal(prompt, goal_id, goal_type, user_id)
```

### Step 2: Decomposition

`_decompose_goal()` uses LLM to break the prompt into sub-tasks. For SmartLedger-integrated decomposition:

```python
# In parallel_dispatch.py
tasks, ledger = decompose_goal_to_ledger(prompt, goal_id, goal_type, user_id)
```

This creates a SmartLedger with tasks that have dependency relationships.

### Step 3: Distributed Dispatch

For multi-node execution, the coordinator submits the goal:

```python
distributed_goal_id = coordinator.submit_goal(
    goal_id,
    decomposed_tasks=tasks,
)
```

Tasks are distributed across peer nodes based on:

- Node capability tier (GPU, CPU, model availability)
- Compute policy (local_preferred, cloud_preferred)
- Current load and VRAM availability

### Step 4: Execution

Each node executes its assigned tasks through the CREATE or REUSE pipeline:

- **CREATE mode:** LLM executes the action, result saved as recipe
- **REUSE mode:** Saved recipe replayed without LLM (90% faster)

### Step 5: State Tracking

The ActionState machine tracks each action. State changes auto-sync to SmartLedger:

```
ASSIGNED --> IN_PROGRESS --> STATUS_VERIFICATION_REQUESTED --> COMPLETED
```

### Step 6: Recipe Storage

Completed flows produce recipe files:

```
prompts/{prompt_id}.json                           # Prompt definition
prompts/{prompt_id}_{flow_id}_recipe.json          # Flow recipe
prompts/{prompt_id}_{flow_id}_{action_id}.json     # Action recipe
```

## Speculative Dispatch

When `speculative: true` is passed to `/chat`:

1. Fast response returned immediately from cached/simple model
2. Background expert execution scheduled
3. Expert result available on next query or via polling

## Autonomous Fallback

When `autonomous: true`, the StatusVerifier LLM auto-generates context-aware fallback strategies instead of asking the user. This enables fully autonomous agent operation.

## See Also

- [smart-ledger.md](smart-ledger.md) -- Task persistence
- [nested-tasks.md](nested-tasks.md) -- Nested task system
- [overview.md](overview.md) -- System overview
