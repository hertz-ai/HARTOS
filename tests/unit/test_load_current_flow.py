"""Regression tests for ``core.cache_loaders.load_current_flow``.

Phase 3 of TASK_LEDGER_PERSISTENCE_PLAN.md. The loader closes the bug
where Nunba restart dropped a mid-execution agent back to flow 0 because
``recipe_for_persona`` had no restoration callback. Behaviour:

  * Active session at flow N → loader returns N (the flow with any
    non-terminal task).
  * All sessions for this (user, prompt) terminal → returns the highest
    flow seen (final state).
  * No prior ledger → returns 0 (matches ``initialise_current_flow_to_zero``).
  * Parse failure (malformed user_prompt) → returns None so TTLCache
    falls through to the default-zero path explicitly.
  * User isolation: an unfinished session for user A must NOT influence
    user B's resolved flow.
"""

import json
from unittest.mock import patch
import pytest

# Import under test; the loader is deliberately imported lazily inside
# the test functions so monkeypatch.chdir + tmp_path can be applied
# before the SmartLedger.list_grouped helper resolves AGENT_DATA_DIR.


def _write_ledger(tmp_path, agent_id, session_id, flow_action_status):
    """Helper: write a ledger JSON file with the given flow/action/status
    tuples.  ``flow_action_status`` is a list of (flow_id, action_id,
    status_str) — one Task entry per tuple, stamped with recipe_* fields
    so the grouping helper can read them without legacy fallback."""
    tasks = {}
    for flow_id, action_id, status in flow_action_status:
        tid = f"action_{action_id}"
        tasks[tid] = {
            "task_id": tid,
            "description": f"flow {flow_id} action {action_id}",
            "execution_mode": "parallel",
            "task_type": "pre_assigned",
            "status": status,
            "priority": 50,
            "recipe_prompt_id": str(agent_id),
            "recipe_flow_id": flow_id,
            "recipe_action_id": action_id,
        }
    fname = f"ledger_{agent_id}_{session_id}.json"
    (tmp_path / fname).write_text(
        json.dumps({"agent_id": str(agent_id), "session_id": session_id,
                    "tasks": tasks})
    )


def _scoped_helper_classmethod(tmp_path):
    """Build a SmartLedger.list_grouped_by_recipe_hierarchy classmethod
    that targets tmp_path instead of the AGENT_DATA_DIR default."""
    from agent_ledger.core import SmartLedger
    original = SmartLedger.list_grouped_by_recipe_hierarchy

    def _impl(cls=None, ledger_dir=None):
        return original.__func__(SmartLedger, str(tmp_path))

    return classmethod(_impl)


def test_active_session_returns_active_flow(tmp_path, monkeypatch):
    """Mid-execution session at flow 2 (action_1 pending, action_2
    completed, action_3 in_progress, action_4 pending) → loader picks
    the HIGHEST flow with any non-terminal task = flow 2."""
    _write_ledger(tmp_path, agent_id=42, session_id="10202_42_1716000000000",
                  flow_action_status=[
                      (0, 1, "completed"),
                      (1, 1, "completed"),
                      (2, 1, "in_progress"),
                      (2, 2, "pending"),
                  ])
    from agent_ledger.core import SmartLedger
    monkeypatch.setattr(
        SmartLedger, 'list_grouped_by_recipe_hierarchy',
        _scoped_helper_classmethod(tmp_path),
    )
    from core.cache_loaders import load_current_flow
    assert load_current_flow("10202_42") == 2


def test_all_terminal_returns_highest_flow(tmp_path, monkeypatch):
    """Session whose every task is terminal → returns the highest
    flow_id seen (the session's final resting place — keeps follow-up
    logic monotonic, never resets to 0)."""
    _write_ledger(tmp_path, agent_id=42, session_id="10202_42_1716000000000",
                  flow_action_status=[
                      (0, 1, "completed"),
                      (1, 1, "completed"),
                      (2, 1, "completed"),
                  ])
    from agent_ledger.core import SmartLedger
    monkeypatch.setattr(
        SmartLedger, 'list_grouped_by_recipe_hierarchy',
        _scoped_helper_classmethod(tmp_path),
    )
    from core.cache_loaders import load_current_flow
    assert load_current_flow("10202_42") == 2


