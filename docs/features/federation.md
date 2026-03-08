# Federation & Gossip

Decentralized peer discovery and hierarchical data synchronization across the HART OS network.

## Peer Discovery

- **UDP auto-discovery**: Nodes broadcast presence on the local network via UDP.
- **Gossip protocol**: Peer lists propagate through signed gossip messages. Each node forwards known peers to its neighbors, enabling organic network growth without a central registry.
- **Ed25519 signed messages**: All gossip messages are signed with the node's Ed25519 key to prevent spoofing and ensure message integrity.

## Hierarchical Sync (SyncQueue)

Data flows upward through a three-tier hierarchy:

```
local node --> regional node --> central instance
```

The **SyncQueue** batches changes and pushes them to the next tier. Conflict resolution uses last-write-wins with vector clocks for ordering.

## Peer Attestation and Fraud Detection

- Peers periodically attest to each other's availability and behavior.
- Anomalous patterns (sudden score inflation, impossible uptime claims, mismatched inference counts) trigger fraud flags.
- Flagged peers are excluded from task dispatch and revenue distribution until cleared.

## Federation Guardrails

The `HiveCircuitBreaker` in `security/hive_guardrails.py` enforces that the network only federates with hiveminds that share the core principle: **humans are always in control**. Nodes that fail attestation or present invalid certificates are refused federation.

## Source Files

- `integrations/social/peer_discovery.py`
- `integrations/agent_engine/federated_aggregator.py`
- `security/hive_guardrails.py`
- `security/key_delegation.py` (3-tier certificate chain)
