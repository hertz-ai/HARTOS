# Agentic-harness plan — 2026-08-16

Every instruction the steward gave, then the plan gated by them.
Written because the instruction set is now larger than one turn can
hold, and because several of them are *constraints on how* rather than
*what*, which is exactly the kind of thing that gets dropped.

---

## PART 1 — THE INSTRUCTIONS (verbatim intent, grouped)

### A. Standing method constraints (apply to EVERY change)

| # | Instruction |
|---|---|
| A1 | Zero regression. Zero parallel paths. Do not reinvent — leverage existing code. |
| A2 | "your greps usually miss things" — widen the search before concluding |
| A3 | Do NOT change code unless confident, with full context AND the full caller list |
| A4 | Be minimalistic, align with existing code |
| A5 | Do not create new methods — work at the canonical point where we read first |
| A6 | Fix the ROOT CAUSE where design intent is not met, never the symptom; never fix for this machine alone |
| A7 | Test what we ship — never claim fixed without live verification |
| A8 | HARTOS + Nunba are multi-platform BY DESIGN: code platform-aware, functionality platform-agnostic |
| A9 | MAIN branch only. No `claude/*`, no worktrees. Never force-push. Never `--no-verify` |
| A10 | No `Co-Authored-By: Claude` in commits |
| A11 | Never commit `proof_reports/`, `pip/`. Stage explicit paths |
| A12 | Never hand-edit bundled or installed copies — canonical source only |
| A13 | Commit and push always, so work is not lost |

### B. Quality gates the steward attached to THIS plan

| # | Gate |
|---|---|
| B1 | Full harness |
| B2 | Full regression run |
| B3 | Full parallel-path scan |
| B4 | Full callers audit |
| B5 | 100% test coverage, including edge cases |
| B6 | **Functional RUNTIME tests — NOT source-code tests** |
| B7 | **LLM in the loop at runtime** |
| B8 | Gate the PLAN with B1-B7 — *not* a rebuild.  No rebuild was asked for. |

### C. Concrete work items requested

