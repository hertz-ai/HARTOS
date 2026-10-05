"""
Coding Agent Benchmark Tracker — SQLite-backed performance tracking.

Records task completion time and success rate per tool, task type, and model.
Exports compact deltas for hive distributed learning via FederatedAggregator.

DB location: <core.platform_paths.get_agent_data_dir()>/coding_benchmarks.db
(the user data dir, never the install tree: an installed Nunba cannot write
under Program Files, and a failed write there cost every coding task its
result; see tests/unit/test_coding_result_survives_benchmark_write.py).
"""
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.coding_agent')

_DB_FILENAME = 'coding_benchmarks.db'


def _default_db_path() -> str:
    """The benchmark DB in the canonical agent data dir, created if absent."""
    from core.platform_paths import get_agent_data_dir
    agent_dir = get_agent_data_dir()
    os.makedirs(agent_dir, exist_ok=True)
    return os.path.join(agent_dir, _DB_FILENAME)

# Minimum samples before a tool is considered "benchmarked" for a task type
MIN_SAMPLES = 5

#: What a row's ``success`` means.  1: whatever the backend said; aider_native
#: said True for any model reply, edit or not (all 1,797 of its rows on the
#: owner's desktop, 2026-10-05, with 0 edits applied in the 442 runs the logs
#: still held).  2: an edit backend succeeds only when an edit was applied, a
#: receipt (owner ruling 2026-10-04).  Routing, the summary and the hive export
#: read only rows recorded under the current rule: rows recorded under another
#: measure a different thing, and the old lie would elect its own tool.  The
#: old rows stay on disk, untouched.
SUCCESS_RULE = 2


