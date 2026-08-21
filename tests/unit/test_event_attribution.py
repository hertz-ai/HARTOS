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


# ── A fresh appliance must HEAL once the human signs up ──────────────────────
# 0 users and >1 users are both "no sole user", but they are not the same
# negative. 0 users is the normal state of a just-flashed box for the minutes
# before the human creates an account, while the daemons are already emitting.
# Caching it pinned the answer to None for the life of the process, so signing
# up did NOT restore the agent panels -- they stayed dark until a backend
# restart. Measured on the reflashed Samsung box 2026-08-22: users=1,
# agent_goals=0, and still 666+ "SSE broadcast refused ... has no user_id"
# per boot with a reconnect/retry button on screen.

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


def _install_fake_db(monkeypatch, rows):
    """Point _sole_local_user_id's query at `rows` without a real DB."""
    import core.event_attribution as ea

    class _Q:
        def limit(self, _n):
            return self

        def all(self):
            return list(rows)

    class _DB:
        def query(self, _model):
            return _Q()

    @contextlib.contextmanager
    def _session():
        yield _DB()

    mod = type(sys)('integrations.social.models')
    mod.db_session = _session
    mod.User = object
    monkeypatch.setitem(sys.modules, 'integrations.social.models', mod)
    ea._SOLE_USER_CACHE.clear()
    ea._ZERO_USER_LAST_CHECK[0] = 0.0
    return ea


def test_zero_users_is_rechecked_not_pinned(monkeypatch):
    """The fresh-appliance case: 0 users, then the human signs up."""
    rows = []
    ea = _install_fake_db(monkeypatch, rows)

    assert ea._sole_local_user_id() is None      # nobody has signed up yet

    rows.append(_FakeUser('owner-1'))            # the human creates an account
    ea._ZERO_USER_LAST_CHECK[0] = 0.0            # jump past the re-check throttle

    assert ea._sole_local_user_id() == 'owner-1', \
        "a zero-user answer must not be pinned: signing up has to restore routing"


def test_zero_user_recheck_is_throttled(monkeypatch):
    """It heals, but must not add a DB round trip per emit (~4,882/day)."""
    rows = []
    ea = _install_fake_db(monkeypatch, rows)

    assert ea._sole_local_user_id() is None
    rows.append(_FakeUser('owner-1'))
    # No throttle reset: within _ZERO_USER_RECHECK_S it must NOT re-query.
    assert ea._sole_local_user_id() is None


def test_multi_tenant_negative_is_still_cached(monkeypatch):
    """>1 users is stable and must stay cached — no behaviour change there."""
    ea = _install_fake_db(monkeypatch, [_FakeUser('a'), _FakeUser('b')])

    assert ea._sole_local_user_id() is None
    assert ea._SOLE_USER_CACHE == [None], "multi-tenant must pay exactly one query"
