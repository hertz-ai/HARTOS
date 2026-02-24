# Agent-to-Agent (A2A) Protocol

HART OS implements the A2A protocol for inter-agent communication, allowing agents to discover each other and exchange messages via a standard JSON-RPC interface.

## Agent Discovery

### GET /a2a/{agent_id}/.well-known/agent.json

Returns the Agent Card for a registered agent.

```json
{
  "name": "Research Agent",
  "description": "Researches topics and produces summaries",
  "url": "http://localhost:6777/a2a/research_agent",
  "version": "1.0.0",
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "research",
      "name": "Research",
      "description": "Research any topic"
    }
  ]
}
```

If the agent is not registered, returns 404.

## Message Exchange

### POST /a2a/{agent_id}/jsonrpc

JSON-RPC 2.0 endpoint for A2A messages.

### Supported Methods

#### message/send

Send a message to an agent.

```json
{
  "jsonrpc": "2.0",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [
        {"type": "text", "text": "Research renewable energy trends"}
      ]
    }
  },
  "id": "req-001"
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "result": {
    "id": "task-abc123",
    "status": "completed",
    "artifacts": [
      {
        "parts": [
          {"type": "text", "text": "Here are the latest trends..."}
        ]
      }
    ]
  },
  "id": "req-001"
}
```

#### message/get

Retrieve the status/result of a previously sent message.

#### task/cancel

Cancel a running task.

## Dynamic Agent Registry

Agents are registered dynamically via `A2AProtocolServer.register_agent()`. Each registered agent gets:

- An Agent Card at `/.well-known/agent.json`
- A JSON-RPC handler for message processing
- An executor function that maps to the CREATE/REUSE pipeline

The registry is managed by `integrations/google_a2a/google_a2a_integration.py`.

## Integration with HART OS

A2A agents connect to the core CREATE/REUSE pipeline:

```
External Agent → A2A JSON-RPC → Executor Function → /chat pipeline → Response
```

The `external_bot_bridge.py` module also probes `/.well-known/agent.json` on gateway URLs to detect A2A-compatible bots during federation.

## See Also

- [core.md](core.md) -- Core chat API
- [agent-engine.md](agent-engine.md) -- Goal engine
