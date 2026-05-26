"""Agentic-harness helpers for non-deterministic E2E journey tests.

Sister to `Nunba-HART-Companion/tests/journey/conftest.py` — that harness
drives Nunba live; this one lives in HARTOS so backend-only journeys
can be exercised without the electron layer.  Use both: Nunba's for
journeys that need the desktop shell, this one for journeys that need
HARTOS internals (lifecycle_hooks, ResonanceTransaction, EventBus,
consent_service, compute_mesh_service, ConstitutionalFilter).

## What an agentic-harness test is

A classical unit test asserts exact output.  An agentic test drives a
REAL (or real-shaped) agent run, tolerates LLM output variance, and
asserts on the verifiable SIDE EFFECTS:

  - state transitions (ActionState machine)
  - EventBus emissions
  - Spark ledger rows
  - audit log entries
  - guardrail hash integrity
  - latency envelopes (NFT)

Non-determinism is handled by:
  - asserting state-sequence SHAPE, not token content
  - asserting metric ranges, not exact values
  - `LLMJudge` for natural-language output — picks deterministic
    stand-in when HEVOLVE_TEST_LLM_JUDGE=0 (default in CI) and real
    LLM judge when =1 (local)
  - tolerance retries for transient flakes

## Module contract

- `AgenticHarness(flask_app)` — context manager, wires EventBus +
  ledger probe for the test's lifetime
- `EventRecorder` — accumulates emitted events; assertion helpers
- `LedgerProbe` — reads ResonanceTransaction, ConsentAuditLog,
  AgentAttribution, ActionState (when available)
- `LLMJudge` — score-by-LLM-or-heuristic
- `NFTTimer` — measure LAT p50/p99 across a block
- `skip_if_missing(*modules)` — pytest.skip guard for RED-journey
  infra that isn't built yet; the skip is the gap marker

## CLAUDE.md compliance

- Gate 2 (DRY): every assertion here wraps an existing primitive —
  EventBus, ResonanceService, lifecycle_hooks.  No parallel ledger,
  no shadow event stream.
- Gate 5 (test-first): this module IS the test scaffold that future
  BUILD tasks land against.
- Memory: journeys covered are documented in
  `memory/user_journey_coverage.md` (J200-J249).
"""
from __future__ import annotations

import contextlib
import importlib
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pytest


# ─── Skip guard for RED-journey gaps ────────────────────────────────

def skip_if_missing(*module_paths: str) -> None:
    """Skip the test (with a descriptive reason) when any listed module
    or attribute path doesn't import.  RED journeys from
    user_journey_coverage.md use this so the skip output serves as a
    gap marker: CI shows "SKIPPED: J210 — session_handoff_service
    not available yet" until the feature lands.

    Accepts either a dotted module path or a `module:attr` form to
    check a specific symbol.
    """
    missing: List[str] = []
    for path in module_paths:
        try:
            if ':' in path:
                mod_path, attr = path.split(':', 1)
                mod = importlib.import_module(mod_path)
                if not hasattr(mod, attr):
                    missing.append(path)
            else:
                importlib.import_module(path)
        except Exception as exc:
            missing.append(f'{path} ({exc.__class__.__name__})')
    if missing:
        pytest.skip(
            'agentic-harness skip — infrastructure not yet built: '
            + ', '.join(missing)
            + ' (see memory/user_journey_coverage.md for the RED list)'
        )


# ─── EventBus recorder ──────────────────────────────────────────────

@dataclass
class RecordedEvent:
    topic: str
    data: Any
    at: float


