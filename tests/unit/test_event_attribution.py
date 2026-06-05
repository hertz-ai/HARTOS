"""#58 — owner_user_id resolves the owning user for agent/goal/memory events.

Publishers (agent.action.completed, action_state.changed, memory.item_added) now
stamp the owner so the P3a SSE guard routes them per-user instead of dropping
~5,200/day.  None when unresolvable → the guard keeps refusing (no regression,
no cross-user leak).  inference.completed is deliberately NOT wired — it carries
no user (model/latency only).
"""
from __future__ import annotations

import contextlib
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.event_attribution import owner_user_id  # noqa: E402


def test_resolve_from_user_prompt():
    assert owner_user_id(user_prompt='u1_123') == 'u1'
    # user_id may be a UUID (hyphens, no underscore) — split on the FIRST underscore
    assert owner_user_id(user_prompt='cda61a4f-91a6-446d_999') == 'cda61a4f-91a6-446d'
    assert owner_user_id(user_prompt='noUnderscore') is None
    assert owner_user_id(user_prompt='') is None
    assert owner_user_id(user_prompt=None) is None


def test_resolve_from_metadata():
    assert owner_user_id(metadata={'user_id': 'u2'}) == 'u2'
    assert owner_user_id(metadata={'user_id': 42}) == '42'  # coerced to str
    assert owner_user_id(metadata={}) is None
    assert owner_user_id(metadata=None) is None


def test_unresolvable_returns_none_no_regression():
    # No context → None → the SSE guard keeps refusing the event, exactly as
    # before the fix (this is the no-leak invariant).
    assert owner_user_id() is None


def test_precedence_prompt_over_metadata_over_goal():
    # user_prompt wins even if metadata also present (cheapest, most specific).
    assert owner_user_id(user_prompt='ux_1', metadata={'user_id': 'um'}) == 'ux'
    assert owner_user_id(metadata={'user_id': 'um'}, goal_id='g1') == 'um'


def test_resolve_from_goal_owner(monkeypatch):
    import integrations.social.models as M

    class _Goal:
        owner_id = 'owner9'
        created_by = None
        user_id = None

    class _Q:
        def filter(self, *a, **k):
            return self
        def first(self):
            return _Goal()

    class _DB:
        def query(self, *a, **k):
            return _Q()

    @contextlib.contextmanager
    def _fake_session():
        yield _DB()

    monkeypatch.setattr(M, 'db_session', _fake_session, raising=False)
    monkeypatch.setattr(M, 'AgentGoal', type('AgentGoal', (), {'id': 0}), raising=False)
    assert owner_user_id(goal_id='g1') == 'owner9'
