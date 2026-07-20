# Unified Sync Architecture — one colocated sync layer, zero parallel paths

> Steward vision (2026-06-21): "All syncs should be colocated and centralised.
> social posts, communities, resonance, encounters, thought experiments, users,
> friends, community user join/leave, profiles, public agents — these are
> different things we need to sync, **peer-to-peer first, then central**, **without
> the assets** (assets are found from the peer first, then central when the peer
> is unavailable), with a **storage ceiling of 10 TB for all users in central**."

This document is the canonical design. Every sync edit must conform to it. The
goal is **ONE** sync registry/dispatcher/producer/transport — never a second.

## 1. The problem today: scattered parallel sync paths

| Concern | Where it lives now | Parallel-path smell |
|---|---|---|
| post/community content up-sync | `federation.sync_to_parent` → `sync_post` op → `_handle_sync_post` | C-series #146–149 |
| public-agent up-sync | `federation.sync_agent_to_parent` → `register_agent` op → `_handle_sync_agent` | bespoke twin of the above |
| user/profile/FCM down+up | `sync_user` op → `_handle_sync_user` (caches FCM via `store_local_fcm_token`) | bespoke again |
| dispatch | `SyncEngine.receive_sync_batch` — a hand-written `if op == … elif …` ladder | every new entity adds an `elif` |
| producer | one inline producer per entity (post create hook, register_agent hook, consent re-sync hook) | N producers, each re-deriving the gate |
| transport | up = HTTP POST `/api/social/hierarchy/sync`; down = (none / was an IDOR pull) | direction asymmetry |
| assets | `#149` CDN P2P-first→central-fallback (separate from entity sync) | correct already; keep separate |

Three near-identical receivers + N producers + a growing `if/elif` ladder = the
parallel paths to collapse.

## 2. The unified model: a `SyncEntity` registry (single source of truth)

Every syncable thing is one registry entry. **No per-entity branches anywhere.**

```python
@dataclass(frozen=True)
class SyncEntity:
    op: str                      # wire op name, e.g. 'sync_post', 'sync_user'
    model: type                  # the SQLAlchemy model
    gate: Callable               # (db, instance) -> bool  — privacy/consent gate
    serialize: Callable          # (db, instance) -> dict  — the sync payload (NO assets)
    apply: Callable              # (db, payload) -> None    — idempotent upsert-by-id receiver
    p2p: bool = True             # replicate peer-to-peer first
    central: bool = True         # then to central (bounded backup)

SYNC_ENTITIES: dict[str, SyncEntity] = {}   # op -> SyncEntity, the ONLY registry
```

The entities (the steward's list), each a single registry entry:

`user`/`profile`, `friend`, `public_agent`, `post`, `community`,
`community_membership` (join/leave), `resonance`, `encounter`,
`thought_experiment`.

- **Dispatcher** — `receive_sync_batch` becomes: `SYNC_ENTITIES[op].apply(db, payload)`
  inside the existing idempotency + try/except loop. The `if/elif` ladder is deleted.
- **Producer** — ONE `queue_entity(db, instance)`: look up the entity by `type(instance)`,
  run its `gate`, `serialize` (assets excluded), and `SyncEngine.queue(...)`. Replaces
  `sync_to_parent` + `sync_agent_to_parent` + every inline producer.
- The 3 existing receivers (`_handle_sync_post/_user/_agent`) become the `apply` of
  their registry entry — **moved, not rewritten** (zero behaviour change in phase 1).

## 3. Transport: P2P first, central second (bounded)

- **P2P (primary):** the gossip/PeerLink fabric replicates an entity to interested
  peers (followers/community members) first. Reuse `core/peer_link` (the WAMP topics
  in `message_bus.py`) — NOT a new bus.
- **Central (fallback/backup):** the same payload rides the existing up-sync
  (`/api/social/hierarchy/sync`) to central as a durable backup origin.
- **Down (central → node):** central **pushes over WAMP** (publish to the node's
  per-user topic; the node is subscribed) — never the node pulling by a claimed id.
  The node applies it through the **same** `apply` receiver. This dissolves the IDOR:
  central is the authority and the node verifies central's **signature** instead of
  asserting ownership.

### Trust gate (down direction) — the security invariant
A node applies central-pushed data ONLY if it carries a valid **master-anchored**
signature. Reuse `security/master_key.py:verify_master_signature` (or the
`key_delegation` cert-chain when central signs with its delegated key — central
NEVER signs with the master private key; that stays steward-only / AI-excluded).
Unsigned/invalid down-data is **refused and the reason surfaced** — never silently
applied. (This is the in-flight gap-#2 fix; it becomes Phase 4 here.)

## 4. Assets: never in the entity payload

Entity sync carries **references**, not bytes. Asset bytes resolve through the
existing CDN tier (#149): **P2P-first** (fetch from the origin peer), **central-
fallback** when the peer is gone. Central enforces a **10 TB total ceiling** across
all users — over the ceiling, central evicts coldest-first (LRU) and serves "fetch
from peer" / 410-gone rather than growing unbounded. Central is a *bounded backup
origin*, not a primary store.

## 5. Phased, zero-parallel-path migration (each phase ships green + tested)

1. **Registry + dispatcher.** Introduce `SyncEntity` + `SYNC_ENTITIES`; register the
   3 existing ops (`sync_post`, `sync_user`, `public_agent`); replace the `if/elif`
   ladder in `receive_sync_batch` with a registry lookup. Behaviour-identical.
   Behavioural test: every existing op still dispatches to its receiver.
2. **Unified producer.** Fold `sync_to_parent` + `sync_agent_to_parent` + inline
   producers into `queue_entity(db, instance)`; the gates become each entity's `gate`.
   Delete the duplicates.
3. **Missing entities.** Add `friend`, `community`, `community_membership`,
   `resonance`, `encounter`, `thought_experiment` as registry entries (gate +
   serialize + apply each). No new dispatch code — they're just registrations.
4. **Down-transport + trust gate.** Central WAMP push → node subscriber →
   `verify_master_signature`/cert-chain → the registry `apply`. Refuse unsigned.
   (Central-side signing uses central's delegated key — steward provisions it.)
5. **Asset CDN ceiling.** Wire the 10 TB central ceiling + LRU eviction onto the
   #149 fallback. Entity payloads already exclude assets after phase 1.

## 6. Invariants (every PR is checked against these)
- Adding a syncable entity = **one `SyncEntity` registration**, zero new dispatch/
  producer/transport code. If a change adds an `elif op ==` or a second producer,
  it's wrong.
- One receiver per entity (`apply`), idempotent upsert-by-id (mirrors
  `_handle_sync_user`). Synced rows are **mirrors** — never minted as verified/
  credentialed identities (see `a8b29762`).
- Down-data is signature-gated, fail-closed, reason surfaced on refusal.
- Assets never travel in the entity payload; central is bounded at 10 TB.
- Privacy/consent gate runs **before** serialize, on both P2P and central legs.
