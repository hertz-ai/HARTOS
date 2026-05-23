"""Unit tests for the zombie task reaper.

Tests cover the pure parts (no `agent_ledger` package needed):
- `_max_age` env handling with floor protection
- `_coerce_dt` tolerance for str / datetime / int / None
- `_task_age` selecting the right attribute, returning None for unknowns
- `reap_once` dry-run path with mocked _iter_ledgers + TaskStatus + ImmutableAuditLog
- `reap_once` write path verifies task.fail() called + audit.log_event called

`register_with_scheduler` is covered with a real APScheduler instance
that is started+stopped inside the test (no leftover thread).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

# Import target.  Skip the whole file if zombie_reaper can't import (e.g.
# in environments missing apscheduler / agent_ledger / immutable_audit_log).
zr = pytest.importorskip(
    'integrations.agent_engine.zombie_reaper',
    reason='zombie_reaper module not importable in this env',
)


# ─── _max_age ──────────────────────────────────────────────────────────

def test_max_age_default_is_2h(monkeypatch):
    monkeypatch.delenv('HEVOLVE_ZOMBIE_TASK_MAX_AGE_HOURS', raising=False)
    assert zr._max_age() == timedelta(hours=2)


def test_max_age_honors_env(monkeypatch):
    monkeypatch.setenv('HEVOLVE_ZOMBIE_TASK_MAX_AGE_HOURS', '4.5')
    assert zr._max_age() == timedelta(hours=4.5)


def test_max_age_floor_blocks_runaway_reap(monkeypatch):
    """A misconfigured env=0 must NOT reap every in-progress task."""
    monkeypatch.setenv('HEVOLVE_ZOMBIE_TASK_MAX_AGE_HOURS', '0')
    # Floor is 0.1h = 6min.
    assert zr._max_age() == timedelta(hours=0.1)


def test_max_age_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv('HEVOLVE_ZOMBIE_TASK_MAX_AGE_HOURS', 'not-a-number')
    assert zr._max_age() == timedelta(hours=2)


# ─── _coerce_dt ────────────────────────────────────────────────────────

def test_coerce_dt_passes_through_datetime():
    dt = datetime(2026, 5, 17, tzinfo=timezone.utc)
    assert zr._coerce_dt(dt) == dt


def test_coerce_dt_naive_datetime_gets_utc():
    naive = datetime(2026, 5, 17, 12, 0, 0)
    out = zr._coerce_dt(naive)
    assert out is not None and out.tzinfo == timezone.utc


def test_coerce_dt_iso_string_with_z():
    out = zr._coerce_dt('2026-05-17T12:00:00Z')
    assert out is not None and out.year == 2026


def test_coerce_dt_epoch_int():
    out = zr._coerce_dt(1779000000)
    assert out is not None and isinstance(out, datetime)


def test_coerce_dt_unrecognized_returns_none():
    assert zr._coerce_dt(None) is None
    assert zr._coerce_dt('not-a-date') is None
    assert zr._coerce_dt({'wrong': 'shape'}) is None


# ─── _task_age ─────────────────────────────────────────────────────────

def _fresh_task(**ts_attrs):
    """SimpleNamespace masquerading as a SmartLedger task."""
    return SimpleNamespace(**ts_attrs)


def test_task_age_prefers_heartbeat_at():
    long_ago = datetime.now(timezone.utc) - timedelta(hours=10)
    recent = datetime.now(timezone.utc) - timedelta(minutes=1)
    task = _fresh_task(heartbeat_at=recent, updated_at=long_ago, started_at=long_ago)
    age = zr._task_age(task)
    # heartbeat_at was 1 min ago -> age < 5min
    assert age is not None
    assert age < timedelta(minutes=5)


def test_task_age_falls_through_to_updated_at():
    moment = datetime.now(timezone.utc) - timedelta(hours=3)
    task = _fresh_task(updated_at=moment)
    age = zr._task_age(task)
    assert age is not None
    assert age >= timedelta(hours=2, minutes=55)


def test_task_age_returns_none_when_no_timestamps():
    task = _fresh_task(id='x')
    assert zr._task_age(task) is None


# ─── reap_once (mocked dependencies) ───────────────────────────────────

class _FakeTaskStatus:
    """Stand-in for agent_ledger.TaskStatus — only IN_PROGRESS + FAILED are used."""
    IN_PROGRESS = 'in_progress'
    FAILED = 'failed'


class _FakeLedger:
    def __init__(self, tasks):
        self.tasks = {t.id: t for t in tasks}
        self.saved = False
    def save(self):
        self.saved = True


def _make_zombie(task_id='z1', age_hours=3, status=_FakeTaskStatus.IN_PROGRESS):
    return SimpleNamespace(
        id=task_id,
        status=status,
        heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        fail=mock.Mock(),
        blocked_reason=None,
    )


def _patch_imports(monkeypatch, ledgers, audit_calls):
    """Patch the deferred imports inside reap_once.

    Both ``agent_ledger`` and ``security.immutable_audit_log`` are
    injected via ``sys.modules`` BEFORE the reaper's late import runs,
    so neither module's real source needs to be importable in this env
    (the production tree's ``security/__init__.py`` pulls in
    ``cryptography.fernet`` which isn't always installed for unit tests).
    """
    # 1. Fake agent_ledger (so reaper's `from agent_ledger import TaskStatus` succeeds).
    fake_agent_ledger = SimpleNamespace(TaskStatus=_FakeTaskStatus)
    monkeypatch.setitem(sys.modules, 'agent_ledger', fake_agent_ledger)

    # 2. Fake security.immutable_audit_log (with its parent `security`
    #    package shim so the dotted import resolves).
    fake_audit = SimpleNamespace(log_event=mock.Mock(
        side_effect=lambda **kw: audit_calls.append((tuple(), kw))
    ))
    fake_security_pkg = SimpleNamespace(__path__=[])
    fake_audit_module = SimpleNamespace(get_audit_log=lambda: fake_audit)
    monkeypatch.setitem(sys.modules, 'security', fake_security_pkg)
    monkeypatch.setitem(sys.modules, 'security.immutable_audit_log', fake_audit_module)

    # 3. Patch the iterator the reaper imports from .api (must use raising=False
    #    in case the api module also fails to import in this env).
    try:
        monkeypatch.setattr(
            'integrations.agent_engine.api._iter_ledgers',
            lambda agent_filter=None: iter(ledgers),
            raising=False,
        )
    except (ImportError, ModuleNotFoundError):
        # Inject a fake api module too if even that import fails.
        fake_api = SimpleNamespace(_iter_ledgers=lambda agent_filter=None: iter(ledgers))
        monkeypatch.setitem(sys.modules, 'integrations.agent_engine.api', fake_api)
    return fake_audit


def test_reap_once_dry_run_does_not_mutate(monkeypatch):
    zombie = _make_zombie(age_hours=5)
    ledger = _FakeLedger([zombie])
    audit_calls = []
    _patch_imports(monkeypatch, [('agent-A', 'sess-1', ledger)], audit_calls)

    out = zr.reap_once(dry_run=True)

    assert out['examined'] == 1
    assert out['reaped'] == 1
    assert out['dry_run'] is True
    # Dry-run: no state change, no save, no audit call.
    zombie.fail.assert_not_called()
    assert ledger.saved is False
    assert audit_calls == []


def test_reap_once_reaps_old_in_progress(monkeypatch):
    zombie = _make_zombie(age_hours=5)
    ledger = _FakeLedger([zombie])
    audit_calls = []
    _patch_imports(monkeypatch, [('agent-A', 'sess-1', ledger)], audit_calls)

    out = zr.reap_once(dry_run=False)

    assert out['reaped'] == 1
    zombie.fail.assert_called_once()
    assert ledger.saved is True
    assert len(audit_calls) == 1
    audit_kwargs = audit_calls[0][1]
    assert audit_kwargs.get('event_type') == 'zombie_reaped'
    assert audit_kwargs.get('target_id') == 'z1'


def test_reap_once_leaves_young_tasks_alone(monkeypatch):
    young = _make_zombie(age_hours=0.5)  # 30 min < 2h threshold
    ledger = _FakeLedger([young])
    audit_calls = []
    _patch_imports(monkeypatch, [('agent-A', 'sess-1', ledger)], audit_calls)

    out = zr.reap_once(dry_run=False)

    assert out['examined'] == 1
    assert out['reaped'] == 0
    young.fail.assert_not_called()
    assert ledger.saved is False


def test_reap_once_ignores_non_in_progress(monkeypatch):
    completed = _make_zombie(age_hours=5, status='completed')
    ledger = _FakeLedger([completed])
    audit_calls = []
    _patch_imports(monkeypatch, [('agent-A', 'sess-1', ledger)], audit_calls)

    out = zr.reap_once(dry_run=False)

    assert out['reaped'] == 0
    completed.fail.assert_not_called()


class _RealSignatureTask:
    """Stand-in for SmartLedger Task that enforces the REAL fail()
    signature: ``fail(self, error: str, reason: str = "Task failed")``.

    This class exists because the bare ``mock.Mock()`` used elsewhere in
    this file accepts any arguments and therefore hid the 2026-05-23
    production bug where zombie_reaper called ``task.fail(reason=...)``
    without the required ``error`` positional.  The TypeError raised on
    every zombie task crashed the reaper, leaving all 10 daemon worker
    slots locked and silently blocking goal dispatch for hours.

    DO NOT replace this with ``mock.Mock(spec=...)`` of the real Task —
    that would couple this regression test to the full agent_ledger
    import chain (cryptography, etc.) which isn't installed in CI.
    """

    def __init__(self, task_id='z1', age_hours=5):
        self.id = task_id
        self.status = _FakeTaskStatus.IN_PROGRESS
        self.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        self.blocked_reason = None
        self.fail_calls = []  # records (error, reason) actually passed

    def fail(self, error: str, reason: str = "Task failed") -> bool:
        # Mirror the real signature exactly — error is positional/keyword
        # but REQUIRED.  Calling fail(reason=...) without error must
        # raise TypeError here as it does in production.
        self.fail_calls.append((error, reason))
        self.status = _FakeTaskStatus.FAILED
        return True


def test_reap_once_uses_real_fail_signature(monkeypatch):
    """Regression for the 2026-05-23 production bug: zombie_reaper called
    ``task.fail(reason=...)`` without the required ``error`` positional.
    The reaper crashed per-task; 0 zombies were reaped; daemon worker
    slots stayed locked; 8 dispatched goals never claimed.

    This test uses a fake Task that enforces the real fail() signature
    so a future drift of the call site is caught immediately."""
    zombie = _RealSignatureTask(task_id='z-real-sig', age_hours=5)
    ledger = _FakeLedger([zombie])
    audit_calls = []
    _patch_imports(monkeypatch, [('agent-A', 'sess-1', ledger)], audit_calls)

    out = zr.reap_once(dry_run=False)

    # 1 task examined, 1 reaped, 0 errors — proves the call didn't crash
    assert out['examined'] == 1, f"examined={out['examined']}, expected 1"
    assert out['reaped'] == 1, f"reaped={out['reaped']}, expected 1 (was 0 before fix)"
    assert out['errors'] == 0, (
        f"errors={out['errors']}, expected 0.  Per-task crash means the "
        f"production bug regressed: zombie_reaper called task.fail() with "
        f"the wrong signature."
    )
    # And the error message recorded on the task is the zombie reason —
    # not the default "Task failed".  Audit trail must explain WHY.
    assert len(zombie.fail_calls) == 1
    err, _reason = zombie.fail_calls[0]
    assert 'zombie' in err.lower() and 'min' in err.lower(), (
        f"error_message={err!r} — must contain 'zombie' + age suffix so "
        f"operators can grep for reaper-caused failures vs real failures."
    )


def test_reap_once_one_failing_task_does_not_break_loop(monkeypatch):
    """Per-task exception is caught and counted, not raised."""
    bad = SimpleNamespace(
        id='bad', status=_FakeTaskStatus.IN_PROGRESS,
        heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=5),
        fail=mock.Mock(side_effect=RuntimeError('boom')),
    )
    good = _make_zombie(task_id='good', age_hours=5)
    ledger = _FakeLedger([bad, good])
    audit_calls = []
    _patch_imports(monkeypatch, [('agent-A', 'sess-1', ledger)], audit_calls)

    out = zr.reap_once(dry_run=False)

    assert out['examined'] == 2
    assert out['errors'] >= 1
    # The other task still got processed.
    good.fail.assert_called_once()


# ─── register_with_scheduler ───────────────────────────────────────────

def test_register_with_scheduler_idempotent():
    """Re-registering with the same job_id replaces, doesn't stack."""
    pytest.importorskip('apscheduler')
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler()
    sched.start(paused=True)
    try:
        assert zr.register_with_scheduler(sched, interval_minutes=15) is True
        first_job = sched.get_job(zr._SCHEDULER_JOB_ID)
        assert first_job is not None
        # Re-register with a different interval — must replace, not stack.
        assert zr.register_with_scheduler(sched, interval_minutes=30) is True
        jobs = [j for j in sched.get_jobs() if j.id == zr._SCHEDULER_JOB_ID]
        assert len(jobs) == 1, 'expected exactly one job after re-register'
    finally:
        sched.shutdown(wait=False)
