"""The dispatch stamp must survive the completion gate and reach the row.

Measured on central 2026-09-03 with a SQLAlchemy before_cursor_execute
listener: the daemon tick reached `goal.last_dispatched_at = utcnow()`, ran to
`db.commit()`, and emitted NO `UPDATE agent_goals` statement at all. Goal rows
sat frozen from 2026-09-02 12:32 -- the hour the completion gate shipped --
while the daemon dispatched every 30 seconds for two days. Every diagnosis that
looked at the daemon's logs, its gates or the DB's write path found nothing,
because all of those were healthy.

Two independent SQLAlchemy traps, both in _settle_dispatched_goal's first lines:

  1. `db.refresh(goal)` EXPIRES the instance and reloads it from the database,
     discarding every un-flushed pending change on it -- the stamp and the
     spark_at_dispatch snapshot the tick had just set. The object still showed
     up in `session.dirty` (the attribute HAD been set), but its net diff was
     empty, so flush emitted nothing.

  2. `config_json` is a plain JSON column, not a MutableDict. Mutating the dict
     the attribute already holds and assigning that same object back compares
     equal at flush time, so the column is never written. Every marker the gate
     records was lost this way even when a write did happen.

Behavioural: real sqlite file, real session, real _settle_dispatched_goal, and
the assertion is what a second session reads back.

    python -m pytest tests/unit/test_goal_stamp_persists.py -q --noconftest
"""
import os
import tempfile
from datetime import datetime

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")


@pytest.fixture
def db_path(monkeypatch):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "hevolve_database.db")
    monkeypatch.setenv("HEVOLVE_DB_PATH", p)
    monkeypatch.setenv("HEVOLVE_DB_URL", "sqlite:///" + p.replace("\\", "/"))
    from integrations.social import models
    # Both caches are bound to the old engine — clear them or the fixture is
    # a no-op and the test silently runs against the developer's real DB.
    for attr in ("_engine", "_ENGINE", "_engine_cache", "_SessionLocal"):
        if hasattr(models, attr):
            monkeypatch.setattr(models, attr, None, raising=False)
    return p


def _seed_goal(db, goal_id, **kw):
    from integrations.social.models import AgentGoal
    g = AgentGoal(
        id=goal_id,
        goal_type=kw.get("goal_type", "hive_growth"),
        title=kw.get("title", "seeded"),
        status="active",
    )
    for attr, val in (("spark_budget", 500),
                      ("spark_spent", kw.get("spark_spent", 10)),
                      ("config_json", kw.get("config_json", {"continuous": True}))):
        if hasattr(g, attr):
            setattr(g, attr, val)
    db.add(g)
    db.commit()
    return g


def test_the_dispatch_stamp_reaches_the_row(db_path):
    """The production failure, reproduced: stamp the goal exactly as _tick
    does, run the REAL gate, commit, and read the row back in a NEW session."""
    from integrations.social.migrations import run_migrations
    from integrations.social.models import get_db, AgentGoal
    from integrations.agent_engine.agent_daemon import _settle_dispatched_goal

    run_migrations()
    db = get_db()
    g = _seed_goal(db, "g-stamp")

    # Exactly what _tick does, in order, before the gate runs.
    stamp = datetime.utcnow()
    g.last_dispatched_at = stamp
    cfg = dict(g.config_json or {})
    cfg["spark_at_dispatch"] = g.spark_spent or 0
    g.config_json = cfg

    _settle_dispatched_goal(db, g, "g-stamp")
    db.commit()
    db.close()

    db2 = get_db()
    row = db2.query(AgentGoal).filter_by(id="g-stamp").first()
    assert row.last_dispatched_at is not None, (
        "the completion gate discarded the dispatch stamp: refresh() reverted "
        "an un-flushed change, so no UPDATE was ever emitted")
    assert (row.config_json or {}).get("spark_at_dispatch") == 10, (
        "spark_at_dispatch did not reach the column — config_json was mutated "
        "in place, which a plain JSON column cannot detect")
    db2.close()


def test_the_gate_flushes_before_it_refreshes(db_path):
    """The ordering itself, named. refresh() before flush() is the defect;
    a seam records the order so a reordering cannot pass silently."""
    from integrations.agent_engine.agent_daemon import _settle_dispatched_goal

    calls = []

    class Db:
        def flush(self):
            calls.append("flush")

        def refresh(self, obj):
            calls.append("refresh")

    class Goal:
        spark_spent = 5
        status = "active"
        config_json = {"continuous": True}

    _settle_dispatched_goal(Db(), Goal(), "g1")

    assert "flush" in calls, \
        "the gate no longer flushes, so refresh() will revert the tick's stamp"
    assert calls.index("flush") < calls.index("refresh"), \
        "flush must precede refresh, or the pending stamp is reloaded away"


def test_the_gate_writes_a_new_config_dict_not_the_one_it_read(db_path):
    """A plain JSON column only registers a change when the value compares
    unequal. Handing back the same dict the attribute already held is the
    silent-no-write trap, and it hid every marker this gate records."""
    from integrations.agent_engine.agent_daemon import _settle_dispatched_goal

    original = {"continuous": False, "spark_at_dispatch": 0}

    class Db:
        def flush(self):
            pass

        def refresh(self, obj):
            pass

    class Goal:
        def __init__(self):
            self.spark_spent = 0
            self.status = "active"
            self.config_json = original

    g = Goal()
    _settle_dispatched_goal(Db(), g, "g1")

    assert g.config_json is not original, (
        "the gate handed back the same dict object it read, so SQLAlchemy "
        "compares it equal and never writes the column")
