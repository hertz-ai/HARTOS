# Peer transport & capability: full impact assessment (2026-08-30)

Commissioned by the steward after "three capability detectors, three peer registries"
was raised — with the correction that **"something might be intentional."** That
correction was right, and it changed the conclusion. Read this before touching
`core/peer_link/`, `integrations/social/peer_discovery.py`,
`integrations/agent_engine/compute_mesh_service.py`, or
`security/system_requirements.py`.

Every claim below was read out of the source, not inferred. Line numbers are HEAD.

---

## 1. The architecture AS DESIGNED — coherent, four layers, NOT parallel paths

My first pass called PeerLink a duplicate of gossip. **That was wrong**, and the
files say so plainly.

| layer | module | question it answers | cost |
|---|---|---|---|
| **1. Hardware truth** | `security/system_requirements.py` | *what IS this machine* | expensive; run once, cached |
| **2. Discovery** | `integrations/social/peer_discovery.py` | *who exists, where, at what tier* | periodic (60s), stateless |
| **3. Session transport** | `core/peer_link/` | *a durable encrypted pipe to ONE peer* | persistent |
| **4. Runtime availability** | `integrations/agent_engine/compute_mesh_service.py` | *what can I spare RIGHT NOW* | live, changes per minute |

**PeerLink DEPENDS on discovery, it does not replace it** — `link.py:8-9`:
*"LAN discovered via UDP beacon; WAN discovered via compute_mesh or gossip when
user_id matches."*

**Its channels are routing slots, not reimplementations** — `channels.py:138-140`:
```
Each subsystem registers its handler:
  dispatcher.register('gossip', peer_discovery.handle_exchange)
  dispatcher.register('federation', federation.receive_inbox)
```
The intent is that a `gossip` frame arriving over PeerLink is handled by **the same
`peer_discovery` code** that handles it over HTTP. One handler, two transports. That
is textbook layering, and an earlier draft of this document was wrong to call the
eleven channels "eight duplicates".

---

## 2. What is INTENTIONAL — do not "fix" these

**(a) `compute_mesh` having its own detector.** It reports `available_compute`
(`1.0 - max_offload_percent/100` — a *policy* value) and `loaded_models` (changes as
models load/evict). That is runtime availability, a genuinely different question from
static hardware. Static and dynamic must not be conflated.

**(b) NOT persisting hardware from relayed gossip records.** This is the important
one, and it is a *security* property. `_merge_peer_list` (`peer_discovery.py:1303`)
documents the two-tier trust model:

> *"These are RELAYED records, so they are address hints, not identity claims… A
> relayed record physically cannot carry proof of who it describes… Measured against
> live central: of 72 records returned by `/api/social/peers/exchange`, exactly ONE
> carried a signature, the sender's own live self-info. The other 71 were unusable."*

Hardware claims in a relayed record are unverifiable hearsay. Persisting them would
let any node inflate a peer's apparent capacity — and since
`hive_guardrails.py:541` and `model_registry.py:276` read those values for
compute-democracy weighting, that is a direct attack on the ≤5% influence cap.
**Only the signed, direct self-announce may set capacity.**

**(c) The per-link handler copy.** `link_manager._channel_handlers` is the
subsystem registry; `_apply_channel_handlers` fans it out to each link's
`_message_handlers` BEFORE `accept()` starts the receive thread (the ordering is
commented — a handler attached later would miss whatever arrived first). The per-link
copy is a deliberate fan-out of one registry, not a second one. **`ChannelDispatcher`
is a different matter — see D3 as corrected in §5b.**

**(d) `provable_user_id()` diverging from `link_manager`'s identity lookup.**
`link.py:83-103` explains it at length: one asks *"is this peer mine?"* (local
comparison, widest view correct), the other asks *"what can I demonstrate to a
stranger?"* (must match what the verifier reads, or you sign a value nobody checks).
Deliberate, documented, tested.

---

## 3. What is GENUINE DRIFT — evidence, not opinion

**(D1) `peer_link/link.py:789 _get_local_capabilities` duplicates static hardware
detection, and does it WORSE.**

