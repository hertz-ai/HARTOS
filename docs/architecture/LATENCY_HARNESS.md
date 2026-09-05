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
