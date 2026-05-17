"""Periodic reaper for in-progress tasks that never finished.

PROBLEM
=======
``/api/agent-engine/ledger/tasks?status=in_progress`` returns dozens of
rows that have not advanced in days.  They are zombies: a worker
claimed the task, then died or swallowed an exception, and the ledger
was never marked terminal.  They poison three things:

  1. The Live Agents dashboard counts them as "running", inflating the
     stalled-agent count operators see (pending task #179 was filed
     after the count hit 67 in_progress / 2465 pending on 2026-05-17).
  2. Daemon worker slots are reserved for tasks that will never report
     completion, blocking the scheduler from dispatching new work.
  3. Owner-claim locks block legitimate retry of the same action.

REUSES (no parallel paths)
==========================
- ``_iter_ledgers(...)`` from ``integrations/agent_engine/api.py:265``
  (the canonical "walk every SmartLedger on disk" generator the
  /api/agent-engine/ledger/tasks endpoint already uses).
- ``get_audit_log().log_event(...)`` from
  ``security/immutable_audit_log.py:95`` (the canonical hash-chained
  audit log; reuses existing event-type vocabulary plus one new value
  ``zombie_reaped``).
- ``agent_ledger.TaskStatus`` (external pip-installed package; same
  import the API endpoint uses).
- ``task.fail(reason=...)`` if present, else direct status assignment.
  This is the IN_PROGRESS -> FAILED transition that
  ``lifecycle_hooks.py:96`` already maps via STATE_MAP.

NO NEW INFRASTRUCTURE
=====================
- No new daemon process.  Job registered with the existing
  ``BackgroundScheduler`` created in ``create_recipe.py:339``.
- No new table.  Audit rows go through the existing log.
- No new state machine transition.  IN_PROGRESS -> FAILED is already
  legal per STATE_MAP.

THRESHOLD
=========
Default 2h since last state change / heartbeat.  Configurable via
``HEVOLVE_ZOMBIE_TASK_MAX_AGE_HOURS``.  A 2h floor is high enough that
slow but legitimate work (long LLM completion, queued tool retry) is
never wrongly reaped, while still catching the days-old entries this
module exists to clear.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_HOURS = 2.0
_AUDIT_EVENT_TYPE = 'zombie_reaped'
_AUDIT_ACTOR = 'system:zombie_reaper'
_SCHEDULER_JOB_ID = 'hevolve_zombie_reaper'


def _max_age() -> timedelta:
    """Read threshold from env with a hard floor of 6 minutes.

    Floor exists so misconfiguration (e.g. ``HEVOLVE_ZOMBIE_TASK_MAX_
    AGE_HOURS=0``) cannot accidentally reap every in-progress task on
    the next tick.  The floor of 6 minutes is greater than the longest
    legitimate LLM call observed in production (4 min p99 on cold
    Qwen3-VL load).
    """
    try:
        hours = float(os.environ.get(
            'HEVOLVE_ZOMBIE_TASK_MAX_AGE_HOURS',
            _DEFAULT_MAX_AGE_HOURS,
        ))
    except (TypeError, ValueError):
        hours = _DEFAULT_MAX_AGE_HOURS
    return timedelta(hours=max(0.1, hours))


def _coerce_dt(value: Any) -> Optional[datetime]:
    """Tolerant timestamp parser — tasks may carry str, datetime, or epoch.

    Returns ``None`` for unrecognized shapes rather than raising; reaping
    must never crash the scheduler tick on one malformed task.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            # Tolerate trailing Z or +HH:MM.
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    return None


def _task_age(task: Any) -> Optional[timedelta]:
    """Time since the task's last state change / heartbeat.

    Probes a list of attribute names in preference order.  ``heartbeat_
    at`` first because ledger tasks emit one on every state change
    (per ``lifecycle_hooks._auto_sync_to_ledger`` Heartbeat section),
    making it the most accurate liveness signal.  ``updated_at`` /
    ``started_at`` / ``assigned_at`` are fallbacks for older task
    shapes.
    """
    for attr in ('heartbeat_at', 'updated_at', 'started_at', 'assigned_at',
                 'created_at'):
        ts = _coerce_dt(getattr(task, attr, None))
        if ts is not None:
            return datetime.now(timezone.utc) - ts
    return None


