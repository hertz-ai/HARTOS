# Agentic harness — E2E coverage for non-deterministic journeys

This directory hosts HARTOS's agentic-harness test infrastructure.
A sister harness lives in `Nunba-HART-Companion/tests/journey/` for
desktop-shell journeys; this one covers backend-only journeys that
exercise HARTOS internals (lifecycle_hooks, ResonanceTransaction,
EventBus, consent_service, guardrails).

## What agentic tests are (and aren't)

| Classical unit test | Agentic harness test |
|---|---|
| Asserts exact return value / string match | Asserts verifiable side effects — state transitions, events, ledger rows |
| Deterministic by construction | Tolerates LLM output variance via LLM judge or shape-only assertions |
| Fast, no external services | Real Flask / real EventBus / real DB; mockable provider |
| Fails on any output drift | Fails only when contract breaks — not when wording shifts |

Every journey in `memory/user_journey_coverage.md` that carries
non-determinism (LLM reasoning, peer timing, network flake, user
typing) should be covered here, not in unit tests.

## Files

| File | Role |
|---|---|
| `agentic_harness.py` | Reusable harness — EventRecorder, LedgerProbe, LLMJudge, NFTTimer, `skip_if_missing` gap-marker, `AgenticHarness` orchestrator |
| `test_agentic_harness_J200_J240.py` | 5 representative tests, one per RED cluster (J200, J210, J224, J230, J240) |
| `AGENTIC_HARNESS.md` | This file |

New journey tests follow the pattern: `test_J<id>_<slug>.py`, use
`with harness() as h:`, and assert on `h.events`, `h.ledger`, `h.judge`,
`h.nft`.

## Writing a new agentic test

Minimal template:

```python
from tests.e2e.agentic_harness import harness, skip_if_missing

class TestJxxxMyJourney:
    def test_happy_path(self):
        skip_if_missing(
            'integrations.social.models:SomeRequiredModel',
            'core.some_service',
        )
        with harness() as h:
            run = h.new_run(user_id='u1', prompt_id='p1')
            # drive the agent (POST /chat, emit an event, whatever)
            ...
            # assertions — shape, not exact text
            h.events.assert_emitted('action_state.changed')
            h.ledger.assert_spark_awarded('u1', 'contest:',
                                          min_amount=1)
            h.nft.assert_budget(p50_ms=1500, p99_ms=4500)
```

## Gap marker pattern (RED journeys)

Most J200-J249 journeys are RED — infrastructure not yet built.  For
these, the test SKIPS with a reason that names the missing module:

```python
def test_foo(self):
    skip_if_missing('core.capability_registry')
    # ... driver ...
```

The skip output in CI becomes the gap tracker:

```
SKIPPED: tests/e2e/test_j230.py::TestJ230::test_gpu_delegation —
agentic-harness skip — infrastructure not yet built:
core.capability_registry (ModuleNotFoundError)
```

When the BUILD task lands, the `skip_if_missing` call becomes a
no-op and the real assertions run.  **Never delete a skip to hide a
gap.**  Always replace with real assertions once the infra lands.

## Environment variables

| Var | Default | Effect |
|---|---|---|
| `HEVOLVE_TEST_LLM_JUDGE` | `0` | When `1`, `LLMJudge` calls the real local LLM for rubric scoring.  When `0` (CI default), heuristic rules only — deterministic + fast. |
| `NUNBA_USE_LIVE` | unset | When `1` + a live Nunba on :5000, the Nunba journey harness talks to it.  No effect here (HARTOS tests bind to in-process Flask). |
| `HARTOS_MCP_DISABLE_AUTH` | unset in prod | Tests set this so MCP `before_request` yields.  Consistent with `Nunba/tests/journey/conftest.py:95-100`. |

## Running

```bash
# All agentic-harness tests (CI default — fast, heuristic judge)
pytest tests/e2e/test_agentic_harness_*.py -v

# One cluster
pytest tests/e2e/test_agentic_harness_J200_J240.py::TestJ200MultiPersonaReviewer -v

# With LLM judge locally (slower, more thorough rubric scoring)
HEVOLVE_TEST_LLM_JUDGE=1 pytest tests/e2e/test_agentic_harness_*.py -v

# Report which RED journeys are still skipped (gap dashboard)
pytest tests/e2e/test_agentic_harness_*.py -v -rs | grep SKIPPED
```

## Coverage status (2026-04-23)

This file is the E2E-side of `memory/user_journey_coverage.md`.
Current representative coverage:

| Cluster | Journey | Status |
|---|---|---|
| Multi-persona + HITL | J200 state-seq shape | SKIP (driver gap) |
| Multi-persona + HITL | J201 reviewer-reject retry | SKIP (gap) |
| Multi-persona + HITL | J202 consent-keyword PREVIEW | SKIP (gap) |
| Multi-device | J210 history sync | SKIP (gap — BUILD-3) |
| Multi-device | J212 3-device concurrent | SKIP (gap) |
| Multi-device | J216 device_roster endpoint | SKIP (gap — BUILD-4) |
| Multi-channel | J224 channel→persona binding | SKIP (gap — schema column) |
| Multi-channel | J228 per-channel rate-limit E2E | SKIP (unit covered; E2E gap) |
| Capability | J230 GPU auto-delegation | SKIP (gap — BUILD-2) |
| Capability | J231 constitutional-filter block | PARTIAL — asserts filter trips |
| Capability | J232 circuit-breaker halt | SKIP (gap) |
| Consent | J240 estimate_weekly_spark helper | SKIP (helper not built) |
| Consent | J248 consent.changed emit | SKIP (helper present, emit missing) |
| Consent | J241 revoke mid-task drain | SKIP (gap) |

**Every skip above names the specific missing module in its reason.**
Track via:

```bash
pytest tests/e2e/test_agentic_harness_*.py -rs 2>&1 \
    | grep -E "^SKIPPED|agentic-harness skip" > gap_dashboard.txt
```

## Contract

1. **Assertions are on side effects, never on exact text.** Shape,
   state, metric — not tokens.
2. **LLMJudge is opt-in, heuristic is default.** CI never depends
   on a live model responding a particular way.
3. **Every RED journey SKIPS with a reason that names the missing
   module.** Never mask a gap with `pass`.
4. **Assertions wrap existing primitives.** No parallel ledger, no
   shadow pub/sub. See `agentic_harness.py` — every helper calls
   through to an existing HARTOS service.
5. **Budget every NFT you claim.** If a test asserts latency, use
   `h.nft.assert_budget(...)`.  Don't hand-roll time math.

## Cross-references

- [memory/user_journey_coverage.md](../../../.claude/projects/C--Users-sathi-PycharmProjects-HARTOS/memory/user_journey_coverage.md)
  — the journey catalog these tests implement
- [memory/parity_audit.md](../../../.claude/projects/C--Users-sathi-PycharmProjects-HARTOS/memory/parity_audit.md)
  — cross-repo UI tests live in Nunba/tests/journey/; backend E2E
  stays here
- [memory/idle_compute_workstream.md](../../../.claude/projects/C--Users-sathi-PycharmProjects-HARTOS/memory/idle_compute_workstream.md)
  — feeds J240-J249 designs
- `Nunba-HART-Companion/tests/journey/PRODUCT_MAP.md` — canonical
  J01-J199 catalog
