# INPUT-TO-PHOTON HARNESS -- measured for every component, view and animation

**Steward mandate (2026-07-20):** "input to photon shd be measured for every UI
component view animations as tests which can be applied at scale for full
spectrum of what we develop."

Latency is the metric the whole native program is justified by (see
NATIVE_SHELL_PARITY_PROGRAM.md, "THE BAR"). An unmeasured latency claim is an
opinion. This document defines the instrument, the attribution, and the tests --
and the rule that coverage is AUTOMATIC, so a new component cannot ship
unmeasured.

## 1. Why we can measure TRUE input-to-photon (and app frameworks cannot)
An application only sees "event arrived" to "I finished painting". It cannot see
the compositor queue, the page flip, or scanout -- so every browser/app-level
number is a PROXY.

hart-comp owns BOTH ends:
- **T_input**: libinput delivers the event with a KERNEL timestamp (`input_event`
  `time`), captured before any of our code runs.
- **T_photon**: the DRM page-flip completion (vblank) for the frame that carried
  the damage caused by that event -- `reap_completed_vblanks` in `udev.rs`
  already runs at exactly this point (it is where the #131 first-scanout beacon
  fires).

`latency = T_photon - T_input`, measured across compositor queueing and scanout,
not "time to paint".

## 2. Attribution: which component was that frame for
A raw histogram is not actionable ("something is slow"). Each measurement must
name the component.

- The input event is tagged with the SceneNode id that consumed it (hit-test
  result), or `shell:<surface>` when it was routed to a Wayland client.
- That id rides the damage record into the frame build.
- On flip completion the sample is recorded as
  `(component_id, interaction_kind, latency_us)`, where kind is one of
  `press | drag | hover | scroll | key | resize | window-move | animate-start`.
- **The same instrument measures the WEB shell**, because hart-comp also
  delivers its input and presents its surface. The native and WebView shells are
  measured by ONE instrument, so "native is faster" is a demonstrated delta, not
  a claim.

## 3. Output contract
Two sinks, both machine-readable:
- **Journal (always on, cheap):** one aggregated line per component per 10s
  window: `hart-latency component=orb kind=drag n=142 p50=8.1ms p99=14.7ms
  max=19.2ms budget=25ms verdict=PASS`. Costs one atomic add per frame.
- **`/run/hart/latency.jsonl` (harness runs):** raw samples for the test asserts
  and for regression diffing between builds.

## 4. The tests -- and why coverage scales automatically
Three layers. Only the third is per-component, and it is GENERATED, not authored.

**L1 -- coverage guard (dev box, runs today).**
`tests/unit/test_latency_budget_coverage.py` enumerates every interactive and
animated surface from the SERVED shell + the CSS parity ledger, and asserts each
has a declared budget in `docs/architecture/latency_budgets.json`. A new
component with no budget FAILS the build, so "full spectrum" coverage cannot
silently regress.

**L2 -- synthetic input replay (VM/nixosTest).**
A uinput virtual pointer/keyboard replays a scripted interaction per component
(press, 60-sample drag sweep, hover enter/exit, scroll, key). hart-comp records
samples; the test asserts p50/p99 against the budget. Runs headless in CI on
llvmpipe -- catches ALGORITHMIC regressions (an easing layer sneaking into a
drag path, a relayout per frame) even though absolute numbers differ from real
hardware.

**L3 -- real-hardware gate (the node).**
The same replay on the HD 620, asserted against the REAL budgets. This is the
only number that may be quoted publicly. A milestone reports it or does not
close (NATIVE_SHELL_PARITY_PROGRAM: "'Feels fine' is not evidence").

## 5. Budgets (declared as DATA, versioned, reviewable)
`latency_budgets.json` maps `component -> {kind: budget_ms}`. Defaults derive
from the program's bar (<25ms input-to-photon), tightened where the interaction
is continuous:
- `drag`, `hover`, `scroll`, `window-move`: **16ms** (one frame -- these must be
  same-frame by construction; anything slower means an easing layer exists)
- `press`, `key`: **25ms**
- `animate-start`: **33ms** (two frames to first motion is perceptually fine)
A budget may only be RAISED with a recorded justification in the same commit.

## 6. Anti-gaming rules
- Measure the WHOLE path or nothing: any sample not anchored to a kernel input
  timestamp and a flip completion is invalid and must not be reported.
- Report distributions, never a single mean. p99 is the user's experience of
  "it stuttered"; a good mean hides it.