| | canonical `system_requirements` | `peer_link/link.py:789` |
|---|---|---|
| cpu | `os.cpu_count()` | `os.cpu_count()` |
| **RAM** | `_detect_ram_gb()` — psutil → sysconf → Win32 → /proc | **absent** |
| **disk** | `_detect_disk_gb()` | **absent** |
| gpu | via `vram_manager` | via `vram_manager` |

Neither cost nor layering justifies it: `get_capabilities()` (`:722`) is an **O(1)
cached global read**, and `link.py` already imports from `security.*` freely
(`node_integrity`, `channel_encryption`, `pre_trust_contract`). The expensive
`detect_hardware()` is called once by `run_system_check()`
(`integrations/social/__init__.py:481`); everyone else reads the cache.

**(D2) The handshake capability exchange is ASYMMETRIC — a live read-before-write
bug.**
- `_perform_handshake` (`:417-478`) builds `hello` with `node_id`, `ed25519_public`,
  `x25519_public`, `trust_requested`, `protocol_version`, `timestamp`, optionally
  `user_id_proof` and `trust_contract` — then signs and sends. **No `capabilities`.**
- `_complete_handshake` (`:540`) does `self.capabilities = hello_data.get('capabilities', {})`.

So the ACCEPTING side records `{}` for every peer. Only the ack direction (`:597`)
carries them. **This is the exact bug class the same file already fixed once** — see
the comment at `:435-448`: `user_id_proof` was read by `_complete_handshake` but
"nothing in the codebase ever WROTE that field", so every SAME_USER request was
demoted and "multi-device sync (and the skill broadcast riding it) had no recipients
on any node." Fixed 2026-08-13 (`ce8be281`, `6098a66f`). The capabilities field is
the same defect, still open.

**(D3) `ChannelDispatcher` is an ORPHAN registry.** *(Restated after the §5b
trace — the first version of this finding blamed the receive loop and was WRONG.)*
`link_manager._channel_handlers` is the live registry with FOUR real registrants, and
`_receive_loop → _message_handlers` is fed from it correctly. `ChannelDispatcher`
(`channels.py:135`) is a SECOND module-level registry that only `hivemind_handler`
writes to — and it writes to the real one too (`:204` + `:208`) — and from which
`dispatch()` is **never called**. It is a duplicate of a working mechanism, not the
broken half of it.

**(D4) The registrations in `channels.py:139-140` are inside a DOCSTRING**, not code.
Harmless in itself, but it is what makes D3 hard to see: a reader greps
`register('gossip'`, gets a hit, and concludes the dispatcher is wired. The real
registrations live on `link_manager.register_channel_handler` in four other files.

**(D5) `_resolve_ws_url` (`:674-682`) has two identical branches.** Both return
`ws://`; the `wss://` for WAN promised in the header docstring (`:23`) is
unimplemented.

**(D6) No listener.** It dials `ws://{addr}/peer_link`; nothing serves that route
(confirmed by `HIVE_COLLAB_BOOTSTRAP.md`, and by search).

---

## 4. WHY we drifted — five mechanisms, each evidenced

1. **The import boundary hides the history.** `47183e457` (2026-06-29) added
   **2,330 files / 803,462 insertions**. All three detectors arrived pre-formed in
   that one commit, so no commit message explains the choice. Archaeology cannot
   recover intent that was never recorded.
2. **Concurrent agents building adjacent capability.** `0a338951f` (2026-07-03) is
   literally titled *"…(concurrent session's work)"* and extends `compute_mesh`. The
   standing memory rule (`feedback_respect_concurrent_sessions_no_parallel_paths`)
   exists because of exactly this.
3. **Read-before-write fields.** A receiver reads a field no sender writes. It fails
   silently and looks like "the peer didn't send it". Happened with `user_id_proof`;
   still live for `capabilities` (D2).
4. **Docstrings used as specification.** D4. A reader greps `register('gossip'`,
   finds a hit, and concludes it is wired.
5. **Dead code that reads as alive.** Trust ratchet, AES-256-GCM, key rotation,
   per-tier budgets (10/50/200) — all unreachable, so nothing ever fails loudly and
   the gap stays invisible. Same failure family as `openclaw.enable = true`
   installing nothing.

---

## 5. How each SHOULD be wired

