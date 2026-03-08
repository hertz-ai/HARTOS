# Recipe Pattern

The core innovation of HART OS: learn a task once, then replay it without repeated LLM calls.

## Two Modes

### CREATE Mode

1. User input is decomposed into hierarchical **flows** and **actions**.
2. Each action is executed against the appropriate tool or LLM.
3. The full execution trace is saved as a **recipe** for future reuse.

### REUSE Mode

1. A matching recipe is loaded from disk.
2. Each step is replayed deterministically -- no LLM calls required.
3. Achieves approximately **90% faster** execution compared to CREATE mode.

## Hierarchical Task Decomposition

```
User Prompt
+-- Flow 1 (Persona A)
|   +-- Action 1
|   +-- Action 2
|   +-- Action 3
+-- Flow 2 (Persona B)
    +-- Action 1
    +-- Action 2
```

## ActionState Machine

```
ASSIGNED --> IN_PROGRESS --> STATUS_VERIFICATION_REQUESTED --> COMPLETED
                                                          \--> ERROR --> TERMINATED
```

States auto-sync to the SmartLedger for persistence across sessions.

## Recipe Storage

| Path Pattern | Contents |
|--------------|----------|
| `prompts/{prompt_id}.json` | Prompt definition |
| `prompts/{prompt_id}_{flow_id}_recipe.json` | Trained recipe for a flow |
| `prompts/{prompt_id}_{flow_id}_{action_id}.json` | Individual action recipe |

## Autonomous Fallback

When an action enters the ERROR state, the StatusVerifier LLM auto-generates a context-aware fallback strategy. No user prompts are required for fallback, enabling fully autonomous agents.

## Source Files

- `create_recipe.py` -- Agent creation, action execution, recipe generation.
- `reuse_recipe.py` -- Recipe loading and trained agent execution.
- `helper.py` -- Action class, JSON utilities, tool handlers.
- `lifecycle_hooks.py` -- ActionState machine, ledger sync.