class BenchmarkTracker:
    """SQLite benchmark tracker — thread-safe singleton."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or _default_db_path()
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS benchmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    model_name TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    completion_time_s REAL NOT NULL,
                    success INTEGER NOT NULL DEFAULT 0,
                    offloaded INTEGER NOT NULL DEFAULT 0,
                    timestamp REAL NOT NULL,
                    success_rule INTEGER NOT NULL DEFAULT 1
                )
            ''')
            # A table made before the column existed gets it, every old row
            # marked rule 1 by the column default.
            columns = {row[1] for row in conn.execute(
                'PRAGMA table_info(benchmarks)')}
            if 'success_rule' not in columns:
                conn.execute('ALTER TABLE benchmarks ADD COLUMN '
                             'success_rule INTEGER NOT NULL DEFAULT 1')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS hive_routing (
                    task_type TEXT PRIMARY KEY,
                    best_tool TEXT NOT NULL,
                    success_rate REAL NOT NULL,
                    avg_time_s REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_benchmarks_task_tool
                ON benchmarks(task_type, tool_name)
            ''')
            conn.commit()
            conn.close()

    def record(self, task_type: str, tool_name: str, completion_time_s: float,
               success: bool, model_name: str = '', user_id: str = '',
               offloaded: bool = False):
        """Record a benchmark entry."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                'INSERT INTO benchmarks '
                '(task_type, tool_name, model_name, user_id, completion_time_s, '
                ' success, offloaded, timestamp, success_rule) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (task_type, tool_name, model_name, user_id,
                 completion_time_s, int(success), int(offloaded), time.time(),
                 SUCCESS_RULE)
            )
            conn.commit()
            conn.close()

    def get_best_tool(self, task_type: str) -> Optional[Tuple[str, float, float]]:
        """Get best tool for a task type based on local benchmarks.

        Returns (tool_name, success_rate, avg_time) or None if insufficient data.
        Requires MIN_SAMPLES entries.
        """
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute('''
                SELECT tool_name,
                       AVG(success) as success_rate,
                       AVG(completion_time_s) as avg_time,
                       COUNT(*) as cnt
                FROM benchmarks
                WHERE task_type = ? AND success_rule = ?
                GROUP BY tool_name
                HAVING cnt >= ?
                ORDER BY success_rate DESC, avg_time ASC
                LIMIT 1
            ''', (task_type, SUCCESS_RULE, MIN_SAMPLES)).fetchall()
            conn.close()

        if rows:
            return (rows[0][0], rows[0][1], rows[0][2])
        return None

    def get_hive_best_tool(self, task_type: str) -> Optional[Tuple[str, float, float]]:
        """Get best tool from hive-aggregated intelligence.

        Returns (tool_name, success_rate, avg_time_s) or None — same shape
        as get_best_tool() so callers can index consistently.
        """
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                'SELECT best_tool, success_rate, avg_time_s FROM hive_routing WHERE task_type = ?',
                (task_type,)
            ).fetchone()
            conn.close()
        return (row[0], row[1], row[2]) if row else None

    def get_summary(self) -> Dict:
        """Dashboard summary data."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            total = conn.execute(
                'SELECT COUNT(*) FROM benchmarks WHERE success_rule = ?',
                (SUCCESS_RULE,)).fetchone()[0]
            by_tool = conn.execute('''
                SELECT tool_name,
                       COUNT(*) as total,
                       AVG(success) as success_rate,
                       AVG(completion_time_s) as avg_time
                FROM benchmarks
                WHERE success_rule = ?
                GROUP BY tool_name
            ''', (SUCCESS_RULE,)).fetchall()
            by_task = conn.execute('''
                SELECT task_type,
                       tool_name,
                       COUNT(*) as total,
                       AVG(success) as success_rate,
                       AVG(completion_time_s) as avg_time
                FROM benchmarks
                WHERE success_rule = ?
                GROUP BY task_type, tool_name
                ORDER BY task_type, success_rate DESC
            ''', (SUCCESS_RULE,)).fetchall()
            conn.close()

        return {
            'total_benchmarks': total,
            'by_tool': [
                {'tool': r[0], 'total': r[1],
                 'success_rate': round(r[2], 3), 'avg_time_s': round(r[3], 2)}
                for r in by_tool
            ],
            'by_task_type': [
                {'task_type': r[0], 'tool': r[1], 'total': r[2],
                 'success_rate': round(r[3], 3), 'avg_time_s': round(r[4], 2)}
                for r in by_task
            ],
        }

    # ─── Hive learning integration ───

    def export_learning_delta(self) -> Optional[Dict]:
        """Export benchmark stats as a compact delta for hive learning.

        Format: {task_type → {tool → {success_rate, avg_time, count}}}
        Only exports task types with MIN_SAMPLES data.
        """
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute('''
                SELECT task_type, tool_name,
                       AVG(success) as sr, AVG(completion_time_s) as at,
                       COUNT(*) as cnt
                FROM benchmarks
                WHERE success_rule = ?
                GROUP BY task_type, tool_name
                HAVING cnt >= ?
            ''', (SUCCESS_RULE, MIN_SAMPLES)).fetchall()
            conn.close()

        if not rows:
            return None

        delta = {}
        for task_type, tool, sr, at, cnt in rows:
            if task_type not in delta:
                delta[task_type] = {}
            delta[task_type][tool] = {
                'success_rate': round(sr, 3),
                'avg_time_s': round(at, 2),
                'sample_count': cnt,
            }

        return {'coding_benchmarks': delta, 'ts': time.time()}

    def import_hive_delta(self, aggregated: Dict):
        """Apply hive-aggregated routing intelligence to local hive_routing table.

        Merges peer benchmarks with a decay factor — local data always
        takes priority over hive data.
        """
        benchmarks = aggregated.get('coding_benchmarks', {})
        if not benchmarks:
            return

        with self._lock:
            conn = sqlite3.connect(self._db_path)
            for task_type, tools in benchmarks.items():
                if not tools:
                    continue
                # Find best tool across hive peers
                best = max(tools.items(),
                           key=lambda x: (x[1].get('success_rate', 0),
                                          -x[1].get('avg_time_s', 999)))
                tool_name, stats = best
                conn.execute('''
                    INSERT INTO hive_routing (task_type, best_tool, success_rate,
                                              avg_time_s, sample_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_type) DO UPDATE SET
                        best_tool = excluded.best_tool,
                        success_rate = excluded.success_rate,
                        avg_time_s = excluded.avg_time_s,
                        sample_count = excluded.sample_count,
                        updated_at = excluded.updated_at
                ''', (task_type, tool_name,
                      stats.get('success_rate', 0),
                      stats.get('avg_time_s', 0),
                      stats.get('sample_count', 0),
                      time.time()))
            conn.commit()
            conn.close()
            logger.info(f"Imported hive routing delta for {len(benchmarks)} task types")


# ─── Module-level singleton ───
_tracker = None
_tracker_lock = threading.Lock()


def get_benchmark_tracker() -> BenchmarkTracker:
    """Get or create the singleton BenchmarkTracker."""
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = BenchmarkTracker()
    return _tracker
