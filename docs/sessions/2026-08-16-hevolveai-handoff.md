# HANDOFF — hevolveai agent

Written 2026-08-16 from the **Nunba desktop host's** runtime logs
(`~/Documents/Nunba/logs/{langchain,agent_system,gui_app}.log`), where the
hevolveai supervisor runs as a child process and forwards its stdout under a
`hevolveai:` prefix.

**Scope rule used throughout:** a line prefixed `hevolveai:` (logger
`hevolve_agent_engine`, thread `hevolveai-supervisor`, module
`embodied_ai.context`) is yours. Everything else is Nunba/HARTOS and stays
with us. This split is the steward's call, not an inference.

**2026-08-18 ADDENDUM AT THE END OF THIS FILE, and it is addressed to HARTOS rather than to hevolveai:** every hevolveai learning flag is OFF in production, and the decision to change that lives in `_build_env` in this repo. See *ADDENDUM 2026-08-18* below. Note before checking yourself: 48 `HEVOLVE_*` variables ARE set in production and they are Nunba product flags that merely share the prefix.

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


---

## ADDENDUM 2026-08-18 -- DIRECTION REVERSED: FROM hevolveai TO HARTOS

Everything above was written FROM the HARTOS/Nunba side TO the hevolveai agent.
This section goes the other way: it is written for **whoever owns HARTOS**, by
the hevolveai side, and it concerns a decision that can only be made in this
repo. Nothing here has been acted on and no HARTOS code has been modified.

### THE FINDING, IN ONE PARAGRAPH

hevolveai's learning and serving behaviour is gated behind roughly **62
environment flags**. Production -- the child that `hevolveai_supervisor.py`
spawns -- has **exactly one of them set, and it is `HEVOLVEAI_PORT`**, a port
number. Every behavioural change built, tested, pinned and proved
byte-identical-when-off since C121 is therefore **correct, pinned, and
dormant**: the code paths those proofs describe run only when a flag is set,
and in production no flag is set. This is not a correctness problem and it is
not a bug in either repo. It is that **promotion has never been performed**,
and promotion belongs to the boot, which lives here.

### THE EVIDENCE, AND THE METHOD IT DEPENDS ON

The method matters more than the number, because the obvious way to check this
gives a confidently wrong answer (see the prefix trap below).

* **Read from the process, not inferred from logs.** The environment block of
  the live production process (pid 38648) was read directly via
  `NtQueryInformationProcess` -> PEB -> `ProcessParameters.Environment`: **153
  entries**. Absence of a flag in that block is direct evidence, not the
  absence of a log line.
* **Behavioural confirmation, independent of the environment read.**
  `RealityGroundedLearner.learn_from_reality` executed on `[thread=MainThread]`
  **603 times** in one day, against **49** on a worker thread. With
  `HEVOLVE_LEARN_OFF_LOOP=1` that call is dispatched through
  `asyncio.to_thread` onto a worker, so **603 MainThread executions are only
  possible with the flag unset.** Two independent methods, same answer.

### THE PREFIX TRAP -- READ THIS BEFORE CONCLUDING THE FLAGS ARE ALREADY ON

Production's environment **does** contain **48 variables starting with
`HEVOLVE_`**: `HEVOLVE_FLAG_MENTIONS`, `HEVOLVE_TTS_ENABLED`,
`HEVOLVE_CODING_AGENT_ENABLED`, `HEVOLVE_SPECULATIVE_ENABLED`, and so on.

**These are Nunba PRODUCT feature flags, read by HARTOS. They have nothing to
do with hevolveai's learning flags and merely share the prefix.** Anyone who
checks "are the HEVOLVE flags set in production?" will get a truthful `yes`
that means nothing, and will conclude the learning work is already live. It is
not. The learning flags are the ones hevolveai's own source reads, and of those
62, exactly one (`HEVOLVEAI_PORT`) is present.

### THE EXACT SITE

