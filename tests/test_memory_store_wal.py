"""PERF-4 (audit #566): MemoryStore must use WAL + synchronous=NORMAL.

register_conversation does 2 INSERTs (+ FTS5 trigger writes) per chat turn; the
default rollback journal + synchronous=FULL fsyncs per statement (5-30x slower).
Matches the sibling embeddings.py WAL idiom.  The ":memory:" path must stay a
safe no-op (in-memory DBs cannot use WAL).
"""
import importlib


def _Store():
    return importlib.import_module(
        'integrations.channels.memory.memory_store').MemoryStore


def test_pragmas_applied_on_file_db(tmp_path):
    store = _Store()(db_path=str(tmp_path / 'mem.db'))
    conn = store._ensure_connection()
    assert conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    assert conn.execute('PRAGMA synchronous').fetchone()[0] == 1      # NORMAL
    assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 30000


def test_wal_sidecar_created_after_write(tmp_path):
    db = tmp_path / 'mem.db'
    store = _Store()(db_path=str(db))
    conn = store._ensure_connection()
    conn.execute(
        "INSERT INTO memory_items (id, content, created_at, updated_at) "
        "VALUES (?,?,?,?)", ('t1', 'hello', 0.0, 0.0))
    assert (tmp_path / 'mem.db-wal').exists()


def test_inmemory_db_does_not_error():
    # ":memory:" can't use WAL — the PRAGMA must be a safe no-op, not a crash.
    store = _Store()(db_path=None)   # → ":memory:"
    conn = store._ensure_connection()
    mode = conn.execute('PRAGMA journal_mode').fetchone()[0].lower()
    assert mode in ('memory', 'wal')   # never raises; stays 'memory' for :memory:
