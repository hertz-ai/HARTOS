# Federation Protocol

HART OS nodes form a decentralized network using gossip-based peer discovery, signed messages, and hierarchical synchronization.

## Gossip Protocol

**File:** `integrations/social/peer_discovery.py`

Nodes discover each other by exchanging peer lists periodically. No central registry is required.

### Gossip Cycle

1. Each node maintains a list of known peers
2. Every gossip interval, select `fanout` random peers
3. Send signed peer list to selected peers
4. Receive and merge peer lists from other nodes
5. New nodes propagate automatically through the network

### Bandwidth Profiles

Auto-selected by node capability tier, overridable via `HEVOLVE_GOSSIP_BANDWIDTH`:

| Profile | Gossip Interval | Health Interval | Fanout | Payload |
|---------|----------------|-----------------|--------|---------|
| `full` | 60s | 120s | 3 | Full JSON |
| `constrained` | 300s | 600s | 2 | Compact JSON |
| `minimal` | 900s | 1800s | 1 | msgpack (~60% smaller) |

### Tier-to-Profile Mapping

| Capability Tier | Profile |
|----------------|---------|
| `embedded` | minimal |
| `observer`, `lite` | constrained |
| `standard`, `full`, `compute_host` | full |

### Peer Health

| Status | Threshold |
|--------|-----------|
| `active` | Recent heartbeat |
| `stale` | No heartbeat for stale_threshold (5-45 min) |
| `dead` | No heartbeat for dead_threshold (15-120 min) |

## Seed Peers

New nodes bootstrap by connecting to seed peers configured via `HEVOLVE_SEED_PEERS` environment variable. Once connected to any seed, the gossip protocol discovers the rest of the network.

## Auto-Discovery (UDP)

Local network discovery uses UDP broadcast for nodes on the same subnet. This allows Nunba instances to find each other without external seed peers.

## SyncQueue (Hierarchical)

**Table:** `sync_queue` in `integrations/social/models.py`

For tiered networks (central > regional > local), changes propagate through the hierarchy:

1. Local node makes a change (post, comment, vote)
2. Change queued in `SyncQueue` with target tier
3. Regional host pulls changes from local nodes
4. Central pulls changes from regional hosts
5. Changes flow back down to ensure consistency

## Signed Messages

All gossip messages are Ed25519-signed:

1. Node generates an Ed25519 keypair at first boot
2. Public key stored in `PeerNode.public_key`
3. Every gossip payload includes a signature
4. Receiving nodes verify signature before accepting data

### Gossip Payload Fields

Full mode includes all fields. Compact mode (`constrained` profile) includes only:

```
node_id, url, public_key, guardrail_hash, code_hash,
signature, tier, capability_tier, timestamp
```

## Peer Attestation

**Tables:** `node_attestations`, `integrity_challenges`

Peers verify each other's integrity:

1. Challenger sends a random challenge to target node
2. Target computes response using its code hash
3. Challenger verifies response matches expected hash
4. Attestation recorded in `node_attestations`
5. Mismatches trigger `FraudAlert`

### Guardrail Hash Verification

Nodes compute a SHA-256 hash of all guardrail values. This hash is included in gossip payloads. Peers reject nodes with mismatched guardrail hashes -- ensuring all nodes enforce the same safety constraints.

## Certificate Chain

See [../developer/security.md](../developer/security.md) for the 3-tier certificate chain (central > regional > local).

## See Also

- [overview.md](overview.md) -- Network topology diagram
- [nested-tasks.md](nested-tasks.md) -- Distributed task execution
- [../developer/security.md](../developer/security.md) -- Security model
