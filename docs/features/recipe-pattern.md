# Recipe Pattern

The core innovation of HART OS: learn a task once, then replay it without repeated LLM calls.

## Two Modes

### CREATE Mode

1. User input is decomposed into hierarchical **flows** and **actions**.
2. Each action is executed against the appropriate tool or LLM.
3. The full execution trace is saved as a **recipe** for future reuse.

### REUSE Mode

1. A matching recipe is loaded from disk.
2. The proven steps are replayed as an **LLM-GUIDED** path: the agent follows the recipe
   and **adapts each step to the live screen/context** (`reuse_recipe.py` — "Adapt these
   steps to the current screen state as needed"). It is **NOT** a deterministic macro.
   Intelligence stays in the loop; a pure code replay would break the instant the world
   differs and would not be intelligence at all.
3. The speedup (~**90% fewer/cheaper LLM calls** vs CREATE) comes from **skipping
   re-decomposition + exploration + re-verification** — the expensive reasoning CREATE
   already paid for — **not** from removing the LLM.

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
