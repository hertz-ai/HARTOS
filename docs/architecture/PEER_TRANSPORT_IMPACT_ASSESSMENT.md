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

**(c) Two handler registries.** `PeerLink._message_handlers` (per-link, for
request/response correlation on *that* socket) and `ChannelDispatcher._handlers`
(module singleton, for subsystem routing) answer different questions. Both are
legitimate.

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

**(D3) `_receive_loop` bypasses the ChannelDispatcher.** `:751` dispatches to
`self._message_handlers` only. The module-singleton dispatcher that `channels.py`
documents as *the* routing mechanism is never consulted — which is why
`dispatcher.dispatch()` has **zero call sites** and only 1 of 11 channels
(`hivemind_handler.py:204`) ever registers.

**(D4) The registrations in `channels.py:139-140` are inside a DOCSTRING**, not code.
They read as wiring; nothing executes them.

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
