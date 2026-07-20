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

`latency = T_photon - T_input`. That is the real number, including compositor
queueing and scanout -- not "time to paint".

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
  delivers its input and presents its surface. That gives an honest A/B: the
  native and WebView shells measured by ONE instrument, so "native is faster" is
  a demonstrated delta, not a claim.

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
component with no budget FAILS the build. This is what makes "full spectrum"
enforceable rather than aspirational -- coverage cannot silently regress.

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