| # | Item | State |
|---|---|---|
| C1 | Check HARTOS booted as OS, running without hangs; restart ONLY if clean | ANSWERED — `hart-node` 192.168.0.69 booted, stack NOT serving → restart correctly withheld.  Blocked on SSH key. |
| C2 | Check via Nunba (not raw network probes) | DONE — MCP `system_health` + peer discovery |
| C3 | Log analysis: admin/GGUF, chat retries, errors/warnings | DONE |
| C4 | Fix log separation cleanly — redundancy OK, nothing gulped | DONE — HARTOS `029956d1`, Nunba `1e73eaaa` |
| C5 | GGUF: "no preset for Qwen3.8-27B-UD-Q4_K_XL.gguf" | DONE — HARTOS `0c7570ef` + Nunba `6a8f601d` |
| C6 | Harness around the agentic framework | THIS PLAN |
| C7 | Where is the hevolveai handoff doc | DONE — created, `38e7491b` |
| C8 | Fix `openai.APITimeoutError` | **NOT STARTED** |
| C9 | Why no request logging / silent exception gulps | PARTIAL — proven at the data layer (0/1000 cause fields) |
| C10 | langchain port: in-process via `hart_intelligence_entry`; 6777 Docker-only; Nunba bundled exposes 5000 only | ACCEPTED — probe still to fix (#460) |
| C11 | Most common issue preventing agents completing | ANSWERED — nothing consumes the queue; 6 revisions, root cause NOT pinned |
| C12 | Delete the broken catalog row | DONE — `200 {"success": true}` |
| C13 | P2P / central-federation sync for new model additions | **NOT STARTED** |
| C14 | Research Opik before writing our own | DONE — verdict below |
| C15 | Check the latest built app | DONE — build is 14 Aug; **none of today's 5 commits are in it** (byte-verified) |

---

## PART 2 — OPIK VERDICT (C14)

Apache-2.0, `pip install opik`, self-hostable in full
(`opik.configure(use_local=True)`, Docker Compose / K8s), `@track`
decorator, OpenTelemetry, agent trace trees over LLM calls + tool
executions, LLM-as-judge metrics, **native PyTest integration**.

**Adopt for:** LLM-path tracing + evaluation in dev/CI.  Self-hosting
satisfies the sovereignty rule (nothing leaves the box).

**Do NOT adopt for the invariant class that actually broke:** every
failure found on 2026-08-16 was a *process* failure, not an LLM-quality
failure — daemon thread not alive, ledger not advancing for 14h, a
request-id lost across a thread boundary, an event routed at a
non-existent user, an admin route accepting an undownloadable row.
Opik traces what the model did; it does not assert "my worker is alive
and my queue is draining."  It is also a Docker-backed service and
cannot ship inside a cx_Freeze desktop app.

**Therefore:** Opik for trace/eval (dev + CI only, never bundled);
write the invariant probes ourselves.

---

## PART 3 — PARALLEL-PATH SCAN RESULT (B3)

**A harness already exists.**  Building a second one would have been
the exact violation A1/A4 forbid:

```
HARTOS/tests/e2e/AGENTIC_HARNESS.md
HARTOS/tests/e2e/agentic_harness.py      EventRecorder, LedgerProbe,
                                          LLMJudge, NFTTimer, AgenticHarness
HARTOS/tests/e2e/test_agentic_harness_J200_J240.py
HARTOS/scripts/probe_unification_layer_e2e.py
Nunba/scripts/probe_liquid_ui_audit.py, staging_e2e_probe.sh
Nunba/tests/journey/                      (sister harness, desktop shell)
```

Its charter already matches B6 exactly — *"Real Flask / real EventBus /
real DB"*, *"asserts verifiable side effects — state transitions,
events, ledger rows"* — and B7 is already supported via
`HEVOLVE_TEST_LLM_JUDGE=1`, which makes `LLMJudge` call the real local
LLM instead of heuristics.

`opik` is NOT currently a dependency in either repo.

**The gap:** `LedgerProbe` covers resonance / spark / consent.  It has
**no** probe for the agent task ledger draining, and none for daemon
liveness.  That is precisely the invariant class that failed silently
for 14 hours.

---

## PART 4 — THE PLAN

Each step names the canonical point it extends (A5), its caller
audit (B4), and its runtime proof (B6/B7).

### P1 — Extend `LedgerProbe` with the two missing runtime invariants
Canonical point: `tests/e2e/agentic_harness.py::LedgerProbe`.
Reads through the loopback route `/api/agent-engine/ledger/stats`,
which already carries `daemon` liveness as of `22671142` — the
in-process authority.  No new class, no new route.

- `agent_engine_stats()` → dict | None (None when unreachable, matching
  the class's existing "return None and let the caller skip" contract)
- `assert_daemon_alive()`
- `assert_ledger_advancing(within_minutes=N)`

### P2 — Functional runtime journey tests (B5/B6/B7)
`with harness() as h:` driving REAL Flask, asserting side effects.
Edge cases enumerated: daemon stopped, daemon zombie
(`running=True, thread_alive=False`), tick static, queue empty vs
queue stalled, route unreachable, malformed payload.
LLM in the loop via `HEVOLVE_TEST_LLM_JUDGE=1`.

### P3 — Replace my own source-code test
`tests/unit/test_daemon_liveness_authority.py::TestAdditiveOnly` uses
`inspect.getsource` — that is a source-code test and violates B6.
Replace with a runtime assertion against a real response.

### P4 — Ledger records WHY (C9) — DONE `3520d7aa`
`lifecycle_hooks.py` set `set_blocked_reason` for BLOCKED and **nothing**
for FAILED.  `failure_reason` 0/1000 vs `blocked_reason` 44/1000, and
`Task.set_failure_reason()` had **zero callers in the whole tree** — a
setter written and never wired.  Symmetric branch added at the same
site; `FailureReason` gained `ABANDONED` in its canonical home for the
GAVE_UP case (nothing threw, retries were not the limit, so neither
ERROR nor MAX_RETRIES_EXCEEDED is true).  5 tests in the EXISTING
`TestAutoSyncToLedger`; proven red pre-fix by reverting only the source.

### P5 — Full regression (B2) + push (A13) — DONE

**Result: 20 failed / 174 passed / 211 skipped, ALL 20 pre-existing.**
Proven, not assumed: `tests/e2e/test_e2e_pipelines.py` run ALONE — with
the new journey file absent — reproduces 19 of them (19 failed / 62
passed).  Both sampled failures PASS individually, so the file corrupts
its own process: 16× `cv2.dnn has no attribute 'DictValue'` and 2×
`torch ... already has a docstring` (a double-import under two module
identities — the shadow-module family, not ordering).  Tracked as #655.

Two tooling traps re-confirmed here, both mine:
- `cmd | tail; echo $?` reports **tail's** status.  The regression
  printed `exited with code 0` while pytest had 20 failures.
- I grepped the captured output for my own modules, got 0, and nearly
  read that as exoneration — but the capture was 27 summary lines with
  **no tracebacks at all**.  A check that cannot fail is not a check.
  The real attribution came from re-running the file without my code.

### C8 — `openai.APITimeoutError` — NOT REPRODUCIBLE, do not "fix"

I filed this as "16x APITimeoutError, fix it".  Re-checked today and
**I cannot reproduce it**, so there is nothing to fix yet:

| Search | Result |
|---|---|
| all `~/Documents/Nunba/logs` incl. rotated `.log.1` | **0** occurrences |
| `HARTOS/logs` | 0 |
| `git grep APITimeoutError` in HARTOS `*.py` | not referenced |
| `git grep APITimeoutError` in Nunba `*.py` | not referenced |
| positive control: `Traceback` in same logs | **4,906** |
| positive control: `openai` in HARTOS source | found |

Both controls pass, so the search machinery is sound and the zeros are
real absence rather than a dead pattern (the `grep -E` `\|` lesson).
Either the occurrences rotated out of retention, or my original count
came from a window I no longer hold.  Either way, acting now would mean
writing a fix for a phantom — the #620 pattern.

NOT a claim that it never happened.  To act on it we need a fresh
capture: the exception is raised by the `openai` SDK itself, so it can
only be attributed from a live traceback naming the call site.

Also worth noting but NOT concluded: `APITimeoutError` being unreferenced
does **not** prove it is unhandled — a surrounding `except Exception`
would still catch it.  That distinction needs checking at the real call
seam before anyone files it as a gap.

### Deferred, explicitly NOT silently dropped
C13 (model P2P/federation sync), the #460 langchain probe fix (in
progress — the port literal is already resolved via `port_registry`;
the REAL defect is that the probe unconditionally HTTP-dials a port that
does not exist in bundled mode, where langchain is in-process), and the
un-pinned daemon root cause (C11).

### Live ledger, re-measured
`completed` moved +35 and `pending` +432 between two readings, then
**zero** terminal transitions across a 60s window.  So the engine is not
dead — it works in bursts and the queue grows ~12:1 faster than it
drains.  This also RETRACTS my earlier "nothing completed in 14 hours",
which was a sampling artifact: `limit=1000` per status against 1,300
completed rows could never have shown the newest ones.

---

## PART 5 — HONESTY LEDGER FOR THIS SESSION

Six wrong root-cause claims on the daemon outage, each corrected by
evidence:

1. tasks start then hang → wrong, 0 in flight
2. engine dead 14h → wrong mechanism, alive until 10:16
3. `user_active` a stuck latch → wrong, re-stamped not stuck
4. leaked `_active_create_sessions` → wrong, the reason label
   distinguishes `create_in_flight` and the logs said `user_active`
5. starvation-override livelock → unsupported once the gate's own
   transition log showed 6-7s closes, not a 10h block
6. "no host on the /24 has port 22" → false negative from my own scan

Two were self-inflicted tooling errors: `grep -E` with `\|` (a literal
pipe in ERE, so every alternation silently matched nothing) and a
fixed-sleep async port scan polled with `WaitOne(0)`.

**Rule earned:** a negative result from a scan I wrote is not evidence
until the scan is validated against a known-positive control.
