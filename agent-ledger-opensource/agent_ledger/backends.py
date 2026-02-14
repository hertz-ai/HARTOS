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
        self.storage_dir.mkdir(exist_ok=True)

    def _get_path(self, key: str):
        return self.storage_dir / f"{key}.json"

    def save(self, key: str, data: Dict[str, Any]) -> bool:
        try:
            path = self._get_path(key)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            print(f"[JSONBackend] Save error: {e}")
            return False

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            path = self._get_path(key)
            if not path.exists():
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[JSONBackend] Load error: {e}")
            return None

    def exists(self, key: str) -> bool:
        return self._get_path(key).exists()

    def delete(self, key: str) -> bool:
        try:
            path = self._get_path(key)
            if path.exists():
                path.unlink()
            return True
        except Exception as e:
            print(f"[JSONBackend] Delete error: {e}")
            return False

    def list_keys(self, pattern: str = "*") -> List[str]:
        import fnmatch
        files = [f.stem for f in self.storage_dir.glob("*.json")]
        return [f for f in files if fnmatch.fnmatch(f, pattern)]


class RedisBackend(StorageBackend):
    """
    Redis in-memory storage (FAST!).

    Pros: Extremely fast (in-memory), great for production, handles concurrency
    Cons: Requires Redis server, data not persistent by default

    Performance: ~10-100x faster than JSON for reads/writes

    Installation: pip install redis
    """

    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 0,
                 password: Optional[str] = None, prefix: str = "agent_ledger:"):
        try:
            import redis
            self.redis_client = redis.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                decode_responses=True
            )
            self.prefix = prefix
            # Test connection
            self.redis_client.ping()
            print(f"[RedisBackend] Connected to Redis at {host}:{port}")
        except ImportError:
            raise ImportError("Redis backend requires 'redis' package: pip install redis")
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Redis: {e}")

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
        keys = self.redis_client.keys(redis_pattern)
        return [k.replace(self.prefix, '') for k in keys]


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
