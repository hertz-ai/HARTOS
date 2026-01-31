"""
HevolveSocial - Schema Migrations
Version tracking and migration helpers.
"""
import logging
from sqlalchemy import text
from .models import get_engine, Base

logger = logging.getLogger('hevolve_social')

SCHEMA_VERSION = 1


def get_schema_version(engine) -> int:
    """Get current schema version from DB."""
    try:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT value FROM social_meta WHERE key = 'schema_version'"))
            row = result.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def set_schema_version(engine, version: int):
    """Set schema version in DB."""
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS social_meta (key TEXT PRIMARY KEY, value TEXT)"))
        conn.execute(text(
            "INSERT OR REPLACE INTO social_meta (key, value) VALUES ('schema_version', :v)"),
            {'v': str(version)})
        conn.commit()


def run_migrations():
    """Run any pending migrations."""
    engine = get_engine()
    current = get_schema_version(engine)

    if current < 1:
        logger.info("HevolveSocial: creating initial schema (v1)")
        Base.metadata.create_all(engine)
        set_schema_version(engine, 1)

    # Future migrations go here:
    # if current < 2:
    #     _migrate_v2(engine)
    #     set_schema_version(engine, 2)
