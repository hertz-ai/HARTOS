# The 10,023 pending ledger tasks are decomposed source code, not work

2026-08-18. This changes the reading of the "ledger never drains" finding.

## What the pending list actually contains

`GET /api/agent-engine/ledger/tasks?status=pending`, first five:

```
action_5: filters = [spec for spec in ENGINE_REGISTRY if spec.install_target == 'venv']
action_6: engine_ids = [spec.engine_id for spec in filters]
action_7: unhealthy_engine = None
action_8: for eid in engine_ids:
action_9:     healthy = tts.backend_venv.is_venv_healthy(eid)
```

Sequential ids, created ~10 ms apart (23:42:09.303 → .346), including a bare
`for` header and an indented continuation line.

Sampled 200 pending tasks:

| Classification | Count |
|---|---|
| Looks like source code | **176** |
| Looks like a real task | 24 |
| `task_id` shape `action_N` | **200 / 200** |

And the 24 "real-looking" ones are not tasks either — they include `break`
(a Python keyword) and bare function names: `verify_master_key`,
`check_recipe_reuse_rate`, `identify_stale_recipes`,
`verify_recipe_version_compatibility`.

So essentially the whole backlog is a code block split line-by-line and filed
as ledger tasks.

## Why this matters

The daemon reached `tick_count` 275 with `pending` unchanged at 10,023, and I
had recorded that as "the drain is broken". That framing is wrong. A task
whose description is `for eid in engine_ids:` is not dispatchable work — it
cannot be executed independently of the lines around it. There is nothing
meaningful for the dispatcher to do.

The queue is not stuck. It is full of entries that should never have been
created.

## Producer

`create_recipe.py:3883-3899`, inside
`create_action_with_ledger(actions: List[Dict], ...)` (line 3790):

```python
for action in actions:
    task_id = f"action_{action.get('action_id', 'unknown')}"
    if task_id not in ledger.tasks:
        task = Task(
            task_id=task_id,
            description=action.get('description', action.get('action', '')),
            task_type=TaskType.PRE_ASSIGNED,
            status=TaskStatus.PENDING,
            prerequisites=[f"action_{p}" for p in action.get('prerequisites', [])],
            ...
        )
```

Faithful to its input. The defect is upstream: whatever builds `actions` is
emitting per-line code fragments as plan steps.

Six call sites, all passing a flow-actions list:

```
create_recipe.py:4597, 4631, 4809   create_action_with_ledger(flow_actions, ...)
create_recipe.py:5706               create_action_with_ledger(next_flow_actions, ...)
create_recipe.py:5754               create_action_with_ledger(current_flow_actions, ...)
create_recipe.py:5898               create_action_with_ledger(...)
```

**Not yet traced:** where `flow_actions` is parsed, and why a code block ends
up split by newline into actions. That is the next hop and the actual fix
site. Do not patch `create_action_with_ledger` — it is doing what it was
asked.

## Consequences to re-check

- `#659` "ledger drain broken" should be re-scoped. The daemon ticking at 275
  with a static `pending` count is consistent with a malformed backlog, not
  necessarily a broken dispatcher. Whether dispatch ALSO has a defect is now
  unproven either way.
- `zombie_reaper` (registered with the scheduler at `create_recipe.py:424`)
  reaps stale `in_progress` rows. It does not touch `pending`, so this
  backlog is not something it was ever going to clear.
- Any "N tasks pending" figure shown to an operator or on the Live Agents
  dashboard is currently counting source lines.

## Ledger write path, for the record

`/api/agent-engine/ledger/*` is GET-only — `tasks`, `tasks/<id>`, `stats`.
There is no task-creation endpoint. Work enters via `POST /api/goals`
(and the `create_goal` MCP tool), which the system expands into ledger tasks.
So filing engineering findings as ledger tasks is not possible directly, and
filing them as goals would add to a backlog that currently cannot drain.