class EventRecorder:
    """Subscribes to the platform EventBus for the test's lifetime and
    records every emission.  Uses the existing EventBus `on`/`off`
    API — no parallel pub/sub.  Gracefully no-ops when the platform
    isn't bootstrapped (import-only tests)."""

    def __init__(self, topic_filter: str = '*'):
        self._filter = topic_filter
        self._events: List[RecordedEvent] = []
        self._lock = threading.Lock()
        self._bus = None
        self._callback: Optional[Callable] = None

    def __enter__(self) -> 'EventRecorder':
        try:
            from core.platform.registry import get_registry
            reg = get_registry()
            self._bus = reg.get('events') if reg.has('events') else None
        except Exception:
            self._bus = None
        if self._bus is None:
            return self  # no-op recorder; assertions will see empty list

        def _cb(topic: str, data: Any) -> None:
            with self._lock:
                self._events.append(RecordedEvent(topic, data, time.time()))

        self._callback = _cb
        self._bus.on(self._filter, _cb)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._bus is not None and self._callback is not None:
            try:
                self._bus.off(self._filter, self._callback)
            except Exception:
                pass

    @property
    def events(self) -> List[RecordedEvent]:
        with self._lock:
            return list(self._events)

    def by_topic(self, topic: str) -> List[RecordedEvent]:
        return [e for e in self.events if e.topic == topic]

    def assert_emitted(
        self,
        topic: str,
        *,
        matching: Optional[Callable[[Any], bool]] = None,
        count: Optional[int] = None,
    ) -> List[RecordedEvent]:
        """Assert that at least one event matching `topic` (and
        optionally `matching(data)` and `count`) was emitted."""
        matches = self.by_topic(topic)
        if matching is not None:
            matches = [e for e in matches if matching(e.data)]
        if count is None:
            assert matches, (
                f'expected at least one {topic!r} event; '
                f'saw {[e.topic for e in self.events]}'
            )
        else:
            assert len(matches) == count, (
                f'expected exactly {count} {topic!r} events; '
                f'got {len(matches)}'
            )
        return matches

    def assert_sequence(self, topics: List[str]) -> None:
        """Assert emitted topics contain the given sequence as a
        subsequence (contiguous or not — non-deterministic shapes may
        interleave other events)."""
        it = iter(self.events)
        for want in topics:
            for e in it:
                if e.topic == want:
                    break
            else:
                all_topics = [e.topic for e in self.events]
                raise AssertionError(
                    f'sequence not found — missing {want!r} after '
                    f'prior matches; full topic list: {all_topics}'
                )


# ─── Ledger probe ────────────────────────────────────────────────────

