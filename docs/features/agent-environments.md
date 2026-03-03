# Agent Environments

**File:** `core/platform/agent_environment.py`

## Overview

Logical scopes for AI agent workloads. NOT containers -- lightweight, fast,
with tool gating, budget limits, and scoped event emission. Think Android's
Context but for agents.

## EnvironmentConfig (dataclass)

Defines boundaries, permissions, and resource limits for an environment.
All fields are optional -- unconfigured fields impose no constraints.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `working_dir` | str | '' | Filesystem scope |
| `allowed_tools` | list | [] | Whitelist (empty = allow all) |
| `denied_tools` | list | [] | Blacklist (takes precedence over allowed) |
| `model_policy` | str | 'local_preferred' | Model selection policy |
| `max_cost_spark` | float | 0.0 | Budget cap (0 = unlimited) |
| `ai_capabilities` | list | [] | Required AI capabilities |
| `event_scope` | str | '' | EventBus topic prefix |
| `timeout_seconds` | float | 0.0 | Max execution time (0 = no limit) |
| `metadata` | dict | {} | Arbitrary key-value pairs |

## AgentEnvironment (dataclass)

A single agent execution environment with these core methods:

### Tool Gating

```python
env.check_tool('web_search')   # True (in allowed_tools)
env.check_tool('write_file')   # False (not in allowed_tools)
```

Precedence: denied_tools > allowed_tools > allow all. Empty lists impose no constraints.

### Budget Control

```python
env.check_budget(5.0)   # True if cost_spent + 5.0 <= max_cost_spark
env.record_cost(5.0)    # Track expenditure
```

### Scoped Inference

```python
result = env.infer('Summarize this paper', model_type='llm')
```

Dispatches through ModelBusService with the environment's `model_policy`.
Returns error dict if environment is inactive or service unavailable.

### Scoped Events

```python
env.emit('task.completed', {'result': 'done'})
# Publishes: 'env.<env_id>.task.completed'
```

### Lifecycle

```python
env.deactivate()      # Mark inactive (infer() returns error after this)
env.active            # Property: True/False
```

## EnvironmentManager

Manages environment lifecycle. Registered in ServiceRegistry as `'environments'`.
Thread-safe via `threading.Lock`.

| Method | Description |
|--------|-------------|
| `create(name, config=None, **kwargs)` | Create a new environment |
| `get(env_id)` | Retrieve by ID |
| `destroy(env_id)` | Deactivate and remove |
| `list_environments()` | List all (as dicts) |
| `count()` | Number of managed environments |
| `health()` | ServiceRegistry health report |

Emits `environment.created` and `environment.destroyed` events.

## SDK Usage

```python
from hart_sdk import environments

env = environments.create('research',
    allowed_tools=['web_search', 'read_file'],
    max_cost_spark=50.0,
    model_policy='local_preferred')

if env.check_tool('web_search'):
    result = env.infer('Find recent papers on transformers')

environments.destroy(env.env_id)
```

## See Also

- [ai-capabilities.md](ai-capabilities.md) -- AI capability declarations
- [platform-layer.md](platform-layer.md) -- Core platform services