```
security/system_requirements          ← the ONE static hardware truth
  run_system_check()  once at boot (already: social/__init__.py:481)
  get_capabilities()  O(1) cached, returns None before the check runs
        │
        ├─► GOSSIP direct announce ──► hardware_summary        [ALREADY CORRECT]
        │      └─► receiver persists capacity ONLY from the SIGNED direct
        │          announce, never from a relayed hint  → PeerNode.compute_*
        │          through the existing HierarchyService.report_node_capacity
        │
        ├─► PEERLINK handshake ──► cheap advert, in BOTH directions (fixes D2)
        │      └─► frames route through ChannelDispatcher so subsystems reuse
        │          their existing handlers (fixes D3/D4)
        │
        └─► COMPUTE_MESH static half ──► from get_capabilities()
               + its OWN dynamic half (available_compute, loaded_models)  [KEEP]
```

Guard for every consumer: `get_capabilities()` returns `None` until
`run_system_check()` has run. Gossip already guards correctly (`:1117 if caps:`);
any new consumer must do the same rather than assume a profile exists.

---

## 5b. THE COMPLETE CALL FLOW — what fires, when, driven by what

Traced end to end 2026-08-30. This is the part that makes the change plan surgical
rather than speculative, and it **corrects two claims made earlier in this document**.

### BOOT
```
integrations/social/__init__.py:481   run_system_check()  → _capabilities (cached global)
                                        └─ detect_hardware(): cpu_count, RAM ladder
                                           (psutil→sysconf→Win32→/proc), disk, GPU via
                                           vram_manager, and a NETWORK reachability probe
security/system_requirements.py:722   get_capabilities()  → O(1) read; None before the check
```

### GOSSIP ROUND — 60s, fanout 3, HTTP `:6777` (THE LIVE RAIL)
```
_exchange_with_peer(peer_url)                            peer_discovery.py:839
  ├─ _self_info()                                        :1135   ← the ANNOUNCE payload
  │    ├─ node_id, url, name, version, tier, hart_tag
  │    ├─ public_key, code_hash, guardrail_hash, x25519_public, certificate
  │    ├─ capability_tier + hardware_summary             :~1190
  │    │     {cpu_cores, ram_gb, gpu_vram_gb, disk_free_gb}   ← from caps.hardware
  │    ├─ idle_compute {available, idle_agents, opted_in}:~1205
  │    └─ signature = sign_json_payload(info)            :1267   ← signed LAST, covering
  │                                                              every field (fix 477d61b7)
  ├─ POST /api/social/peers/exchange
  ├─ handle_exchange(their_peers)                        :1049
  │    └─ _merge_peer_list(peer_list)                    :1303   ALL entries relayed=True
  │         └─ _merge_peer(db, p, relayed=True)          :1371
  │              ├─ banned check · Sybil cap (HEVOLVE_MAX_PEERS_PER_IP, default 5)
  │              ├─ guardrail-hash agreement
  │              ├─ signature_valid = verify_json_signature(...)  ← COMPUTED REGARDLESS
  │              │                                                  of `relayed`
  │              └─ writes node_id/url/public_key/tier/... but **NEVER compute_***
  └─ get_link_manager().record_http_exchange(peer_id)    :856, :889
       └─ at _UPGRADE_THRESHOLD → _try_auto_upgrade()    link_manager.py:472
            ├─ gossip.get_peer_list() → resolve peer_info
            ├─ core/peer_link/nat.py ladder (LAN direct via beacon → STUN → relay)
            └─ upgrade_peer() → PeerLink.connect() → dial ws://<addr>/peer_link
                                                          ↑ NOTHING SERVES THIS ROUTE
```

### PEERLINK — fully wired, blocked on one route
```
subsystem → link_manager.register_channel_handler(ch, h)  link_manager.py:337
     FOUR real registrants, not one:
       core/peer_link/hivemind_handler.py:208        'hivemind'
       core/peer_link/message_bus.py:485             'events'
       embedded_main.py:279                          'dispatch'
       integrations/agent_engine/federated_aggregator.py:1681  'learning' (FedAvg deltas)
  └─ _apply_channel_handlers(link)                        :290
        (called BEFORE accept() on purpose — a handler attached after the receive
         thread starts would miss whatever arrived first)
       └─ link.on_message(ch, h) → PeerLink._message_handlers
            └─ _receive_loop dispatches to them            link.py:751   ✓ CORRECT PATH
inbound:  link_manager.accept_inbound(peer_id, addr, ws, hello)  :230  ✓ IMPLEMENTED
          (same budget check, same handler registration, same side effects as outbound;
           trust constructed at PEER and ratcheted up only on a valid user_id_proof)
```

