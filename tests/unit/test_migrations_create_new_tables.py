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


def test_a_column_added_to_a_model_reaches_an_already_stamped_db(db_path):
    """Central, 2026-09-03: `comments.agent_id` and `comments.privacy` were
    added to the Comment model with no migration behind them, so every DB
    created before that change lacked both. SQLAlchemy selects every mapped
    column, so this did not degrade -- it 500'd the whole endpoint:

        sqlite3.OperationalError: no such column: comments.agent_id
          services.py:761 CommentService.get_by_post -> q.all()

    30 identical failures in a 30-minute window, i.e. every attempt to read the
    comments on any post. Same shape as the missing-table regression above:
    a model changed, the ladder did not.

    Behavioural — drives the REAL run_migrations() against a REAL sqlite file
    whose `comments` table is missing the columns, and asserts the ORM query
    that was failing now runs."""
    from integrations.social.migrations import run_migrations, set_schema_version
    from integrations.social.models import get_engine

    run_migrations()
    engine = get_engine()

    # Rebuild `comments` as it existed before the model gained the two
    # columns, and roll the stamp back so the ladder has to do the work.
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE comments"))
        conn.execute(text(
            "CREATE TABLE comments ("
            " id VARCHAR(64) PRIMARY KEY,"
            " post_id VARCHAR(64) NOT NULL,"
            " author_id VARCHAR(64) NOT NULL,"
            " parent_id VARCHAR(64),"
            " content TEXT NOT NULL,"
            " upvotes INTEGER DEFAULT 0,"
            " downvotes INTEGER DEFAULT 0,"
            " score INTEGER DEFAULT 0,"
            " depth INTEGER DEFAULT 0,"
            " is_deleted BOOLEAN DEFAULT 0,"
            " is_hidden BOOLEAN DEFAULT 0,"
            " created_at DATETIME,"
            " updated_at DATETIME)"))
        conn.commit()
    # The stamp lives in social_meta, not a table of its own — go through the
    # canonical setter so this test cannot drift from the ladder's storage.
    set_schema_version(engine, 53)

    live = {c["name"] for c in inspect(engine).get_columns("comments")}
    assert "agent_id" not in live and "privacy" not in live, \
        "precondition: the table must start without the drifted columns"

    run_migrations()

    live = {c["name"] for c in inspect(engine).get_columns("comments")}
    for col in ("agent_id", "privacy"):
        assert col in live, (
            f"comments.{col} is in the model but no migration adds it, so any "
            f"DB created before the model change 500s on every comment read")


def test_the_model_and_a_migrated_db_agree_on_every_column(db_path):
    """The general invariant the case above is one instance of: after
    run_migrations(), no mapped column may be missing from its live table.

    A drift sweep over Base.metadata is what found the comments pair on
    central, and it found exactly those two -- so this assertion is the cheap
    standing version of that sweep."""
    from integrations.social.migrations import run_migrations
    from integrations.social.models import get_engine, Base

    run_migrations()
    engine = get_engine()
    insp = inspect(engine)

    drift = {}
    for name, table in Base.metadata.tables.items():
        if not insp.has_table(name):
            continue
        live = {c["name"] for c in insp.get_columns(name)}
        missing = [c.name for c in table.columns if c.name not in live]
        if missing:
            drift[name] = missing

    assert not drift, f"model columns with no table column behind them: {drift}"
