"""
Storage Backends for Agent Ledger

Provides pluggable storage backends for different use cases:
- InMemoryBackend: Fast, no persistence (testing)
- JSONBackend: Simple file-based (development/small scale)
- RedisBackend: Fast in-memory cache (production)
- MongoDBBackend: Document store (large scale)
- PostgreSQLBackend: Relational DB (complex queries)

Usage:
    from agent_ledger.backends import RedisBackend

    backend = RedisBackend(host='localhost', port=6379)
    ledger = SmartLedger(agent_id="my_agent", session_id="session_1", backend=backend)
"""

import json
import os
import threading
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from datetime import datetime


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def save(self, key: str, data: Dict[str, Any]) -> bool:
        """Save data to storage."""
        pass

    @abstractmethod
    def load(self, key: str) -> Optional[Dict[str, Any]]:
        """Load data from storage."""
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete key from storage."""
        pass

    @abstractmethod
    def list_keys(self, pattern: str = "*") -> List[str]:
        """List keys matching pattern."""
        pass


class InMemoryBackend(StorageBackend):
    """
    In-memory storage (for testing/temporary use).

    Pros: Extremely fast, no I/O, perfect for testing
    Cons: Data lost on process exit, no persistence
    """

    def __init__(self):
        self.storage: Dict[str, Dict[str, Any]] = {}

    def save(self, key: str, data: Dict[str, Any]) -> bool:
        try:
            self.storage[key] = data.copy()
            return True
        except Exception as e:
            print(f"[InMemoryBackend] Save error: {e}")
            return False

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        return self.storage.get(key)

    def exists(self, key: str) -> bool:
        return key in self.storage

    def delete(self, key: str) -> bool:
        try:
            if key in self.storage:
                del self.storage[key]
            return True
        except Exception as e:
            print(f"[InMemoryBackend] Delete error: {e}")
            return False

    def list_keys(self, pattern: str = "*") -> List[str]:
        import fnmatch
        return [k for k in self.storage.keys() if fnmatch.fnmatch(k, pattern)]


class JSONBackend(StorageBackend):
    """
    File-based JSON storage (default).

    Pros: Simple, no dependencies, human-readable
    Cons: Slow for large data, no concurrency control
    """

    def __init__(self, storage_dir: str = ".agent_ledger"):
        from pathlib import Path
        self.storage_dir = Path(storage_dir)
        try:
            # parents=True so a nested storage dir works; the old exist_ok-only
            # call needed the parent to exist already.
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A read-only or otherwise unusable location must not raise HERE.
            # This runs during SmartLedger construction, which runs inside the
            # agent daemon's goal dispatch: raising takes the goal engine down
            # over a storage problem. save() already reports its own failures
            # ("[JSONBackend] Save error: ..."), so the honest degradation is to
            # construct, let writes fail loudly, and keep the daemon alive.
            pass

    def _get_path(self, key: str):
        # Sanitize key to prevent path traversal
        safe_key = key.replace('..', '_').replace('/', '_').replace('\\', '_')
        return self.storage_dir / f"{safe_key}.json"

    # Keys whose saved data holds a task in progress: one empty file per key
    # in <storage_dir>/.in_progress/.  A reader that wants only those (the
    # zombie reaper) lists this folder instead of opening every file, so its
    # cost follows the work in flight, not every session ever recorded.  The
    # files carry no .json suffix and sit one level down, so no glob of
    # storage_dir ('*.json', 'ledger_*.json') ever sees them.
    _MARK_DIR = '.in_progress'
    # Its modification time is when SmartLedger.keys_in_progress last started
    # a pass over the unmarked files; the names in _MARK_DIR starting with '.'
    # are the index's own, never keys.
    _MARK_INDEX_STAMP = '.last_pass'

    def _mark_path(self, key: str):
        return self.storage_dir / self._MARK_DIR / self._get_path(key).stem

    def set_mark(self, key: str, on: bool) -> None:
        """Mark ``key`` as holding a task in progress, or clear the mark.
        Idempotent; raises OSError when the mark cannot be written."""
        path = self._mark_path(key)
        if not on:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        try:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY))
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY))

    def marked_keys(self) -> List[str]:
        """The keys currently marked as holding a task in progress."""
        try:
            names = os.listdir(self.storage_dir / self._MARK_DIR)
        except FileNotFoundError:
            return []
        return [n for n in names if not n.startswith('.')]

    def mark_index_time(self) -> Optional[float]:
        """When the last pass over the unmarked files started, or None
        before the first one."""
        try:
            return os.stat(self.storage_dir / self._MARK_DIR
                           / self._MARK_INDEX_STAMP).st_mtime
        except OSError:
            return None

    def stamp_mark_index(self, when: float) -> None:
        """Record that every file written before ``when`` (epoch seconds)
        has been read.  Raises OSError."""
        path = self.storage_dir / self._MARK_DIR / self._MARK_INDEX_STAMP
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY))
        os.utime(path, (when, when))

    def save(self, key: str, data: Dict[str, Any]) -> bool:
        try:
            path = self._get_path(key)
            tmp_path = path.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp_path), str(path))
            return True
        except Exception as e:
            print(f"[JSONBackend] Save error for {path}: {e}")
            # Clean up temp file on failure
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            path = self._get_path(key)
            if not path.exists():
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[JSONBackend] Load error for {path}: {e}")
            return None

    def exists(self, key: str) -> bool:
        return self._get_path(key).exists()

    def delete(self, key: str) -> bool:
        try:
            path = self._get_path(key)
            if path.exists():
                path.unlink()
        except Exception as e:
            print(f"[JSONBackend] Delete error for {path}: {e}")
            return False
        try:
            self.set_mark(key, False)
        except OSError as e:
            # The data is gone; a mark left behind is cleared by the next
            # SmartLedger.keys_in_progress, which finds no file for it.
            print(f"[JSONBackend] Could not clear the in-progress mark for {key}: {e}")
        return True

    def list_keys(self, pattern: str = "*") -> List[str]:
        import fnmatch
        files = [f.stem for f in self.storage_dir.glob("*.json")]
        return [f for f in files if fnmatch.fnmatch(f, pattern)]


#: How long a Redis endpoint that never answered is left alone before a
#: RedisBackend dials it again.  Same number as hartos.helper's frame-read
#: breaker (_REDIS_FRAME_BREAKER), which handles the same absent localhost.
REFUSED_REDIS_COOLDOWN_S = 300

#: The endpoints that did not answer, as core.circuit_breaker.
#: KeyedCircuitBreaker (threshold 1), made on first use.  It stays None when
#: agent_ledger runs without HARTOS's core package; then every construction
#: dials, still under the connect limit below.
_refused_redis = None
_refused_redis_lock = threading.Lock()

#: The endpoints that have answered a ping in this process.  The breaker is
#: for a Redis that is not there at all (a desktop runs none).  A Redis that
#: answered once and then fails is restarting or briefly unreachable, so it
#: is dialled again at once: one failed ping must not send five minutes of
#: new ledgers to JSON (review of 3ff0de70e, finding 2).
_answered_redis: set = set()


def _refused_redis_breaker():
    """The one memory of refused Redis endpoints, or None without core.

    Measured 2026-11-22 on the installed Nunba (request
    msg_1795326762473_nquh6t): every new ledger dialled localhost:6379,
    where a desktop runs no Redis, and one refused connect costs 4.08 s
    there (::1, then 127.0.0.1).  A turn makes two such ledgers
    (get_production_backend, and create_production_ledger through the
    user_ledgers cache loader, under that cache's lock), so the first
    reply waited about 8 s for a server that is not there.
    """
    global _refused_redis
    if _refused_redis is None:
        try:
            from core.circuit_breaker import KeyedCircuitBreaker
        except ImportError:
            return None
        with _refused_redis_lock:
            if _refused_redis is None:
                _refused_redis = KeyedCircuitBreaker(
                    threshold=1, cooldown=REFUSED_REDIS_COOLDOWN_S,
                    name='agent_ledger_redis')
    return _refused_redis


def _probe_redis_client(host: str, port: int, db: int,
                        password: Optional[str]):
    """A one-off client for the reachability ping only: its connect gives up
    after 1 s and nothing is retried.

    core.redis_client.probe_client is the one home of that policy (it
    measured redis-py >= 6 retrying a connect that hangs: 13.8 s, not 1 s),
    and its docstring keeps it to "is Redis there?" checks.  So it is used
    for the ping and closed after; the client RedisBackend keeps is built
    as before, with the library's own retries.  host and port are always
    passed, so its REDIS_HOST default is never used: central's backend has
    no REDIS_HOST, its ledgers are JSON today, and moving them into that
    Redis would hide them from every reader of the ledger folder.
    Raises ImportError when the redis package is missing.
    """
    try:
        from core.redis_client import probe_client
    except ImportError:
        probe_client = None
    if probe_client is not None:
        client = probe_client(host=host, port=port, db=db, password=password)
        if client is None:
            raise ImportError("Redis backend requires 'redis' package: "
                              "pip install redis")
        return client
    import redis  # agent_ledger on its own: the same limit, written here
    return redis.Redis(host=host, port=port, db=db, password=password,
                       decode_responses=True, socket_connect_timeout=1,
                       socket_timeout=2, retry=None)


def _answered_but_said_no(error) -> bool:
    """True when the server answered and refused the request (a wrong
    password, a bad db number): it is there, so it is not shut out.  In
    redis-py AuthenticationError is a kind of ConnectionError, so it is
    asked first."""
    try:
        import redis
    except ImportError:
        return False
    exceptions = redis.exceptions
    answered = tuple(t for t in (getattr(exceptions, 'AuthenticationError', None),
                                 getattr(exceptions, 'ResponseError', None))
                     if t is not None)
    return bool(answered) and isinstance(error, answered)


def _check_redis_answers(host: str, port: int, db: int,
                         password: Optional[str]) -> None:
    """Ping the endpoint through a fail-fast probe; raise ConnectionError,
    saying why, when it cannot be used.

    An endpoint that never answered in this process and did not answer
    within REFUSED_REDIS_COOLDOWN_S is not dialled at all.  The one retry
    the breaker hands out after the cooldown is always settled, even when
    the ping is interrupted by a BaseException (review of 3ff0de70e,
    finding 3).  Raises ImportError when the redis package is missing.
    """
    endpoint = f"{host}:{port}"
    breaker = None if endpoint in _answered_redis else _refused_redis_breaker()
    probe = _probe_redis_client(host, port, db, password)
    if breaker is not None and breaker.is_open(endpoint):
        raise ConnectionError(
            f"Failed to connect to Redis: {endpoint} did not answer in the "
            f"last {REFUSED_REDIS_COOLDOWN_S} s, so it is not dialled again "
            f"until then. Start Redis there, or give another host and port.")
    settled = False
    try:
        try:
            probe.ping()
        except Exception as e:
            if _answered_but_said_no(e):
                _record_answer(endpoint, breaker)
            elif breaker is not None:
                breaker.record_failure(endpoint)
            settled = True
            raise ConnectionError(f"Failed to connect to Redis: {e}")
        _record_answer(endpoint, breaker)
        settled = True
    finally:
        if not settled and breaker is not None:
            breaker.record_failure(endpoint)
        close = getattr(probe, 'close', None)
        if close is not None:
            try:
                close()
            except Exception as e:  # a probe that will not close leaks one socket
                print(f"[RedisBackend] Could not close the probe client: {e}")


def _record_answer(endpoint: str, breaker) -> None:
    """The endpoint answered: never shut it out again in this process."""
    with _refused_redis_lock:
        _answered_redis.add(endpoint)
    if breaker is not None:
        breaker.record_success(endpoint)


class RedisBackend(StorageBackend):
    """
    Redis in-memory storage (FAST!).

    Pros: Extremely fast (in-memory), great for production, handles concurrency
    Cons: Requires Redis server, data not persistent by default

    Performance: ~10-100x faster than JSON for reads/writes

    Installation: pip install redis

    Reachability is asked first, through a one-off probe whose connect gives
    up after 1 s.  An endpoint that never answered and did not answer within
    REFUSED_REDIS_COOLDOWN_S is not dialled: the construction raises
    ConnectionError at once, saying so.  Callers fall back to JSON on
    ConnectionError, as they always have.  The client kept for reads,
    writes, locks and pub/sub is built as it always was.
    """

    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 0,
                 password: Optional[str] = None, prefix: str = "agent_ledger:"):
        try:
            import redis
        except ImportError:
            raise ImportError("Redis backend requires 'redis' package: pip install redis")
        _check_redis_answers(host, port, db, password)
        try:
            self.redis_client = redis.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                decode_responses=True
            )
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Redis: {e}")
        self.prefix = prefix
        print(f"[RedisBackend] Connected to Redis at {host}:{port}")

    def _make_key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def save(self, key: str, data: Dict[str, Any]) -> bool:
        try:
            redis_key = self._make_key(key)
            serialized = json.dumps(data)
            self.redis_client.set(redis_key, serialized)
            return True
        except Exception as e:
            print(f"[RedisBackend] Save error: {e}")
            return False

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            redis_key = self._make_key(key)
            data = self.redis_client.get(redis_key)
            if data is None:
                return None
            return json.loads(data)
        except Exception as e:
            print(f"[RedisBackend] Load error: {e}")
            return None

    def exists(self, key: str) -> bool:
        redis_key = self._make_key(key)
        return self.redis_client.exists(redis_key) > 0

    def delete(self, key: str) -> bool:
        try:
            redis_key = self._make_key(key)
            self.redis_client.delete(redis_key)
            return True
        except Exception as e:
            print(f"[RedisBackend] Delete error: {e}")
            return False

    def list_keys(self, pattern: str = "*") -> List[str]:
        redis_pattern = f"{self.prefix}{pattern}"
        keys = []
        cursor = 0
        while True:
            cursor, batch = self.redis_client.scan(cursor=cursor, match=redis_pattern, count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        return [k.replace(self.prefix, '') if isinstance(k, str) else k.decode().replace(self.prefix, '') for k in keys]


class MongoDBBackend(StorageBackend):
    """
    MongoDB document storage (SCALABLE!).

    Pros: Handles large datasets, flexible schema, good for complex queries
    Cons: Requires MongoDB server, heavier than Redis

    Best for: Large-scale deployments with complex task relationships

    Installation: pip install pymongo
    """

    def __init__(self, host: str = 'localhost', port: int = 27017,
                 database: str = 'agent_ledger', collection: str = 'ledgers',
                 username: Optional[str] = None, password: Optional[str] = None):
        try:
            from pymongo import MongoClient

            if username and password:
                connection_string = f"mongodb://{username}:{password}@{host}:{port}/"
            else:
                connection_string = f"mongodb://{host}:{port}/"

            self.client = MongoClient(connection_string)
            self.db = self.client[database]
            self.collection = self.db[collection]

            # Test connection
            self.client.server_info()
            print(f"[MongoDBBackend] Connected to MongoDB at {host}:{port}")
        except ImportError:
            raise ImportError("MongoDB backend requires 'pymongo' package: pip install pymongo")
        except Exception as e:
            raise ConnectionError(f"Failed to connect to MongoDB: {e}")

    def save(self, key: str, data: Dict[str, Any]) -> bool:
        try:
            doc = {
                "_id": key,
                "data": data,
                "updated_at": datetime.now().isoformat()
            }
            self.collection.replace_one({"_id": key}, doc, upsert=True)
            return True
        except Exception as e:
            print(f"[MongoDBBackend] Save error: {e}")
            return False

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            doc = self.collection.find_one({"_id": key})
            if doc is None:
                return None
            return doc.get("data")
        except Exception as e:
            print(f"[MongoDBBackend] Load error: {e}")
            return None

    def exists(self, key: str) -> bool:
        return self.collection.count_documents({"_id": key}) > 0

    def delete(self, key: str) -> bool:
        try:
            self.collection.delete_one({"_id": key})
            return True
        except Exception as e:
            print(f"[MongoDBBackend] Delete error: {e}")
            return False

    def list_keys(self, pattern: str = "*") -> List[str]:
        try:
            import re
            regex_pattern = pattern.replace("*", ".*").replace("?", ".")
            docs = self.collection.find(
                {"_id": {"$regex": f"^{regex_pattern}$"}},
                {"_id": 1}
            )
            return [doc["_id"] for doc in docs]
        except Exception as e:
            print(f"[MongoDBBackend] List keys error: {e}")
            return []


class PostgreSQLBackend(StorageBackend):
    """
    PostgreSQL relational database with JSONB support.

    Pros: ACID transactions, complex queries, mature ecosystem
    Cons: More setup, slightly slower than Redis

    Best for: Enterprise deployments requiring strong consistency

    Installation: pip install psycopg2-binary
    """

    def __init__(self, host: str = 'localhost', port: int = 5432,
                 database: str = 'agent_ledger', user: str = 'postgres',
                 password: Optional[str] = None, table: str = 'ledgers'):
        try:
            import psycopg2
            from psycopg2.extras import Json

            self.conn = psycopg2.connect(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password
            )
            self.table = table
            self.Json = Json

            self._create_table()
            print(f"[PostgreSQLBackend] Connected to PostgreSQL at {host}:{port}")
        except ImportError:
            raise ImportError("PostgreSQL backend requires 'psycopg2' package: pip install psycopg2-binary")
        except Exception as e:
            raise ConnectionError(f"Failed to connect to PostgreSQL: {e}")

    def _create_table(self):
        cursor = self.conn.cursor()
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                key VARCHAR(255) PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{self.table}_updated_at
            ON {self.table}(updated_at)
        """)
        self.conn.commit()
        cursor.close()

    def save(self, key: str, data: Dict[str, Any]) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"""
                INSERT INTO {self.table} (key, data, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE
                SET data = EXCLUDED.data, updated_at = CURRENT_TIMESTAMP
            """, (key, self.Json(data)))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[PostgreSQLBackend] Save error: {e}")
            self.conn.rollback()
            return False

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"SELECT data FROM {self.table} WHERE key = %s", (key,))
            row = cursor.fetchone()
            cursor.close()
            if row is None:
                return None
            return row[0]
        except Exception as e:
            print(f"[PostgreSQLBackend] Load error: {e}")
            return None

    def exists(self, key: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT 1 FROM {self.table} WHERE key = %s", (key,))
        result = cursor.fetchone() is not None
        cursor.close()
        return result

    def delete(self, key: str) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"DELETE FROM {self.table} WHERE key = %s", (key,))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[PostgreSQLBackend] Delete error: {e}")
            self.conn.rollback()
            return False

    def list_keys(self, pattern: str = "*") -> List[str]:
        try:
            cursor = self.conn.cursor()
            sql_pattern = pattern.replace("*", "%").replace("?", "_")
            cursor.execute(f"SELECT key FROM {self.table} WHERE key LIKE %s", (sql_pattern,))
            keys = [row[0] for row in cursor.fetchall()]
            cursor.close()
            return keys
        except Exception as e:
            print(f"[PostgreSQLBackend] List keys error: {e}")
            return []