### TWO CORRECTIONS TO THIS DOCUMENT

**(i) D3 was WRONG — the receive loop does NOT bypass the intended registry.**
`link_manager._channel_handlers` is the live mechanism, it has FOUR real registrants,
and `_apply_channel_handlers → link.on_message → _message_handlers → _receive_loop`
is a complete, correct chain. What is orphaned is `ChannelDispatcher`
(`channels.py:135`): a SECOND module-level registry that only `hivemind_handler`
writes to (and it also writes to the real one, belt-and-braces at `:204` + `:208`),
and which nothing ever dispatches from. **The orphan is the dispatcher, not the loop.**

**(ii) PeerLink is far more complete than "dead" suggests.** Handlers registered,
`accept_inbound` implemented, budgets, trust ratchet, key rotation, and the NAT
traversal ladder all present, and the gossip→PeerLink upgrade trigger genuinely fires
(`record_http_exchange` is called from two live gossip sites). **The entire stack is
blocked on ONE missing thing: nothing serves `ws://…/peer_link`.** That is hook #5,
and it is much closer to done than the bootstrap doc's "DIAL-ONLY" implies.

### ONE NEW FINDING FROM THE TRACE

**(D7) The `hardware_summary` block is built TWICE** — `get_health()` (`:1105`, around
`:1119`) and `_self_info()` (`:1135`, around `:1190`) each construct
`{cpu_cores, ram_gb, gpu_vram_gb, disk_free_gb}` from `caps.hardware` independently.
Add a field to one and the other silently lacks it. Small, but it is the same class
as everything else here and it is one extract-method away.

---

## 6. What it takes to canonicalise — ordered by risk, each independently shippable

**C0 — characterise first (blocks the rest).** No behavioural test exists for the
gossip merge path or the handshake. Pin current behaviour: a signed direct announce
upserts a PeerNode; a relayed record lands unverified and sets no capacity.

**C1 — fix D2, the handshake asymmetry.** One line: add
`'capabilities': self._get_local_capabilities()` to the `hello` dict. Blast radius
nil today (no listener), which is exactly why it should land BEFORE hook #5 makes
PeerLink live — otherwise it ships as a silent data-loss bug on day one.

**C2 — persist capacity from the SIGNED direct announce only.** In `_merge_peer`,
where `signature_valid` is already computed, map `hardware_summary` →
`report_node_capacity`. Explicit field mapping, and **never** from `relayed=True`.
This is the change that makes 107 active peers report real compute and stops
compute-democracy weighting on a uniform 1-GPU/8-core fiction. Mapping care:
`gpu_vram_gb` is VRAM, `ram_gb` is system RAM — conflating them is the "#91
wrong-keys class" the code already memorialises.

**C3 — collapse D1.** `peer_link`'s detector calls `get_capabilities()` and keeps a
minimal fallback for the pre-check window. Deletes the RAM/disk blind spot.

**C4 — wire D3/D4.** `_receive_loop` also feeds `ChannelDispatcher`; register
`peer_discovery.handle_exchange` and friends in code rather than in a docstring.
**Do this before hook #5**, not after.

**C5 — then hook #5** (the inbound `/peer_link` listener). Landing it before C1–C4
gives the fleet a live transport whose accepting side records empty capabilities and
whose subsystem routing is unwired.

**Not in scope, flagged:** `_resolve_ws_url`'s dead `wss://` branch (D5) — decide
whether WAN TLS is terminated upstream or must be implemented, then make the code
and the docstring agree.

---

## 7. Registries — the honest count

Five, not three. Three are legitimate; two are the drift.

| registry | holds | verdict |
|---|---|---|
| `PeerNode` (DB) | durable peer identity + capacity | **canonical, persistent** |
| `link_manager._links` | live sockets | legitimate — sockets are not durable state |
| `compute_mesh._peers` | live mesh peers + availability | legitimate — dynamic |
| `PeerLink._message_handlers` | per-link handlers | legitimate — per-socket correlation |
| `ChannelDispatcher._handlers` | subsystem handlers | **documented as canonical, never consulted (D3)** |
