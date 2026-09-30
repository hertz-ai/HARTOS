# Agent-to-Agent (A2A) Protocol

HART OS implements the A2A protocol for inter-agent communication, allowing agents to discover each other and exchange messages via a standard JSON-RPC interface.

## Agent Discovery

### GET /a2a/{agent_id}/.well-known/agent.json

Returns the Agent Card for a registered agent.

```json
{
  "name": "Research Agent",
  "description": "Researches topics and produces summaries",
  "url": "https://hevolve.ai/a2a/research_agent",
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

### Who may call it

`message/send` runs a turn as the agent's owner, and `message/get` /
`task/cancel` read and end those turns, so all three are admitted when
EITHER the node's `/chat` gate admits the caller (this machine's own
callers, LAN-trusted tiers, `X-API-Key` / Bearer JWT, a phone the owner
allowed) OR the body is signed by a node this node has VERIFIED: its
`PeerNode` row (written by the gossip admission gate) is
`integrity_status='verified'`, which only an answered integrity challenge
writes (guardrail hash re-checked live; a failed or undecided code-hash
check withholds or revokes it), and no ban is in force. A row the open
announce created ('unverified'), a self-reported hash ('claimed') or a
fraud score over 40 ('suspicious') does not run anything. A peer signs with
the Ed25519 key it already gossips under; there is no other credential:

```json
{
  "jsonrpc": "2.0", "method": "message/send", "params": {"...": "..."},
  "id": "req-001",
  "agent_id": "<the agent_id in the URL>",
  "sender": {"node_id": "<gossip node_id>", "public_key": "<hex>"},
  "audience": "<the receiving node's node_id>",
  "timestamp": 1790000000,
  "signature": "<hex Ed25519 over every field but 'signature'>"
}
```

The key must be the one on file for `node_id`, the timestamp within
`WITNESS_TIMESTAMP_MAX_AGE` (60 s) of the receiver's clock, and a signature
is admitted once. `integrations/google_a2a/peer_reuse.py::invoke_peer_agent`
signs through `integrations/social/discovery.py::signed_peer_request`; the
receiver's rule is `discovery.admitted_peer_sender`. Only agents the node
shares (`peer_reuse.export_allowed`) are served. A refusal carries the gate's
own status (401, or 403 `consent_pending` for a phone awaiting the owner).

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
