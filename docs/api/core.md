# Core API

The HART OS Flask server runs on port 6777 (via Waitress). All endpoints accept and return JSON.

## POST /chat

Primary endpoint for agent interaction.

### Request

```json
{
  "user_id": "12345",
  "prompt_id": "99999",
  "prompt": "Research the latest trends in renewable energy",
  "create_agent": false,
  "task_source": "own",
  "model_config": null,
  "autonomous": false,
  "speculative": false,
  "casual_conv": false
}
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `user_id` | Yes | -- | User identifier |
| `prompt_id` | Yes | -- | Prompt/conversation identifier (alphanumeric, `_`, `-`) |
| `prompt` | Yes | -- | The user's message |
| `create_agent` | No | `false` | Force CREATE mode (new recipe) |
| `task_source` | No | `own` | `own`, `hive`, or `idle` |
| `model_config` | No | `null` | Per-request model override (speculative execution) |
| `autonomous` | No | `false` | Enable autonomous fallback generation |
| `speculative` | No | `false` | Enable speculative dispatch |
| `casual_conv` | No | `false` | Casual conversation mode (no task decomposition) |

### Response

```json
{
  "response": "Here are the latest trends...",
  "prompt_id": "99999",
  "flow_id": "1",
  "actions_completed": 3,
  "recipe_saved": true
}
```

### Security

- Rate limited: 30 requests/minute per user/IP
- Guardrail pre-dispatch filtering on every call
- Secret redaction on user prompts
- Budget gate estimates LLM cost before execution

## POST /time_agent

Schedule a task for future execution.

```json
{
  "user_id": "12345",
  "prompt_id": "99999",
  "prompt": "Check stock prices at market open",
  "schedule_time": "2026-02-25T09:30:00Z"
}
```

## POST /visual_agent

Vision/computer-use agent endpoint.

```json
{
  "user_id": "12345",
  "prompt_id": "99999",
  "prompt": "Describe what you see in this image",
  "image_url": "https://example.com/image.png"
}
```

## POST /add_history

Add conversation history for an agent session.

```json
{
  "user_id": "12345",
  "prompt_id": "99999",
  "history": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"}
  ]
}
```

## GET /status

Health check endpoint.

```json
{
  "status": "running",
  "version": "1.0.0",
  "uptime_seconds": 3600
}
```

## GET /health

Lightweight health probe (for load balancers).

```json
{"status": "healthy"}
```

## GET /ready

Readiness probe (all subsystems initialized).

```json
{"ready": true}
```

## See Also

- [settings.md](settings.md) -- Compute configuration API
- [social.md](social.md) -- Social platform API
- [agent-engine.md](agent-engine.md) -- Agent engine API