def reap_once(dry_run: bool = False) -> Dict[str, Any]:
    """Walk every SmartLedger, fail old IN_PROGRESS tasks.

    Returns a summary dict — safe to call from a scheduler tick or a
    CLI probe.  Never raises; all per-task errors are caught and
    counted in ``errors``.

    ``dry_run=True`` leaves task state untouched but still produces the
    summary, for safe "what would be reaped" inspection from a probe.
    """
    summary: Dict[str, Any] = {
        'examined': 0,
        'reaped': 0,
        'errors': 0,
        'reaped_ids': [],
        'dry_run': dry_run,
        'threshold_minutes': int(_max_age().total_seconds() // 60),
    }

    try:
        from agent_ledger import TaskStatus
        from .api import _iter_ledgers
    except ImportError as e:
        logger.debug("zombie_reaper: dependency unavailable (%s) — skipping tick", e)
        return summary

    audit = None
    try:
        from security.immutable_audit_log import get_audit_log
        audit = get_audit_log()
    except Exception:
        logger.debug("zombie_reaper: audit log unavailable — proceeding without audit")

    threshold = _max_age()

    try:
        ledgers = list(_iter_ledgers())
    except Exception:
        logger.exception("zombie_reaper: _iter_ledgers failed — aborting tick")
        summary['errors'] += 1
        return summary

    for agent_id, session_id, ledger in ledgers:
        try:
            tasks = list(ledger.tasks.values())
        except Exception:
            logger.exception("zombie_reaper: ledger.tasks unavailable for %s/%s",
                             agent_id, session_id)
            summary['errors'] += 1
            continue

        ledger_dirty = False
        for task in tasks:
            summary['examined'] += 1
            try:
                if getattr(task, 'status', None) != TaskStatus.IN_PROGRESS:
                    continue
                age = _task_age(task)
                if age is None or age < threshold:
                    continue

                task_id = getattr(task, 'id', None) or getattr(task, 'task_id', None) or repr(task)
                age_min = int(age.total_seconds() // 60)
                reason = f'zombie_reaped_after_{age_min}min'

                if not dry_run:
                    if hasattr(task, 'fail') and callable(task.fail):
                        task.fail(reason=reason)
                    else:
                        # Fallback: direct status assignment.  Legal
                        # IN_PROGRESS -> FAILED per STATE_MAP.
                        task.status = TaskStatus.FAILED
                        if hasattr(task, 'blocked_reason'):
                            task.blocked_reason = reason
                    ledger_dirty = True

                    if audit is not None:
                        try:
                            audit.log_event(
                                event_type=_AUDIT_EVENT_TYPE,
                                actor_id=_AUDIT_ACTOR,
                                action=f'timeout_to_failed (age={age_min}min)',
                                detail={
                                    'agent_id': agent_id,
                                    'session_id': session_id,
                                    'task_id': task_id,
                                    'age_seconds': int(age.total_seconds()),
                                    'threshold_seconds': int(threshold.total_seconds()),
                                },
                                target_id=task_id,
                            )
                        except Exception:
                            logger.exception("zombie_reaper: audit.log_event failed for %s", task_id)

                summary['reaped'] += 1
                summary['reaped_ids'].append(f'{agent_id}/{task_id}')
                logger.info("zombie_reaper: %s task %s aged %s",
                            'dry-run' if dry_run else 'reaped',
                            task_id, age)
            except Exception:
                logger.exception("zombie_reaper: per-task error in %s/%s",
                                 agent_id, session_id)
                summary['errors'] += 1

        if ledger_dirty and not dry_run:
            try:
                ledger.save()
            except Exception:
                logger.exception("zombie_reaper: ledger.save() failed for %s/%s",
                                 agent_id, session_id)
                summary['errors'] += 1

    logger.info(
        "zombie_reaper tick complete: examined=%d reaped=%d errors=%d "
        "threshold_min=%d dry_run=%s",
        summary['examined'], summary['reaped'], summary['errors'],
        summary['threshold_minutes'], dry_run,
    )
    return summary


def register_with_scheduler(scheduler, interval_minutes: int = 15) -> bool:
    """Register the reaper as a periodic job on the existing scheduler.

    Idempotent — re-registering with the same job_id replaces the prior
    instance via ``replace_existing=True``.  Returns True on success,
    False if the scheduler is unavailable or the registration raised.

    Called once at boot from ``create_recipe.py`` right after
    ``scheduler.start()`` (no separate daemon process).
    """
    try:
        scheduler.add_job(
            reap_once,
            'interval',
            minutes=max(1, int(interval_minutes)),
            id=_SCHEDULER_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("zombie_reaper scheduled every %d minutes (job_id=%s)",
                    interval_minutes, _SCHEDULER_JOB_ID)
        return True
    except Exception:
        logger.exception("zombie_reaper: failed to register with scheduler")
        return False
