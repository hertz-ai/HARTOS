"""test_voice_profile_migration.py — regression guard for the v37 schema drift.

Background (2026-04-25 incident):
- Three orchestrator subagents (a669afda, abc2296d, ac7d3a19) independently
  reported `sqlite3.OperationalError: no such column: users.voice_profile`
  blocking every daemon dispatch and forcing fall-through to Gate 8 self-review.
- Root cause: a live SQLite at `agent_data/hevolve_database.db` was at
  schema_version=36; the v37 migration (which ALTERs `users` to add the
  `voice_profile TEXT` column) had been added to `migrations.py` but the
  daemon process never re-invoked `run_migrations()` after deploy, so the
  ORM and the live schema drifted.

This test does NOT just check the User class — `test_voice_profile_persistence.py`
already does that on a freshly created in-memory schema (which would always
pass because Base.metadata.create_all() makes every column declared on the
model).  This test specifically guards the *migration path*: starting from a
v36-state DB (the precise state we found in the wild), running
`run_migrations()` must bring schema_version to >= 37 AND add the column.

If a future change to migrations.py renumbers / merges / drops v37 without
preserving the ALTER, this test fails — exactly the kind of silent
re-removal the original fix wanted to prevent.
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def v36_db(tmp_path, monkeypatch):
    """Build a real on-disk SQLite at schema_version=36 with no voice_profile column.

    Mirrors what we found at `agent_data/hevolve_database.db` 2026-04-25.
    Uses an isolated tmp_path so this test never touches the developer's
    real DB.
    """
    db_path = tmp_path / 'hevolve_database.db'
    monkeypatch.setenv('HEVOLVE_DB_PATH', str(db_path))
    monkeypatch.delenv('HEVOLVE_DB_URL', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)

    # Force a fresh engine — the module caches one globally.
    import importlib
    from integrations.social import models as models_mod
    importlib.reload(models_mod)

    # Bootstrap the schema and stamp it at v36 (pre-voice_profile).
    from sqlalchemy import text
    eng = models_mod.get_engine()
    models_mod.Base.metadata.create_all(eng)

    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS social_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"))
        # Drop the voice_profile column to truly emulate v36.  SQLite < 3.35
        # has no DROP COLUMN, so we rebuild the users table without it.
        cols = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        col_names = [c[1] for c in cols]
        if 'voice_profile' in col_names:
            keep_cols = [c for c in col_names if c != 'voice_profile']
            cols_csv = ', '.join(keep_cols)
            conn.execute(text(f"CREATE TABLE users_v36 AS SELECT {cols_csv} FROM users"))
            conn.execute(text("DROP TABLE users"))
            conn.execute(text("ALTER TABLE users_v36 RENAME TO users"))
        conn.execute(text(
            "INSERT OR REPLACE INTO social_meta (key, value) "
            "VALUES ('schema_version', '36')"))
        conn.commit()

    yield str(db_path), models_mod

    # Tear down: dispose engine so the file handle is released before tmp_path
    # cleanup.  Critical on Windows where open SQLite handles block deletion.
    try:
        eng.dispose()
    except Exception:
        pass


def test_v37_migration_adds_voice_profile_column(v36_db):
    """Running run_migrations() against a v36 DB must add users.voice_profile."""
    db_path, models_mod = v36_db
    from sqlalchemy import text
    from integrations.social.migrations import run_migrations, get_schema_version

    eng = models_mod.get_engine()

    # Pre-condition: column absent, version 36.  This is the exact failing
    # state we found in the wild.
    with eng.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
    assert 'voice_profile' not in cols, (
        "Fixture failed to emulate v36 — voice_profile shouldn't be there yet")
    assert get_schema_version(eng) == 36

    # Act: run all pending migrations.
    run_migrations()

    # Post-condition: column present, version >= 37.
    with eng.connect() as conn:
        cols_after = [r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
    assert 'voice_profile' in cols_after, (
        "v37 migration regressed: ALTER TABLE users ADD COLUMN voice_profile is gone")
    assert get_schema_version(eng) >= 37


def test_user_query_does_not_raise_after_migration(v36_db):
    """The original failure mode: db.query(User).first() raised OperationalError.

    Confirm that after run_migrations(), the exact query path the daemon uses
    no longer raises.
    """
    db_path, models_mod = v36_db
    from integrations.social.migrations import run_migrations

    run_migrations()

    db = models_mod.get_db()
    try:
        # This is the call site that was failing for the daemon.
        # If voice_profile column is missing, SQLAlchemy emits SELECT ...
        # users.voice_profile ... and SQLite raises OperationalError.
        result = db.query(models_mod.User).first()
        # No assertion on count — the DB may be empty.  We just need the
        # query to not raise.
        _ = result
    finally:
        db.close()