`integrations/agent_engine/hevolveai_supervisor.py`, `_build_env`, **lines
731-761**. It starts from `dict(os.environ)` and then sets:

| Variable | Value |
|---|---|
| `HEVOLVEAI_API_URL` | the resolved API URL |
| `HEVOLVEAI_PORT` | the resolved port |
| `HEVOLVE_LAUNCHED_BY` | `'hartos'` |
| `HEVOLVE_DEVICE` | `setdefault('cpu')` |
| `HEVOLVE_LOCAL_LLM_URL` | only when unset, from `port_registry` |
| `PYTHONPATH` | prepended |

**No learning flag is set here, and none is deliberately cleared either.** They
are never mentioned anywhere in the spawn path. **That distinction is the whole
point: this is an omission, not a policy.** Nobody decided these should be off;
the question was never posed at this layer. (`NUNBA_BUNDLED=1` is also present
in the child, inherited from HARTOS's own parent rather than set here. It is
what sends hevolveai's log to `~/Documents/Nunba/logs/` instead of the repo
tree, which is worth knowing before reading either log as authoritative.)

### PER-FLAG STATUS, HONESTLY GRADED

This table exists to be used for a turn-on decision, so the evidence column is
graded rather than uniform. **Where the evidence is thin it says so.** "Proved
byte-identical flag-off" means the OFF path was shown unchanged, which makes
enabling reversible, not that the ON path has run in production.

| Flag | What it does when ON | Evidence it is safe | Promotion |
|---|---|---|---|
| `HEVOLVE_LEARN_OFF_LOOP` | learning runs on a worker via `asyncio.to_thread` instead of on the serving thread | **Strongest.** C162 (ingest path) and C164 (distill path), both with flag-off byte-identical proof; a shared mutex serialises worker learns so no pair is skipped | **Safety settled.** Open question is the boot, not the change |
| `HEVOLVE_DIVERGENCE_BREAKER` | refuses a learning update whose post-error exceeds k times its pre-error | **Strong and LIVE-tested.** C236/C237, validated over **489 live learning events: 1 trip on a genuinely divergent event, 0 false positives on healthy ones** | Safety well evidenced; enabling is a policy choice about refusing updates |
| `HEVOLVE_TEACHER_OFF_LOOP` | the teacher generate runs off the serving loop, with a teacher-backend lock | Moderate. M2.1-B, implemented and reasoned (llama HTTP is concurrent-safe, the shared transformers path is locked). **No live production validation** | Open. **This is the flag that actually addresses latency (below)** |
| `HEVOLVE_NOVELTY_LEARN_OFF_LOOP` | defers novelty-triggered learning off the reflex path | Moderate. C245, exercised over 140 ingests / 119 learning events on a verification boot | Open |
| `HEVOLVE_VALIDATE_PREDICTION` | feeds the world-model prediction validator that had zero callers | Moderate. C241 wired a dead producer to four real consumers; default OFF keeps the metric absent, which is the honest default | Open, and low urgency: it is observability, not behaviour |
| `HEVOLVE_REPLAY_UNION` | widens replay sampling to a union strategy | **Thin.** Default off leaves the wiring byte-identical; no live validation | **Do not promote on this document's evidence** |
| `HEVOLVE_CODEC_RECON` | enables a codec reconstruction term | **Thin.** Staged in `scripts/start_with_tracing.bat` only, i.e. the dev tracing entry; no production evidence | **Do not promote on this document's evidence** |
| ~55 others | assorted | Not assessed here | Not assessed |

### THE LATENCY CORRECTION -- THE THING MOST LIKELY TO BE ACTED ON WRONGLY

It is tempting to read "learning runs on the serving thread" and conclude that
`HEVOLVE_LEARN_OFF_LOOP` will fix hevolveai's slow HTTP responses. **It will
not.** Measured over one full day from production's own timing records, total
occupancy of the serving thread:

