"""Behavioural tests for the flywheel-observability fix in mcp_server.dispatch_goal.

Before this fix:
  - MCP dispatch_goal called dispatch.py:dispatch_goal but never wrote
    back to the AgentGoal row.  Only agent_daemon (which had been
    silent for 3+ days on the live install) updated last_dispatched_at.
  - Result: every MCP-driven flywheel attempt looked identical to a
    stalled goal in list_goals output — no progress trail.

After this fix:
  - Each MCP dispatch stamps `last_dispatched_at = utcnow()` on the
    AgentGoal row, regardless of whether the daemon is alive.
  - The 'system' string sentinel is replaced with
    UserService.ensure_system_user(db, 'nunba', ...) so FK constraints
    on posts.author_id / goals.created_by are satisfied.

These tests exercise the real MCP handler against an in-memory DB —
no network, no live agent, just the persistence contract.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

# The stdio MCP transport is OPTIONAL by design: integrations/mcp/mcp_server.py
# raises a helpful ImportError without the `mcp` package and points at the
# HTTP bridge as the no-install path. These tests exercise mcp_server's
# dispatch persistence, so they need the optional package; where it is not
# installed (the release-gate runners, per the module's own design) they skip
# with the same message instead of failing collection.
pytest.importorskip(
    'mcp.server.fastmcp',
    reason="optional stdio MCP transport not installed (pip install mcp); "
           "the HTTP bridge path needs no extra install. Checked at the "
           "submodule mcp_server actually imports, because an unrelated "
           "package can squat the top-level `mcp` name.",
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fresh_db(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db
    eng = get_engine()
    migrations.run_migrations()
    db = get_db()
    try:
        yield db, eng
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            eng.dispose()
        except Exception:
            pass
        models_mod._engine = None
        models_mod._SessionLocal = None


def _seed_goal(db):
    """Insert one AgentGoal with last_dispatched_at=None so we can
    assert the persistence."""
    from integrations.social.models import AgentGoal
    g = AgentGoal(
        id=str(uuid.uuid4()),
        goal_type='marketing',
        title='Test flywheel goal',
        description='Drive flywheel — test description',
        status='active',
        spark_budget=200,
        spark_spent=0,
        last_dispatched_at=None,
    )
    db.add(g)
    db.commit()
    return g


def _patch_mcp_db(monkeypatch, db):
    """Make the MCP server use the test DB instead of get_db()."""
    import integrations.mcp.mcp_server as mod
    monkeypatch.setattr(mod, '_get_db', lambda: db)


def _patch_dispatch_to_record(monkeypatch, calls):
    """Replace dispatch.py:dispatch_goal with a recording shim so
    we don't need an LLM running in CI."""
    from integrations.agent_engine import dispatch as dispatch_mod

    def _shim(prompt, user_id, goal_id, goal_type, **kwargs):
        calls.append({
            'prompt': prompt[:50],
            'user_id': user_id,
            'goal_id': goal_id,
            'goal_type': goal_type,
        })
        return f"dispatched-{goal_id[:8]}"

    monkeypatch.setattr(dispatch_mod, 'dispatch_goal', _shim)


# ── Persistence contract ───────────────────────────────────────

def test_mcp_dispatch_stamps_last_dispatched_at(fresh_db, monkeypatch):
    """After MCP dispatch_goal, the AgentGoal row's last_dispatched_at
    transitions from None → ~utcnow()."""
    db, _ = fresh_db
    goal = _seed_goal(db)
    pre_time = datetime.utcnow()

    _patch_mcp_db(monkeypatch, db)
    calls = []
    _patch_dispatch_to_record(monkeypatch, calls)

    from integrations.mcp.mcp_server import dispatch_goal
    import json
    result = json.loads(dispatch_goal(goal_id=goal.id, goal_type='marketing'))
    assert result.get('dispatched') is True
    assert len(calls) == 1, "dispatch.py:dispatch_goal must be invoked"

    # Re-fetch the row from a fresh transaction to confirm the write
    # actually committed (not just flushed into the in-flight session).
    from integrations.social.models import AgentGoal
    db.expire_all()
    row = db.query(AgentGoal).filter_by(id=goal.id).first()
    assert row.last_dispatched_at is not None, (
        "MCP dispatch must persist last_dispatched_at — pre-fix this "
        "stayed None forever and the flywheel looked stalled"
    )
    assert row.last_dispatched_at >= pre_time


def test_mcp_dispatch_uses_nunba_system_user(fresh_db, monkeypatch):
    """The MCP handler must resolve a real Nunba User row (FK-correct)
    instead of the literal 'system' string sentinel."""
    db, _ = fresh_db
    goal = _seed_goal(db)
    _patch_mcp_db(monkeypatch, db)
    calls = []
    _patch_dispatch_to_record(monkeypatch, calls)

    from integrations.mcp.mcp_server import dispatch_goal
    dispatch_goal(goal_id=goal.id, goal_type='marketing')

    assert len(calls) == 1
    user_id = calls[0]['user_id']

    # Must be a real UUID — not the literal 'system' string.
    assert user_id != 'system', (
        "MCP dispatch passed the literal 'system' sentinel as user_id "
        "— this fails FK constraints downstream (posts.author_id, etc.)"
    )
    # And the User row must exist + have user_type='system'.
    from integrations.social.models import User
    sys_user = db.query(User).filter_by(id=user_id).first()
    assert sys_user is not None
    assert sys_user.username == 'nunba'
    assert sys_user.user_type == 'system'


def test_mcp_dispatch_idempotent_user_creation(fresh_db, monkeypatch):
    """Two dispatches must NOT create two Nunba users — one identity
    across all flywheel publish paths."""
    db, _ = fresh_db
    goal = _seed_goal(db)
    _patch_mcp_db(monkeypatch, db)
    calls = []
    _patch_dispatch_to_record(monkeypatch, calls)

    from integrations.mcp.mcp_server import dispatch_goal
    dispatch_goal(goal_id=goal.id, goal_type='marketing')
    dispatch_goal(goal_id=goal.id, goal_type='marketing')

    from integrations.social.models import User
    nunbas = db.query(User).filter_by(username='nunba').all()
    assert len(nunbas) == 1, (
        f"expected 1 Nunba identity, got {len(nunbas)} — "
        f"ensure_system_user idempotency broken in mcp_server"
    )


def test_mcp_dispatch_persists_even_when_dispatch_returns_none(
        fresh_db, monkeypatch):
    """A None return from dispatch.py (e.g. user_active cooldown,
    circuit breaker open) MUST still persist last_dispatched_at so
    operators can see the attempt happened.  Otherwise transient
    skips look identical to "never tried"."""
    db, _ = fresh_db
    goal = _seed_goal(db)
    _patch_mcp_db(monkeypatch, db)

    from integrations.agent_engine import dispatch as dispatch_mod
    monkeypatch.setattr(dispatch_mod, 'dispatch_goal',
                        lambda *a, **kw: None)  # simulate cooldown skip

    from integrations.mcp.mcp_server import dispatch_goal
    dispatch_goal(goal_id=goal.id, goal_type='marketing')

    from integrations.social.models import AgentGoal
    db.expire_all()
    row = db.query(AgentGoal).filter_by(id=goal.id).first()
    assert row.last_dispatched_at is not None