def benchmark_backends():
    """
    Benchmark different backends for comparison.

    Typical results:
    - JSONBackend: ~1-5ms per operation
    - RedisBackend: ~0.1-0.5ms per operation (10-50x faster!)
    - MongoDBBackend: ~1-3ms per operation
    - PostgreSQLBackend: ~0.5-2ms per operation
    """
    import time

    test_data = {
        "tasks": {f"task_{i}": {"status": "pending"} for i in range(100)},
        "metadata": {"agent_id": "test", "session_id": "benchmark"}
    }

    backends = {
        "InMemory": InMemoryBackend(),
        "JSON": JSONBackend(storage_dir=".benchmark_test"),
    }

    try:
        backends["Redis"] = RedisBackend()
    except:
        print("Redis not available for benchmark")

    try:
        backends["MongoDB"] = MongoDBBackend()
    except:
        print("MongoDB not available for benchmark")

    results = {}

    for name, backend in backends.items():
        iterations = 100

        # Benchmark writes
        start = time.time()
        for i in range(iterations):
            backend.save(f"benchmark_test_{i}", test_data)
        write_time = (time.time() - start) / iterations * 1000

        # Benchmark reads
        start = time.time()
        for i in range(iterations):
            backend.load(f"benchmark_test_{i}")
        read_time = (time.time() - start) / iterations * 1000

        # Cleanup
        for i in range(iterations):
            backend.delete(f"benchmark_test_{i}")

        results[name] = {
            "write_ms": round(write_time, 3),
            "read_ms": round(read_time, 3)
        }

    print("\n=== Backend Performance Benchmark ===")
    for name, metrics in results.items():
        print(f"{name:12} | Write: {metrics['write_ms']:6.3f}ms | Read: {metrics['read_ms']:6.3f}ms")

    return results


if __name__ == "__main__":
    print("Agent Ledger Storage Backends")
    print("=" * 60)
    benchmark_backends()