class LedgerProbe:
    """Read-only view over HARTOS ledgers used by journey assertions.

    Every read goes through the EXISTING service helpers — no raw SQL,
    no schema duplication.  When a service isn't importable (CI that
    skips DB), the method returns `None` or an empty list and the
    caller decides whether to skip.
    """

    def __init__(self, db_factory: Optional[Callable] = None):
        """db_factory is a zero-arg callable returning a db session —
        usually `integrations.social.models.get_db`.  Passed in so
        tests can inject a fake or let the harness default."""
        if db_factory is None:
            try:
                from integrations.social.models import get_db
                db_factory = get_db
            except ImportError:
                db_factory = None
        self._db_factory = db_factory

    def _with_db(self, fn: Callable[[Any], Any]) -> Any:
        if self._db_factory is None:
            return None
        db = self._db_factory()
        try:
            return fn(db)
        finally:
            try:
                db.close()
            except Exception:
                pass

    def resonance_transactions(
        self,
        user_id: str,
        source_type_pattern: Optional[str] = None,
        since_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Return ResonanceTransaction rows matching filters."""
        def _q(db: Any) -> List[Dict[str, Any]]:
            try:
                from integrations.social.models import ResonanceTransaction
            except ImportError:
                return []
            q = db.query(ResonanceTransaction).filter(
                ResonanceTransaction.user_id == str(user_id),
            )
            rows = q.all()
            out: List[Dict[str, Any]] = []
            for r in rows:
                st = getattr(r, 'source_type', '') or ''
                if source_type_pattern and source_type_pattern not in st:
                    continue
                created_at = getattr(r, 'created_at', None)
                created_ts = (
                    created_at.timestamp()
                    if created_at and hasattr(created_at, 'timestamp')
                    else 0.0
                )
                if since_ts is not None and created_ts < since_ts:
                    continue
                out.append({
                    'amount': getattr(r, 'amount', 0) or 0,
                    'source_type': st,
                    'source_id': getattr(r, 'source_id', None),
                    'created_ts': created_ts,
                })
            return out
        return self._with_db(_q) or []

    def assert_spark_awarded(
        self,
        user_id: str,
        source_type_pattern: str,
        *,
        min_amount: int = 1,
        since_ts: Optional[float] = None,
    ) -> int:
        rows = self.resonance_transactions(
            user_id, source_type_pattern, since_ts=since_ts,
        )
        total = sum(int(r['amount']) for r in rows)
        assert total >= min_amount, (
            f'expected >= {min_amount} Spark on {source_type_pattern!r} '
            f'for user={user_id}; got total={total} rows={rows}'
        )
        return total

    def consent_granted(
        self,
        user_id: str,
        consent_type: str,
    ) -> Optional[bool]:
        """Read ConsentService.check_consent without duplicating its
        query.  Returns None when consent_service is unavailable."""
        def _q(db: Any) -> Optional[bool]:
            try:
                from integrations.social.consent_service import ConsentService
            except ImportError:
                return None
            try:
                return bool(ConsentService.check_consent(
                    db, user_id, consent_type,
                ))
            except Exception:
                return None
        return self._with_db(_q)


# ─── LLM judge ───────────────────────────────────────────────────────

@dataclass
class JudgeVerdict:
    passed: bool
    score: float
    reason: str


class LLMJudge:
    """Non-deterministic-output assertion helper.

    Two backends:
      - HEURISTIC (default in CI, HEVOLVE_TEST_LLM_JUDGE=0 or unset) —
        simple pattern+length rules that are stable across runs
      - LLM (HEVOLVE_TEST_LLM_JUDGE=1) — calls the real local LLM via
        the world_model_bridge; used locally when you need a semantic
        verdict the heuristic can't express

    The heuristic is the contract; the LLM backend is opt-in.  This
    keeps CI deterministic + fast while letting developers run a
    stronger check locally.
    """

    def __init__(self):
        self._llm_enabled = (
            os.environ.get('HEVOLVE_TEST_LLM_JUDGE', '0').lower() in ('1', 'true', 'yes')
        )

    def judge(
        self,
        output: str,
        *,
        must_contain: Iterable[str] = (),
        must_not_contain: Iterable[str] = (),
        min_len: int = 1,
        max_len: int = 20000,
        rubric: str = '',
    ) -> JudgeVerdict:
        text = str(output or '')
        n = len(text.strip())
        if n < min_len:
            return JudgeVerdict(False, 0.0,
                                f'output too short ({n} < {min_len})')
        if n > max_len:
            return JudgeVerdict(False, 0.0,
                                f'output too long ({n} > {max_len})')
        lower = text.lower()
        for needle in must_contain:
            if needle.lower() not in lower:
                return JudgeVerdict(False, 0.0,
                                    f'missing required phrase: {needle!r}')
        for banned in must_not_contain:
            if banned.lower() in lower:
                return JudgeVerdict(False, 0.0,
                                    f'contained banned phrase: {banned!r}')
        heuristic_score = min(1.0, n / max(min_len * 4, 80))
        if not self._llm_enabled or not rubric:
            return JudgeVerdict(True, heuristic_score, 'heuristic ok')
        # LLM backend — called only when explicitly enabled
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            bridge = get_world_model_bridge()
            provider = getattr(bridge, '_provider', None)
            if provider is None:
                return JudgeVerdict(True, heuristic_score,
                                    'llm unavailable; heuristic pass')
            messages = [
                {'role': 'system', 'content':
                    'You are a strict rubric evaluator.  Score 0.0-1.0.  '
                    'Respond with one line: "SCORE=<float>  REASON=<short>".'},
                {'role': 'user', 'content':
                    f'Rubric: {rubric}\n---\nOutput:\n{text}\n---\nYour verdict:'},
            ]
            resp = provider.create_chat_completion(
                messages=messages, model='hevolve-judge',
                temperature=0, max_tokens=80,
            )
            reply = ''
            try:
                reply = resp['choices'][0]['message']['content']
            except Exception:
                pass
            # Parse "SCORE=0.73  REASON=..."
            score = heuristic_score
            try:
                for part in reply.split():
                    if part.startswith('SCORE='):
                        score = float(part.split('=', 1)[1].rstrip(','))
                        break
            except Exception:
                pass
            return JudgeVerdict(score >= 0.5, score, reply.strip() or 'llm verdict')
        except Exception as exc:
            return JudgeVerdict(True, heuristic_score,
                                f'llm judge failed, heuristic pass: {exc}')


# ─── NFT timer ───────────────────────────────────────────────────────

class NFTTimer:
    """Measure p50/p99 across a repeated block.  Usage:

        with NFTTimer() as t:
            for _ in range(20):
                t.sample(do_one_turn)
        t.assert_budget(p50_ms=1500, p99_ms=4500)
    """

    def __init__(self):
        self._samples_ms: List[float] = []

    def __enter__(self) -> 'NFTTimer':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def sample(self, fn: Callable[[], Any]) -> Any:
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            self._samples_ms.append((time.perf_counter() - t0) * 1000.0)

    def record_ms(self, ms: float) -> None:
        self._samples_ms.append(float(ms))

    @property
    def count(self) -> int:
        return len(self._samples_ms)

    def percentile(self, p: float) -> float:
        if not self._samples_ms:
            return 0.0
        sorted_samples = sorted(self._samples_ms)
        k = max(0, min(len(sorted_samples) - 1,
                       int(round((p / 100.0) * (len(sorted_samples) - 1)))))
        return sorted_samples[k]

    def p50(self) -> float:
        return statistics.median(self._samples_ms) if self._samples_ms else 0.0

    def p99(self) -> float:
        return self.percentile(99)

    def assert_budget(
        self, *, p50_ms: Optional[float] = None, p99_ms: Optional[float] = None,
    ) -> None:
        if not self._samples_ms:
            raise AssertionError('NFTTimer: no samples collected')
        if p50_ms is not None:
            got = self.p50()
            assert got <= p50_ms, (
                f'p50 budget exceeded: {got:.1f}ms > {p50_ms}ms '
                f'(n={len(self._samples_ms)})'
            )
        if p99_ms is not None:
            got = self.p99()
            assert got <= p99_ms, (
                f'p99 budget exceeded: {got:.1f}ms > {p99_ms}ms '
                f'(n={len(self._samples_ms)})'
            )


# ─── Harness ─────────────────────────────────────────────────────────

@dataclass
class HarnessRun:
    """Record of a single agent run for downstream assertions."""
    prompt_id: str
    user_id: str
    turns: List[Dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)


class AgenticHarness:
    """Thin orchestrator that wires EventRecorder + LedgerProbe + NFTTimer
    + LLMJudge around a single test.  Use as a context manager:

        with AgenticHarness() as h:
            run = h.new_run(user_id='u1', prompt_id='p1')
            # drive the agent ...
            h.events.assert_emitted('action_state.changed')
            h.ledger.assert_spark_awarded('u1', 'contest:')
            h.nft.assert_budget(p50_ms=1500)
    """

    def __init__(self):
        self.events = EventRecorder('*')
        self.ledger = LedgerProbe()
        self.judge = LLMJudge()
        self.nft = NFTTimer()
        self.runs: List[HarnessRun] = []

    def __enter__(self) -> 'AgenticHarness':
        self.events.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.events.__exit__(exc_type, exc, tb)

    def new_run(self, *, user_id: str, prompt_id: str) -> HarnessRun:
        run = HarnessRun(prompt_id=prompt_id, user_id=user_id)
        self.runs.append(run)
        return run

    def record_turn(
        self, run: HarnessRun, role: str, content: str, **meta: Any,
    ) -> None:
        run.turns.append({
            'role': role, 'content': content, 'at': time.time(), **meta,
        })


# ─── Convenience — pytest-friendly fixture ─────────────────────────

@contextlib.contextmanager
def harness():
    """Usage in a test:  `with harness() as h: ...`"""
    with AgenticHarness() as h:
        yield h
