"""run_migrations() must create tables added AFTER the DB was first stamped.

REAL-HW REGRESSION (steward's flashed node, 2026-07-30). `create_all` was
gated on `schema_version < 1`, so it only ever ran against a virgin DB. Any
model added after that first stamp never got its table on an existing
database — the version ladder only ALTERs columns, it never creates. The node
logged, from BOTH the dashboard query and the agent-daemon tick:

    (sqlite3.OperationalError) no such table: agent_goals

which is why the "continue agents" cards vanished from the shell.

Behavioural: drives the REAL run_migrations() against a REAL sqlite file and
asserts the observable side-effect (the table exists / is recreated), never a
source grep.

    python -m pytest tests/unit/test_migrations_create_new_tables.py -q --noconftest
"""
import os
import tempfile

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import inspect, text  # noqa: E402


@pytest.fixture
def db_path(monkeypatch):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "hevolve_database.db")
    monkeypatch.setenv("HEVOLVE_DB_PATH", p)
    monkeypatch.setenv("HEVOLVE_DB_URL", "sqlite:///" + p.replace("\\", "/"))
    # get_engine caches per-process — clear it so the env above is honoured.
    from integrations.social import models
    for attr in ("_engine", "_ENGINE", "_engine_cache"):
        if hasattr(models, attr):
            monkeypatch.setattr(models, attr, None, raising=False)
    return p


def _table_names(engine):
    return set(inspect(engine).get_table_names())


def test_new_table_appears_on_an_already_stamped_db(db_path):
    """The exact node failure: a DB stamped at v1 that is MISSING a table the
    models define must gain that table on the next migration pass."""
    from integrations.social.migrations import run_migrations
    from integrations.social.models import get_engine

    run_migrations()                       # virgin DB -> everything created
    engine = get_engine()
    assert "agent_goals" in _table_names(engine), \
        "agent_goals must exist after a fresh migration (model is registered)"

    # Simulate the shipped node: the DB is stamped, but a table the models
    # define is absent (it was added to the models after this DB was created).
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE agent_goals"))
        conn.commit()
    assert "agent_goals" not in _table_names(engine)

    run_migrations()                       # the pass that used to be a no-op
    assert "agent_goals" in _table_names(engine), \
        ("run_migrations must create tables missing from an ALREADY-STAMPED db "
         "— this is the 'no such table: agent_goals' the node hit")


def test_migrations_are_idempotent_and_preserve_rows(db_path):
    """Re-running must not wipe data (create_all is checkfirst, never DROP)."""
    from integrations.social.migrations import run_migrations
    from integrations.social.models import get_engine

    run_migrations()
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO agent_goals (id, goal_type, title, status) "
            "VALUES ('g1', 'coding', 'keep me', 'active')"))
        conn.commit()

    run_migrations()
    run_migrations()

    with engine.connect() as conn:
        rows = list(conn.execute(text("SELECT id, title FROM agent_goals")))
    assert [tuple(r) for r in rows] == [("g1", "keep me")], \
        "re-running migrations must preserve existing rows"


def test_schema_version_still_stamps(db_path):
    """The version ladder keeps working (column steps still gate on it)."""
    from integrations.social.migrations import run_migrations, get_schema_version
    from integrations.social.models import get_engine

    run_migrations()
    assert get_schema_version(get_engine()) >= 1
