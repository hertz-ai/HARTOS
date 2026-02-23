"""
Coding Agent Benchmark Tracker — SQLite-backed performance tracking.

Records task completion time and success rate per tool, task type, and model.
Exports compact deltas for hive distributed learning via FederatedAggregator.

DB location: agent_data/coding_benchmarks.db
"""
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.coding_agent')

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'agent_data', 'coding_benchmarks.db'
)

# Minimum samples before a tool is considered "benchmarked" for a task type
MIN_SAMPLES = 5


class BenchmarkTracker:
    """SQLite benchmark tracker — thread-safe singleton."""

    def __init__(self, db_path: str = _DB_PATH):
        self._db_path = db_path
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
                    timestamp REAL NOT NULL
                )
            ''')
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
                ' success, offloaded, timestamp) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (task_type, tool_name, model_name, user_id,
                 completion_time_s, int(success), int(offloaded), time.time())
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
                WHERE task_type = ?
                GROUP BY tool_name
                HAVING cnt >= ?
                ORDER BY success_rate DESC, avg_time ASC
                LIMIT 1
            ''', (task_type, MIN_SAMPLES)).fetchall()
            conn.close()

        if rows:
            return (rows[0][0], rows[0][1], rows[0][2])
        return None

    def get_hive_best_tool(self, task_type: str) -> Optional[str]:
        """Get best tool from hive-aggregated intelligence."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                'SELECT best_tool FROM hive_routing WHERE task_type = ?',
                (task_type,)
            ).fetchone()
            conn.close()
        return row[0] if row else None

    def get_summary(self) -> Dict:
        """Dashboard summary data."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            total = conn.execute('SELECT COUNT(*) FROM benchmarks').fetchone()[0]
            by_tool = conn.execute('''
                SELECT tool_name,
                       COUNT(*) as total,
                       AVG(success) as success_rate,
                       AVG(completion_time_s) as avg_time
                FROM benchmarks
                GROUP BY tool_name
            ''').fetchall()
            by_task = conn.execute('''
                SELECT task_type,
                       tool_name,
                       COUNT(*) as total,
                       AVG(success) as success_rate,
                       AVG(completion_time_s) as avg_time
                FROM benchmarks
                GROUP BY task_type, tool_name
                ORDER BY task_type, success_rate DESC
            ''').fetchall()
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
                GROUP BY task_type, tool_name
                HAVING cnt >= ?
            ''', (MIN_SAMPLES,)).fetchall()
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
