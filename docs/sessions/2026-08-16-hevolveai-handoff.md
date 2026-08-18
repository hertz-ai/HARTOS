# HANDOFF — hevolveai agent

Written 2026-08-16 from the **Nunba desktop host's** runtime logs
(`~/Documents/Nunba/logs/{langchain,agent_system,gui_app}.log`), where the
hevolveai supervisor runs as a child process and forwards its stdout under a
`hevolveai:` prefix.

**Scope rule used throughout:** a line prefixed `hevolveai:` (logger
`hevolve_agent_engine`, thread `hevolveai-supervisor`, module
`embodied_ai.context`) is yours. Everything else is Nunba/HARTOS and stays
with us. This split is the steward's call, not an inference.

---

## HOW THESE NUMBERS WERE TAKEN (read before trusting them)

Counts are `grep -ohE <pattern> <3 logs> | wc -l` over the live files.

**Two corrections already applied, so you inherit the corrected version:**

1. My first pass used `grep -E` with `\|` for alternation. In ERE `\|` is a
   **literal pipe**, so those patterns matched nothing and reported `0`. Every
   alternation count below was re-taken with real ERE.
2. Every "0" below is accompanied by a **positive control** on the same files
   (`Distillation` → 3613). A zero therefore means absence, not a dead pattern.

Anything not measured here is marked UNVERIFIED. Please do not promote an
UNVERIFIED line to a fact without your own measurement.

---

## 1. NaN is propagating through the live learning loop  — PRIMARY

This is the dominant signal by two orders of magnitude. **24,843** occurrences
of `nan` across the three logs.

| field | count |
|---|---|
| `correction_norm=nan` | 8452 |
| `confidence=nan` | 1300 |
| `conf=nan` | 738 |
| `avg_sim=nan` | 650 |
| `template=nan` / `norm=nan` / `mean=nan` / `decode=nan` | 648 each |

Representative lines (verbatim, `langchain.log`, 2026-08-16 13:14):

```
[IntegratedRealtimeAgent] [HierarchicalMemory] Retrieved 20 similar
    experiences (avg_sim=nan)
[SemanticReasoner] forward: grounded=1, relations=0, intent=navigate (conf=nan),
    store_memory=False
[MetaLearningRouter] _learning_step decision: action=create, task_ids=[],
    confidence=nan, pred_error=0.100000
[KernelContinualLearner] forward: inducing_points=3, correction_norm=nan,
    num_corrections=3
```

**Why this is worse than a noisy metric.** `MetaLearningRouter` is *taking a
decision* (`action=create`) while its own `confidence` is NaN. Every
IEEE-754 comparison against NaN is false, so any `if confidence > threshold`
gate silently takes its else-branch, permanently and invisibly. A NaN
confidence does not fail loudly — it quietly disables whatever the threshold
was protecting. `num_corrections=3` alongside `correction_norm=nan` says
corrections are being *applied*, not skipped.

Suggested entry points (from the log module paths, not from reading your
source — treat as leads):
`embodied_ai.context`, `KernelContinualLearning.forward`,
`SemanticReasoner.forward`, `HierarchicalMemory` similarity,
`MetaLearningRouter._learning_step`.

`avg_sim=nan` over 20 retrieved experiences smells like a zero-norm vector in a
cosine denominator — a plausible single upstream source for the rest. UNVERIFIED.

---

## 2. Hidden-state distillation is skipping consecutive items — 16×

```
ERROR: [Distillation] hidden-state distillation has skipped N consecutive
       items for want of a <...>
```

16 at ERROR level (each also mirrored via `embodied_ai.context:80`, so 32 log
lines for 16 events — don't double-count). `Distillation` appears 3613 times
overall, so the subsystem is very active and this is the failing minority.

---

## 3. HiveMind exceeds its own latency budget by ~23× — 650 samples

```
[HiveMind] LOCAL_COLLECTIVE (latency=4750.2ms, agents=['realtime_agent'],
    confidence=1.000, reality_sig=0.5, fusion=attention, timeout=200ms)
```

650 `LOCAL_COLLECTIVE` events. The one sampled shows **4750.2ms against a
declared `timeout=200ms`** — and it still returned `confidence=1.000`, i.e. the
timeout is not enforced and full confidence is reported on a grossly
over-budget result. Whether all 650 are over budget is UNVERIFIED; I sampled,
I did not aggregate the latency distribution.

---

## 4. Smaller hevolveai-prefixed errors

- `[ContinuousProofMonitor] Error analyzing proof:` — 1×
- `asyncio: Task exception was never retrieved` — 1×
- `asyncio: Accept failed on a socket` — 1×
- 5 tracebacks under the `hevolveai:` prefix

---

## EXPLICITLY NOT YOURS — we are keeping these

Listed so nobody works them twice:

| signal | count | owner |
|---|---|---|
| `openai.APITimeoutError` on the autogen path | 16 | **Nunba/HARTOS** |
| `Exception in callback _ProactorBasePipeTransport._call_connection_lost` | 24 | **Nunba/HARTOS** (no hevolveai prefix) |
| `tool_logging.py` errors | 2 | **Nunba/HARTOS** |
| `LLM download: no preset for <gguf>` | — | **FIXED** — HARTOS `0c7570ef`, Nunba `6a8f601d` |
| `SSE broadcast refused (P3a)` / silent misroute | — | **FIXED** — HARTOS `21a4f6e0` |

---

## RETRACTED FROM THE EARLIER VERBAL HANDOFF

I previously told the steward this batch was yours: **154× shape mismatch,
7× distillation, 1× checkpoint, 1× embodied init.**

Re-measured against the current logs with a validated pattern:

- `shape mismatch|size mismatch|Expected .* got` → **0**
- `checkpoint` → **0**

Those counts came from an earlier window or a different file and I did not
re-verify them before repeating them. **Do not chase shape-mismatch or
checkpoint issues on the strength of my earlier statement.** If they matter,
measure first. The distillation item survives re-measurement (§2) at 16, not 7.

---

## WHAT I DID NOT DO

- Did not read hevolveai source. Every "entry point" above is inferred from log
  module paths and is a lead, not a diagnosis.
- Did not aggregate the HiveMind latency distribution (§3) — one sample only.
- Did not trace the NaN to its origin. §1's zero-norm hypothesis is untested.
- Did not run hevolveai's own test suite.

---

## PROVENANCE

Host: Windows desktop `MSI` (192.168.0.165). Nunba self-reports healthy —
Flask :5000 up, LLM :8080 up, DB up (143 users), uptime ~19.7h, 48 agents.
So these errors are from a **running, otherwise-healthy** system, not a
broken boot.

Unrelated but worth knowing if you touch the fleet: the HART OS box is
`192.168.0.69` (`hart-node`, dual-boot), and `192.168.0.9` (`sathish-linux-deep`)
is a *different* machine — see `2026-08-12-HANDOFF.md`, which already documents
both. Its stack was not serving at the time of writing.
