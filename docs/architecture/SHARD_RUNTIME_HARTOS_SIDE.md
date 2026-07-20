# Shard Runtime — the HARTOS side

The HARTOS half of WAN distributed model-inference sharding. The hevolveai half
is frozen in `hevolveai/docs/SHARD_RUNTIME_CONTRACT.md` (v1). This doc is the
mirror: it maps that contract onto HARTOS rails so both sides code to the same
struct, and records what is built now versus next.

## The split (fixed)

- **hevolveai owns** the layer executor: load a contiguous layer range of a
  model and run its forward pass, reproducibly. Exposed behind the Model Bus
  `/v1/shard/*` verbs. All torch / weights / model math live there.
- **HARTOS owns** the open fabric around it: which peers form the cluster, how
  the activation moves between them, how a too-big request is routed, who is
  allowed to be a shard, how a lying shard is caught, and how providers are paid.
  HARTOS has no ML code and never runs a forward pass.

Pipeline parallelism (contiguous layer ranges; only the hidden-state activation
crosses the wire). NOT tensor parallelism (that all-reduces every layer and dies
over a WAN). Idea borrowed from parallax; re-implemented on our own rails. One
mesh (PeerLink), one VRAM-sizing path, no vendored stack.

## HARTOS homes

| Concern | Home | Status |
|---|---|---|
| Activation wire envelope (parse + relay, no torch) | `core/shard_runtime/envelope.py` | BUILT |
| Advertise the cluster as a routable model | `integrations/agent_engine/model_registry.py` (`distributed-shard`, flag `HART_SHARD_RUNTIME`) | BUILT (stub) |
| Form the cluster + capacity-aware layer assignment | `integrations/agent_engine/compute_mesh_service.py` (`ComputeMeshService.get_available_peers` / `score` / `MeshPeer.capabilities`) | reuse; orchestrator NEXT |
| Relay the activation peer-to-peer | `core/peer_link` (`MessageBus` / `link`) | reuse; relay wiring NEXT |
| Drive the pipeline loop (prefill + each decode) | shard-orchestrator (new, `core/shard_runtime/`) | NEXT |
| Admission (which node may be a shard) | `security/` master-anchored cert chain (`master_key.py`, `key_delegation.py`) | reuse; gate NEXT |
| Catch a lying shard | `security/` benchmark_prover cross-check + `fingerprint()` + verify mode | reuse; verify NEXT |
| Reward providers | spark economy | reuse; metering NEXT |

## Runtime flow (where each Model Bus verb fires)

1. A request too big for one node routes to the `distributed-shard` backend.
2. The orchestrator asks `ComputeMeshService` for candidate peers and calls
   `plan_shard(model_id, vram_budget_gb, backend)` on each (over the Model Bus,
   pure and cheap). It cuts contiguous ranges from `layers_that_fit`, then calls
   `load_shard(model_id, start_k, end_k, role_k, quant)` on the chosen nodes:
   node 0 = `first` (embed + layers), last = `last` (layers + norm + lm_head),
   the rest = `middle`.
3. The orchestrator runs one `request_id`:
   - prefill: `token_ids_frame` to the first shard, then `forward(..., "prefill", activation)`
     hop by hop; PeerLink relays each envelope as opaque bytes; the last shard
     returns logits (`is_final`), and the orchestrator samples.
   - decode: `forward(..., "decode", ...)` one token at a time down the chain;
     each node appends to ITS OWN KV cache keyed by `request_id`.
   - finish: `release(handle, request_id)` on every node.
4. Trust: before admission `security` verifies the node's master-anchored cert.
   Periodically the orchestrator takes a shard `fingerprint()`, has a second node
   `load_shard` the same range and re-run `forward` in verify mode on a sampled
   activation, and compares within the contract tolerance (`atol=2e-2, rtol=2e-2`
   on bf16 final-token logits). A mismatch flags the shard. HARTOS does the
   comparing and flagging; hevolveai only guarantees reproducibility.
5. Each contributed forward meters spark to the node's provider.

## Invariants HARTOS honors (from the contract §3)

1. **HARTOS owns the `request_id` lifecycle.** load once, many forwards keyed by
   `request_id`, then release. The executor never invents its own session key,
   so our per-token decode loop and `release()` line up with its KV cache.
2. **The envelope is self-describing** (`parse_header` reconstructs routing from
   the header alone) because PeerLink relays it across a different machine.
3. **`plan_shard` is pure and cheap** so ComputeMesh can poll many candidates.
4. **Fail clean.** If a hop or the orchestrator dies, HARTOS re-plans and
   re-routes; the executor only errors and frees KV. Retry/re-route lives on ONE
   side (HARTOS). Doubling it is a banned parallel path.
5. **Works with central OFF.** ComputeMesh + PeerLink are peer-to-peer; trust is
   master-anchored but data is P2P. No central coordinator is assumed.

## Sequencing (decentralization-safe MVP first)

`ComputeMeshService`'s privacy boundary is `user_id`: only YOUR devices join YOUR
mesh. So the first cut is **same-user-device sharding** (your phone + laptop +
desktop split one big model): trust is trivial (all nodes are yours), so the
verify/benchmark_prover leg can start as a no-op and the risk of a lying shard is
zero. The **crowdsourced cross-user** case (different people's nodes) reuses the
same orchestrator + envelope but turns on master-anchored admission + the verify
cross-check. Ship same-user first; it needs no new trust surface.

## Selection guard (until the orchestrator lands)

`distributed-shard` only registers when `HART_SHARD_RUNTIME=1`, and its
`base_url` is the non-endpoint `shard://cluster`. The dispatcher / speculative
router must SKIP this `model_id` until the orchestrator is wired to intercept it,
so nothing ever dials the placeholder. When the orchestrator lands, it claims
`model_id == 'distributed-shard'` before any OpenAI-style client is built.

## Built now vs next

- BUILT: `core/shard_runtime/envelope.py` (parse/relay + first-hop `token_ids`
  frame, stdlib-only, no torch) with a cross-implementation parity test against
  the frozen byte layout; the flagged `distributed-shard` registry entry.
- NEXT (against this same envelope): the shard-orchestrator + PeerLink relay
  wiring + the `security` admission and verify legs + spark metering. These wait
  on hevolveai's HF backend + golden numerical test (the compute-bound half).
