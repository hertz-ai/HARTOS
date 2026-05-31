"""Behavioural test for the error_advice cross-restart self_heal dedup
(2026-05-31, evidence-driven).

LIVE EVIDENCE: 50 active self_heal goals, only 6 distinct titles — 24×
"Self-heal: tts.probe (RuntimeError)", 20× "Self-heal: tts.install...",
accumulating since 2026-03-29, all created_by='error_advice'.  Root cause:
error_advice._try_agent_remediation gated ONLY on _should_emit's IN-MEMORY
throttle, which resets every process restart.  The goals persist in the DB, so
each Nunba relaunch re-created the same failure's goal → flood.

FIX: a DB-level dedup (mirroring self_healing_dispatcher._is_already_being_fixed)
— skip creation if an active self_heal goal already targets the same fingerprint.
These tests pin: no DB dup created when one exists; created when none exists.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.error_advice as ea  # noqa: E402


class _DBCtx:
    """db_session() context manager stub returning a query-able fake db."""
    def __init__(self, existing_goals):
        self._db = MagicMock()
        self._db.query.return_value.filter.return_value.all.return_value = existing_goals

    def __enter__(self):
        return self._db

    def __exit__(self, *a):
        return False


def _run(existing_goals):
    """Drive _try_agent_remediation with the throttle forced open + the DB
    returning `existing_goals`, and capture whether create_goal was called."""
    fake_gm = MagicMock()
    # Fake AgentGoal needs class-level status/goal_type so the filter-arg
    # expressions (AgentGoal.status == 'active') evaluate without AttributeError
    # — real SQLAlchemy Columns support ==; the query result itself is mocked.
    fake_models = SimpleNamespace(
        db_session=lambda: _DBCtx(existing_goals),
        AgentGoal=type('AgentGoal', (), {'status': MagicMock(), 'goal_type': MagicMock()}),
    )
    fake_gm_mod = SimpleNamespace(GoalManager=fake_gm)

    with patch.dict(sys.modules, {
        'integrations.agent_engine.goal_manager': fake_gm_mod,
        'integrations.social.models': fake_models,
    }):
        # Force the in-memory throttle OPEN so we exercise the DB path.
        with patch.object(ea, '_should_emit', return_value=True), \
             patch.object(ea, '_fingerprint', return_value='FP123'):
            try:
                exc = RuntimeError('tts probe failed')
                ea._try_agent_remediation('tts.probe', exc, {'subsystem': 'tts'}, 'high')
            except Exception:
                pass
    return fake_gm.create_goal


def test_skips_create_when_active_goal_has_same_fingerprint():
    existing = [SimpleNamespace(config_json={'fingerprint': 'FP123'})]
    create = _run(existing)
    assert not create.called, (
        "must NOT create a duplicate self_heal goal when an active goal "
        "already targets this fingerprint (cross-restart dedup)")


def test_creates_when_no_matching_fingerprint():
    existing = [SimpleNamespace(config_json={'fingerprint': 'OTHER'})]
    create = _run(existing)
    assert create.called, "must create when no active goal targets this fingerprint"


def test_creates_when_no_active_goals():
    create = _run([])
    assert create.called, "must create the first goal for a fresh fingerprint"


def test_dedup_survives_null_config_json():
    """A legacy goal with config_json=None must not crash the dedup scan."""
    existing = [SimpleNamespace(config_json=None),
                SimpleNamespace(config_json={'fingerprint': 'FP123'})]
    create = _run(existing)
    assert not create.called, "null config_json must be skipped, dup still caught"