| Operation on the serving thread | Total | Calls | Mean |
|---|---|---|---|
| `_generate_text_only_response` | **9022 s** | 567 | 15.9 s |
| `_generate_response` | **7917 s** | 571 | 13.9 s |
| `learn_from_reality` | **1587 s** | 604 | 2.6 s |

**Generation outweighs learning 10.7 to 1. Learning is 8.6% of the blocking
work.** Promoting `LEARN_OFF_LOOP` alone moves that 8.6% and leaves the rest.
Confirmed by direct correlation rather than inference: three `/health` probes
measured **15.5 s, 0.0 s, 0.0 s** -- health is instant when the thread is free
and delayed by exactly the remainder of whatever holds it. The 15.5 s wait
released the moment a generation exited, and learning accounted for 1.22 s of
it.

**What is generating.** `[QwenDistillation] [Autonomous] No seeded goals --
SYNTHETIC fallback query #N`, **2280 log lines in one day**, one enqueued every
40 to 60 seconds, each driving a teacher generation on the serving thread.
Production is largely occupied **talking to itself**.

**Two levers, and both are HARTOS's:**

1. **`HEVOLVE_TEACHER_OFF_LOOP=1`** moves that generation off the serving loop.
   This is the flag that addresses the dominant term.
2. **Seed goals, which is the DESIGNED cure.** The synthetic query is a
   *fallback* taken only when the goal queue is empty: with goals queued the
   engine consumes a real goal and never generates a synthetic one. hevolveai
   exposes `POST /v1/goals/seed` for exactly this, and **HARTOS does not
   currently call it** (`integrations/agent_engine/goal_seeding.py` seeds the
   agent engine's own goals, not hevolveai's). Feeding real goals would replace
   self-generated filler with useful work at no cost to the loop.

### RECOMMENDED ORDER, AND WHAT THIS DOCUMENT IS NOT

If the decision is taken to promote anything, this order reflects
evidence-strength and blast radius, strongest and safest first:

1. **`HEVOLVE_LEARN_OFF_LOOP`** -- best-evidenced (C162 + C164), reversible by
   construction, removes learning from the serving thread. Do it first because
   it is the one whose safety question is genuinely closed.
2. **Seed real goals via `POST /v1/goals/seed`** -- no flag at all, addresses
   the dominant latency term at its source, and converts wasted teacher calls
   into useful ones. Second because it needs integration work, not a flag.
3. **`HEVOLVE_TEACHER_OFF_LOOP`** -- the remaining latency lever. Third
   because its safety rests on reasoning rather than on live validation, so it
   deserves a watched rollout.
4. **`HEVOLVE_DIVERGENCE_BREAKER`** -- independently of latency. Its live
   record (489 events, 1 true trip, 0 false positives) is the best empirical
   evidence any of these flags has.
5. Everything else, only after someone re-validates it. `REPLAY_UNION` and
   `CODEC_RECON` in particular must not be enabled on the strength of this
   document.

**THIS DOCUMENT DOES NOT AUTHORISE ANY OF THE ABOVE.** It exists so that the
decision is executable by someone who did not follow the campaign. No flag was
enabled, `_build_env` was not modified, no HARTOS code was touched and nothing
was restarted. Turning any of this on is the owner's call.

### ONE ITEM ABOVE THIS LINE IS NOW ANSWERED

Section 3 reported `LOCAL_COLLECTIVE (latency=4750.2ms ... timeout=200ms)` and
marked the distribution UNVERIFIED. The cause has since been found on the
hevolveai side: `SharedLatentManifold._cayley_matrix` recomputed an O(D^3)
`torch.linalg.solve` at D=2048 on every projection, and the thinking path calls
the projections many times per step. It is now memoised on the parameter's
version counter (measured ~333x on the repeated call, ~1433x on the hot path),
with no flag involved. **Caveat: the fix is in source and the shipped `.pyd`
predates it, so it takes effect at the next Cython rebuild, not at the next
restart.** The `timeout=200ms` still not being enforced is a separate issue and
remains open.
