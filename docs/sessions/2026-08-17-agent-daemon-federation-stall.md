# Agent daemon stalled: 9 threads wedged in FederatedAggregator.tick()

2026-08-17. Steward's question: "Nunba shd be able to peer and federate and
distribute tasks etc via the HARTOS layer is the thing you gotta verify."

It does not. Root cause below, found from thread dumps after installing the
2026-08-16 build.

## Root cause

Nine `agent_daemon` threads (ids 31324, 5260, 29044, 30876, 19808, 31452,
4348, 27980, plus `agent-daemon-supervisor`) are all parked at the same place:

```
agent_daemon.py:790  in _loop
agent_daemon.py:1590 in _tick
      line 1590:  fed_result = fed.tick()
```

Chain:

1. `get_federated_aggregator()` returns a singleton holding 7 locks
   (`_lock`, `_embedding_lock`, `_lifecycle_lock`, `_resonance_lock`,
   `_recipe_lock`, `_event_counters_lock`, plus module-level
   `_peer_hmac_lock`).
2. `tick()` runs `extract_local_delta` → `broadcast_delta` → `aggregate` →
   `apply_aggregated` → `track_convergence` → `embedding_tick` →
   `resonance_tick`, taking `with self._lock:` (595/623/700/794) and
   `_embedding_lock` (820/825/858/866) across slow work.
3. Every daemon thread calls the same instance, so they convoy on those locks.
4. `agent-daemon-supervisor` sees a stuck daemon and starts another, which
   wedges at the identical line. Threads accumulate.
5. `_tick()` never completes, so goals never dispatch and the ledger stays at
   10,023 pending.

The `try/except Exception` around 1587-1596 does not help — a hang is not an
exception.

## This is a recurrence

`agent_daemon.py:756-763` records the same failure from 2026-04-29:

> a stuck call inside the proactive path (WorldModelBridge.record_interaction
> was the culprit — daemon restarted 9 times by watchdog without ever reaching
> _tick because complete_action blocked) must NOT block the goal-dispatch tick.

That fix made the proactive tick and home compose fire-and-forget
(`_spawn_proactive_hive_tick_async` at 764, `_spawn_home_compose_async` at
775). `fed.tick()` at 1590 is still inline. Same shape, new call site, again
about nine threads.

## Why tick_count read 0 — corrects an earlier reading in this session

`/api/agent-engine/ledger/stats` returned:

```json
{"available":true,"running":true,"thread_alive":true,"tick_count":0,
 "source":"flask_in_process"}
```

I concluded `_tick()` was never reached, because `self._tick_count += 1` is the
first statement of `_tick` (line 882). That was wrong — the dumps show `_tick`
is entered. With nine daemon instances each carrying its own `_tick_count`, the
API reads one that is not the ticking instance.

This is the shadow-singleton problem inverted: not a stale module, but multiple
live instances. A liveness check here has to count daemon threads, not trust one
instance's counter.

## Measured on the new build (installed 2026-08-17 11:46)

| Probe | Result |
|---|---|
| `/api/hive/session/status` | `disconnected`, `peer_link_id: null`, `session_id: ""` |
| `/api/hive/session/tasks` | `{"completed":[],"pending":[]}` |
| ledger | 12380 total / 1335 completed / 1015 failed / 10023 pending |
| ledger, 2h earlier (old build) | identical four numbers |
| `tick_count` | 0 at T, 0 at T+75s (poll_interval 30, so ≥2 ticks due) |
| `/backend/health` | 200, operational, RTX 3070, 7.83/8.0 GB free |

## Fix direction — needs a caller audit first

Two separable defects. The first strands the queue; the second amplifies it.

a. `fed.tick()` must not run inline in `_tick`. Either fire-and-forget like the
   two calls above it, or bound it (own thread + `join(timeout)`) so a stuck
   federation epoch cannot hold the dispatch loop. Match the existing
   `_spawn_*_async` pattern rather than adding a third shape.

b. The supervisor must not start a second daemon while one is alive. Nine
   threads is what turned one stuck epoch into a total stall. Find the spawn
   site and make it idempotent per live thread.

Also worth checking whether `FederatedAggregator`'s locks need to be held
across network and DB work, or whether the critical sections can shrink.

Not yet established: which of the seven locks is held, and by which thread. The
dumps give the line, not the owner. That needs a dump with full frames per
thread; the log shows two frames each.

## Related

- #660, same class — expensive work reachable before auth (fixed 2026-08-16,
  `c2b2cce1`)
- `memory/feedback_vacuous_guards.md` — a guard that cannot fail for its own
  defect
- `memory/feedback_liveness_vs_readiness_vs_busy.md`
