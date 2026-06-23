# L3 — Collective Earning ("every Nunba earns collectively for the user")

Status: DESIGN (blocks on one steward decision + 2-node verify #150). Grounded in
a read of `integrations/agent_engine/federated_aggregator.py`,
`integrations/agent_engine/revenue_aggregator.py`, and the live blocker map in
`memory/flywheel_action_banking_gap_2026-06-11.md` (L3 section).

## The gap (verified in code, 2026-06-24)
- `federated_aggregator.extract_local_delta()` gossips LEARNING only —
  experience_stats / ralt_stats / hivemind_state / quality_metrics /
  benchmark_results / contribution_score / recipe catalog / resonance.
  There is **no earnings field and no money channel**.
- `revenue_aggregator` computes each box's OWN 90% pool from its OWN local
  `db.query()` revenue (api_revenue + ad_revenue). No cross-node fan-in.
- Net: a user who runs N Nunba nodes earns in **N isolated pools**. The
  steward's vision — earnings AGGREGATE to the user's collective 90% pool — is
  unwired. This is the L3 architectural blocker, NOT a bug to patch in passing.

## The decision the steward must make (the ONE blocker)
Earnings are private + are money. The learning-gossip mechanism is the WRONG
transport for them (it fans out to every peer in the hive → would leak one
user's revenue to strangers). Two correct shapes — pick one:

- **(A) User-scoped remit (RECOMMENDED).** Each of a user's nodes signs +
  remits its earning delta to ONE canonical user ledger (central
  `Hevolve_Database`, or a user-elected "home" node). The ledger SUMS deltas
  keyed by `(user_id, node_id, period)` — idempotent, no double-count. Nothing
  about a user's money ever touches a non-owned peer. Mirrors the existing
  PULL/`_handle_sync_user` profile-sync rail (DESIGN A, #170-172), so it reuses
  a proven, master-anchored, consented transport.
- **(B) Owned-subset gossip.** A user's nodes gossip earning deltas ONLY among
  the user's own node-set (never the hive). Needs a verified "these nodes are
  mine" membership proof (Ed25519 chain to the user identity) before any
  earning leaves a node. More moving parts; same privacy bar.

(A) is simpler, privacy-tightest, and reuses existing rails. Recommendation: A.

## Mechanism (for A)
1. **Producer** — `revenue_aggregator.extract_earning_delta()`:
   read-only, returns `{user_id, node_id, period, gross, pool_share_90,
   api_revenue, ad_revenue, hosting_rewards, ts}`. Pure read of the local db;
   NO mutation, NO broadcast.
2. **Consent + DLP gate** — before any remit: `ConsentService.check_consent(
   user_id, 'cloud_egress', scope='earning_sync')` AND `ScopeGuard.check_egress`
   (same fail-closed backstop the learning broadcast uses). No consent → stay
   local (earn in isolation, surfaced as "link this node to sync earnings").
3. **Transport** — reuse the sync rail (`SyncEntity`/`queue_entity`, #177): a new
   `earning` entity, `central=True p2p=False`, gated by the consent above.
4. **Receiver (central)** — idempotent upsert keyed `(user_id, node_id, period)`;
   the user's collective pool = `SUM(pool_share_90)` across their nodes. LWW on
   `(period, ts)` so a re-remit can't double-credit.
5. **Read-back** — `GET /api/earnings/collective` returns the user's summed pool
   across all their nodes for the wallet UI; each node shows "your share here +
   your collective total".

## Privacy / security invariants (non-negotiable)
- A user's earning delta NEVER reaches a node the user doesn't own.
- Consent-gated (`cloud_egress`/`earning_sync`); unconsented → local-only.
- Master-anchored: the central receiver verifies the node's cert chain + that
  the claimed `user_id` owns the node (no minting earnings for someone else —
  cf. the #183 `_handle_sync_user` privilege-escalation lesson: NEVER honor a
  self-claimed identity from sync ingress without verification).
- Idempotent by `(user_id, node_id, period)`; LWW; no double-count on retry.

## Why this is NOT auto-shipped in one session
Money + privacy + cross-node + multi-user. Per the change protocol it needs:
Gate-0 (this doc), Gate-1 caller audit on revenue_aggregator, Gate-5 tests
(producer shape, idempotent receiver, consent-blocked path, double-remit
rejected, cross-user-leak refused), Gate-8 ciso/privacy review, and **live
2-node + central verification (#150)** — a real user with 2 nodes, assert the
collective pool == sum and a second user's earnings never appear. Shipping the
remit unverified is exactly the BLOCK-defect class flagged in the L3 memory.

## Bounded, safe first slice (no live-money risk) — ready to build on a steward "go A"
- `revenue_aggregator.extract_earning_delta()` (pure read) + unit test.
- `aggregate_collective_earnings(deltas) -> {user_id: total}` pure function
  (user-scoped sum, idempotent by (node_id, period)) + unit test incl. the
  double-remit and cross-user-isolation cases.
- NO broadcast/remit/receiver until the steward picks A vs B and the consent +
  central-verify legs are reviewed. The pure pieces are inert until wired.