- Under-load runs count: a latency number taken on an idle desktop is the easy
  case. The L3 gate includes a run with an app launching (the shell must not
  share a thread with app work -- the bar's third row).
- Never disable a component to make a number. Coverage (L1) exists to catch it.

## Status
- 2026-07-20: designed; L1 coverage guard implemented (runs on the dev box).
  L2/L3 need the compositor instrumentation (M0), tracked in the native program.
- 2026-09-05: M0 attribution LANDED. Samples bucket by (surface, kind) rather than
  kind alone: the scene names the surface under the pointer
  (`scene::SceneNode::component_at`, deepest-wins) and `latency.rs` buckets on it,
  so the 23 per-component budget rows are consulted for the five surfaces the
  native shell draws (orb, top-bar, omnibox, taskbar, home-card). Everything else,
  including the whole WebView shell, stays `component=shell` on purpose: this
  harness is what makes "native is faster" a delta rather than a claim, and that
  line's format is unchanged so historical numbers stay comparable.

  Two facts found while wiring it, both asserted by guards rather than left as
  lore: no component in latency_budgets.json actually overrides the `_defaults`
  for its kind (the table declares which interactions a surface supports, not
  different numbers), and a relative-motion sample is attributed to the surface
  the pointer is LEAVING, because T_input is captured before the event is applied.

- 2026-09-05: `animate-start` joined the measured kinds. udev.rs snapshots
  `ws_switch_at` around `process_input_event`; an input that moved it RE-KINDS its
  own pending sample to AnimateStart on the `workspace-switch` surface, so the
  keypress and the transition it caused stay one interaction and one photon lands
  in one bucket. It is measured from the KEY, not from the fade's own start, which
  is what makes a switch whose key echoes instantly and whose fade begins 200ms
  later show up as the failure the user actually sees rather than a passing `key`.

  A window MAP animation is deliberately still not attributed: it is usually the
  client's own doing, and blaming whatever key happened to be pending would be a
  made-up number. With no pending input there is nothing to re-kind, which is the
  honest outcome and is asserted.

  That leaves `window-move` and `resize` as the only declared-but-unmeasured
  kinds, and only because the compositor does not perform those interactions at
  all: move_request and resize_request are deliberate no-ops (the WM owns
  geometry; the affordance is Phase-8). The exemption list in
  test_latency_budget_coverage.py carries that reason and fails if it grows.

## Measured off the render path: the agentic UI push costs ~400ms
Not an input-to-photon number, and not the compositor's, but it sits directly in
front of the desktop the instrument measures, so it belongs beside it.

MEASURED 2026-09-05 on the dev box: one `liquid_ui_service.agent_ui_update` takes
**~440ms**, of which **~396ms is `immutable_audit_log.log_event`** and ~206ms of
that is a single synchronous `sqlite3.Connection.commit()`. Profiled over ten
pushes after warm-up, so it is not import cost.

`compose_home` goes through that same call, which means every agentic home
compose, the exact payload the native scene renders, pays a durable SQLite commit
before it leaves the service. An agent "baking new UI on the fly" therefore cannot
paint faster than about two frames a second no matter how fast the renderer is.

It also made a security test unfalsifiable. `test_a2ui_gate_hardening.py`'s flood
test pushed 30 times against a 20-token bucket refilling at 2/s; at 440ms a push
the flood spans 13 seconds and refills 26 tokens, so the cap never engaged and the
test failed while the guardrail was working perfectly. Fixed by freezing the clock
for the flood, which is the honest shape: a cap that only shows up when you outrun
a refill is not what that test is for.

NOT CHANGED HERE, deliberately. The audit commit is a constitutional control with
documented real-hardware incidents behind its exact shape (immutable_audit_log's
own comments carry the 2026-08-12 writer-lock evidence and the borrowed-session
rule). Whether an agent UI push should carry a synchronous durable audit write, or
whether that write can be batched or deferred without weakening the hash chain, is
a security decision with an owner. This records the cost and where it goes so that
decision can be made on numbers.

### Re-measured 2026-09-24 (S7), same method
Same instrument as above: cProfile around `LiquidUIService(a2ui_enabled=True)
.agent_ui_update('prof', {'type': 'card'})`, ten pushes after three warm-ups, real
service, real audit log, only `HiveCircuitBreaker.is_halted` pinned False (the
isolation `test_a2ui_gate_hardening.py` uses). Dev box, Windows, NTFS.

| What | 2026-09-05 | 2026-09-24 |
|---|---|---|
| one push, wall | ~440 ms | p50 101.7 ms, mean 100.9, max 111.3 |
| `immutable_audit_log.log_event` | ~396 ms | 100.2 ms of 100.7 (99 percent) |
| `sqlite3.Connection.commit` | ~206 ms | 37.0 ms (10 calls) |
| `sqlite3.Connection.close` | not split out | 41.4 ms (20 calls) |
| pool `connect` | not split out | 17.1 ms (20 calls) |
| `_get_last_hash` | not split out | 15.7 ms (10 calls) |

The box got faster (a different disk state; nothing in the code changed) and the
shape did not: everything is `log_event`. What the split adds is that the durable
commit is under half of it. `log_event` opens and tears down TWO sessions per event
(`_get_last_hash` opens its own to read the chain head, then the writer opens a
second one), and the pool hands out a fresh sqlite connection each time, so the
connect and close churn (58 ms) costs more than the commit (37 ms). Moving the
commit off the paint path alone would leave ~60 ms on it.

Append plus fsync of one journal line (the alternative durable write, measured with
a 50-line loop after 5 warm-ups, 190-byte lines, `O_APPEND` + `os.fsync`):

| Where | p50 | p90 | p99 |
|---|---|---|---|
| dev box, NTFS, the repo dir | 72 ms | 157 ms | 233 ms |
| deepbox `langchain` container, ext4, `/app` | 8.3 ms | 10.5 ms | 21 ms |
| deepbox `langchain` container, ext4, `/tmp` | 8.3 ms | 10.1 ms | 11.3 ms |
| Samsung box (the target) | not measured: the box is read-only to streams; the coordinator runs the probe |

So even the cheapest durable write is half a frame on ext4 and several frames on
this Windows disk. A design that fsyncs before returning cannot meet 16 ms on the
dev box at all and is marginal on the box; the fsync has to be off the return path
and the durability has to be enforced somewhere else. That is the design below.

### DESIGN: the audit row off the paint path, durable before the next gate decision
Status: DESIGN ONLY (no code), the N4 steward decision from NATIVE_OS_PROGRAM.md
section 6 item 1. Written by S7 so the decision is made on a concrete shape.

**Invariant kept.** Every accepted push has an audit row on the chain, in the order
the pushes were accepted, and no push is accepted while the previous push's row is
not yet durable. The gate stays fail-closed: if durability cannot be proven, the
push is refused, never painted unaudited.

**What moves.** Today, inside `agent_ui_update`: allowlist, halted, rate cap,
destructive guardrail, XSS, store, wake, `log_event` (sync commit), emit. Target:

1. At the gate's entry, before the allowlist: `audit.wait_durable(seq_of_previous_push,
   timeout)`. The push is admitted only once the previous accepted push's row is on
   disk. On timeout or a dead writer it returns False and the push is refused with a
   WARNING, exactly like the halted refusal. This is the "durable before the NEXT
   gate decision" rule, and it is a wait that normally costs nothing: by the time a
   second push arrives the first row's fsync (8 ms on ext4) has long completed.
2. Store and wake, unchanged and still first.
3. `log_event(..., durable='journal')`: the SAME function, one new argument whose
   default (`'sync'`) keeps every existing caller exactly as it is (the borrowed
   session `db=` contract, the writer-lock retry, the memory fallback). No sibling
   function, no second log. In journal mode, under the existing `_lock`: take
   `prev_hash` from an in-memory chain head (initialised once from the DB tail,
   then maintained here, so `_get_last_hash` and its session are off the path),
   compute `entry_hash` with the unchanged `_compute_hash`, assign a monotonically
   increasing `seq`, append the row to a bounded in-memory queue, return
   `(seq, entry_hash)`. Nothing touches sqlite. Measured cost of this step is the
   hash and a list append: well under a millisecond.
4. Return. `agent_ui_update` is now store + wake + hash + enqueue: under 16 ms by
   two orders of magnitude, and the number becomes a budget line
   (`core.constants.LATENCY_BUDGETS`, so the no-orphan guard gates it).
5. A single writer thread drains the queue: appends one JSON line per row
   (`seq`, the six hashed fields, `prev_hash`, `entry_hash`) to
   `<data dir>/audit/a2ui-journal.jsonl` opened `O_APPEND`, calls `os.fsync` once
   per drained batch, and only then marks every `seq` in the batch durable (a
   condition variable `wait_durable` blocks on). Then it inserts the same rows into
   `AuditLogEntry` in `seq` order in ONE session per batch and commits. The journal
   is the durable record; the DB stays the queryable replica that `get_trail` and
   `verify_chain` read today.

**Ordered.** `seq` is assigned under the lock at append time; the queue is FIFO;
the writer never reorders; the DB insert is in `seq` order. Two pushes from two
threads get distinct consecutive `seq` and `prev_hash` links in that order.

**Chain hash preserved.** Same fields, same `_compute_hash`, same `created_at`
rule (#48: the hashed timestamp is the stored timestamp). One chain head in memory
under one lock, so journal rows and DB rows are the same chain. `verify_chain`
reads the DB rows and then the journal lines whose `seq` is past the last
replicated row, and checks the link across the boundary.

**Fail closed.** Writer thread dead, journal unwritable, fsync raising, queue
full: `wait_durable` reports not durable, the next push is refused. The journal
directory missing at startup: `log_event` falls back to `'sync'` mode (today's
path, slow but audited), NEVER to the memory-only fallback. The in-memory
fallback remains what it is today for sync callers only.

**Crash between paint and fsync.** The frame was shown; the row is in memory. If
the process dies before the writer's fsync, that ONE row is lost. It is bounded to
one because rule 1 refuses the next push until this row is durable, so at any
instant at most one accepted push is painted and not yet on disk. On restart the
log reconciles: journal rows missing from the DB are replayed in order; the chain
head is whichever store is longer. A tail loss of one row is invisible to
`verify_chain` (the chain simply ends one row earlier), and that is the honest
cost: the alternative that has zero loss is to fsync BEFORE the wake, which is
option B below.

**The two options the steward chooses between.**

| | A: pipelined (above) | B: fsync before wake |
|---|---|---|
| push return time | store + wake + hash: < 1 ms | one journal fsync: 8 ms p50 on ext4 (box unmeasured), 72 ms on this Windows disk |
| rows lost on crash | at most one, the last accepted push | zero |
| ordering, chain, fail-closed | kept | kept |
| sqlite commit and connection churn on the path | gone (replica async) | gone (replica async) |
| meets 16 ms | yes, everywhere | on ext4 SSD yes, on this Windows disk no |

Both remove the ~95 ms of sqlite work from the path; they differ only in whether
the fsync of the LAST row is inside the return. A is the design this document
recommends for the desktop; B is what the reviewer asks for if one lost row is
unacceptable. There is no option that keeps zero loss AND a disk-independent
return time.

**What the security reviewer must sign off.**
1. The one-row tail-loss window (A) or the fsync-on-path cost (B).
2. The journal file joins the audit record: location under the data dir, mode
   0600 hart:hart, append-only, never rotated by this code, carrying the same
   hashes; `verify_chain` and any auditor tooling must read it, not just the table.
3. The in-memory chain head. Today `_get_last_hash` re-reads the head from the DB
   per event, which is what lets TWO processes append to one chain: the agent
   daemon writes `goal_dispatched` rows from its own process (`dispatch.py`
   `dispatch_goal`) while the backend writes `a2ui_push`. That is already a race
   with no cross-process lock (two heads read the same tail and both chain onto
   it: a fork). A memory head in the backend makes the fork certain rather than
   occasional. The reviewer decides between one chain per writer process
   (`chain_id` column, verify per chain) and a cross-process append lock; this
   design assumes per-writer chains, which also removes the 2026-08-12 writer-lock
   retry from the hot path.
4. `os.fsync` semantics: fsync the file after each batch; fsync the directory once
   after the journal is created; on Windows `os.fsync` is `FlushFileBuffers`.
5. Redaction (`_redact_sensitive`) unchanged and applied before append; the row
   content for `a2ui_push` stays type + agent only, no user payload.
6. The `db=` borrowed-session callers and every non-A2UI `log_event` caller stay
   on `'sync'` and are byte-for-byte unchanged.

**The exact test that proves it** (`tests/unit/test_a2ui_audit_off_paint_path.py`,
real `LiquidUIService`, real `ImmutableAuditLog` over a temp journal and the test
DB, the kill switch pinned as in `test_a2ui_gate_hardening.py`):
1. Latency and ordering: patch `os.fsync` with a stub that sleeps 50 ms and records
   its completion time per `seq`. Push 1: assert wall time of `agent_ui_update`
   < 16 ms and the component is in the store before it returns. Push 2: record the
   gate's admission time (the first check after `wait_durable`); assert it is
   later than the recorded fsync completion of push 1's row, and that push 1's
   line is in the journal with its `entry_hash` before push 2 is stored.
2. Fail closed: fsync raising for push 1's row makes push 2 return False, store
   nothing and wake nothing; a writer thread that has exited does the same.
3. Crash: block the writer before fsync, push once, discard the log object, build
   a fresh `ImmutableAuditLog` over the same journal and DB; assert the recovered
   head is the last fsynced row, `verify_chain` is valid, and the un-fsynced push
   is the only row missing (exactly one).
4. Chain across stores: 100 pushes under random fsync delays; journal `seq` strictly
   increasing, `verify_chain` valid over DB plus journal tail, DB tail equals
   journal tail once the writer drains.
5. Mutation: removing the `wait_durable` at the gate fails test 1's admission
   assertion; removing the fsync fails test 3; both are run once with the mutation
   applied and recorded in the commit that lands the code.
6. Budget: `agent_ui_update` gains a 16 ms line in `core.constants.LATENCY_BUDGETS`
   and `tests/unit/test_latency_budgets.py` reads it, so a regression is a red
   build rather than a profile someone has to run.

**Left for the code round (after the decision).** Which option; the chain
question in item 3; the Samsung box fsync number (coordinator); the
`LATENCY_BUDGETS` line lives in `core/constants.py`, not an S7 file.