def test_no_prior_ledger_returns_zero(tmp_path, monkeypatch):
    """Fresh (user, prompt) with no ledger on disk → returns 0 (matches
    pre-Phase-3 ``initialise_current_flow_to_zero`` semantics)."""
    from agent_ledger.core import SmartLedger
    monkeypatch.setattr(
        SmartLedger, 'list_grouped_by_recipe_hierarchy',
        _scoped_helper_classmethod(tmp_path),
    )
    from core.cache_loaders import load_current_flow
    assert load_current_flow("99999_77") == 0


def test_malformed_user_prompt_returns_none(tmp_path, monkeypatch):
    """A user_prompt that doesn't split on '_' into two parts is
    unrecoverable — return None so TTLCache treats it as a miss and the
    caller falls through to the explicit init path."""
    from core.cache_loaders import load_current_flow
    assert load_current_flow("malformed") is None


def test_user_isolation(tmp_path, monkeypatch):
    """An unfinished session for user 11111 must NOT influence the
    resolved flow for user 22222 — same prompt, different users.  The
    user_prefix filter inside the loader enforces this."""
    # Alice (11111) is at flow 3.
    _write_ledger(tmp_path, agent_id=42, session_id="11111_42_1716000000000",
                  flow_action_status=[
                      (3, 1, "in_progress"),
                  ])
    from agent_ledger.core import SmartLedger
    monkeypatch.setattr(
        SmartLedger, 'list_grouped_by_recipe_hierarchy',
        _scoped_helper_classmethod(tmp_path),
    )
    from core.cache_loaders import load_current_flow
    # Bob (22222) has no ledger → resolves to 0 even though Alice's
    # ledger has non-terminal tasks for the same prompt.
    assert load_current_flow("22222_42") == 0
    # Alice resolves to flow 3.
    assert load_current_flow("11111_42") == 3


def test_newest_session_wins_when_multiple_resumable(tmp_path, monkeypatch):
    """When the same (user, prompt) somehow has multiple resumable
    sessions on disk (e.g. legacy deterministic name + new timestamped
    name both with in-flight tasks), the newest session_id wins.
    Lexicographic descending sort handles both formats — ts_ms suffix
    collates after the bare deterministic form for the same prefix."""
    _write_ledger(tmp_path, agent_id=42, session_id="10202_42",
                  flow_action_status=[(1, 1, "in_progress")])
    _write_ledger(tmp_path, agent_id=42, session_id="10202_42_1716000000000",
                  flow_action_status=[(4, 1, "pending")])
    from agent_ledger.core import SmartLedger
    monkeypatch.setattr(
        SmartLedger, 'list_grouped_by_recipe_hierarchy',
        _scoped_helper_classmethod(tmp_path),
    )
    from core.cache_loaders import load_current_flow
    # The timestamped session (lexically larger) wins → flow 4.
    assert load_current_flow("10202_42") == 4


def test_loader_silently_returns_none_on_helper_exception(tmp_path, monkeypatch):
    """If ``SmartLedger.list_grouped_by_recipe_hierarchy`` raises (e.g.
    disk error, corrupted JSON file unparseable), the loader must
    return None — the cache treats it as a miss and the caller falls
    through to ``initialise_current_flow_to_zero``.  Never let a cache-
    loader exception escape and crash the hot path."""
    from agent_ledger.core import SmartLedger
    def _boom(cls=None, ledger_dir=None):
        raise RuntimeError("simulated disk failure")
    monkeypatch.setattr(
        SmartLedger, 'list_grouped_by_recipe_hierarchy',
        classmethod(_boom),
    )
    from core.cache_loaders import load_current_flow
    assert load_current_flow("10202_42") is None
