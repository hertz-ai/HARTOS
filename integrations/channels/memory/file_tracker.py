"""
File Tracker - Monitor and index file changes.

Provides file watching, change detection, and synchronization capabilities
for the memory system. Designed for Docker environments with container-compatible paths.
"""

import asyncio
import fnmatch
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union


class ChangeType(Enum):
    """Types of file changes."""
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass
class FileChange:
    """Represents a single file change event."""

    path: str
    change_type: ChangeType
    timestamp: datetime = field(default_factory=datetime.utcnow)
    old_path: Optional[str] = None  # For renames
    size: int = 0
    content_hash: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "path": self.path,
            "change_type": self.change_type.value,
            "timestamp": self.timestamp.isoformat(),
            "old_path": self.old_path,
            "size": self.size,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileChange":
        """Create from dictionary representation."""
        return cls(
            path=data["path"],
            change_type=ChangeType(data["change_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]) if isinstance(data["timestamp"], str) else data["timestamp"],
            old_path=data.get("old_path"),
            size=data.get("size", 0),
            content_hash=data.get("content_hash", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class SyncResult:
    """Result of a file synchronization operation."""

    path: str
    success: bool
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    files_unchanged: int = 0
    errors: List[str] = field(default_factory=list)
    duration_ms: float = 0.0
    changes: List[FileChange] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        """Total number of changes detected."""
        return self.files_added + self.files_modified + self.files_deleted

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "path": self.path,
            "success": self.success,
            "files_added": self.files_added,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "files_unchanged": self.files_unchanged,
            "total_changes": self.total_changes,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class WatchConfig:
    """Configuration for file watching."""

    patterns: List[str] = field(default_factory=lambda: ["*"])
    ignore_patterns: List[str] = field(default_factory=lambda: [
        "*.pyc", "__pycache__", ".git", ".svn", "*.swp", "*.tmp",
        "node_modules", ".venv", "venv", "*.log"
    ])
    recursive: bool = True
    include_hidden: bool = False
    max_file_size: int = 10 * 1024 * 1024  # 10MB default
    debounce_ms: int = 500
    compute_hash: bool = True


class FileWatcher:
    """
    Watches a directory for file changes.

    Provides real-time monitoring with debouncing and pattern filtering.
    Uses polling for Docker compatibility (inotify doesn't work across mounts).
    """

    def __init__(
        self,
        path: str,
        config: Optional[WatchConfig] = None,
        on_change: Optional[Callable[[FileChange], None]] = None,
    ):
        """
        Initialize the file watcher.

        Args:
            path: Directory path to watch.
            config: Watch configuration.
            on_change: Callback for change events.
        """
        self.path = self._normalize_path(path)
        self.config = config or WatchConfig()
        self.on_change = on_change

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._file_states: Dict[str, Dict[str, Any]] = {}
        self._pending_changes: List[FileChange] = []
        self._last_debounce: float = 0

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path for container compatibility."""
        # Convert Windows paths to Unix-style for Docker
        normalized = path.replace("\\", "/")
        # Handle common Docker mount points
        if not normalized.startswith("/"):
            # Relative path - make absolute
            normalized = os.path.abspath(path).replace("\\", "/")
        return normalized

    def start(self) -> None:
        """Start watching for file changes."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._scan_initial()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop watching for file changes."""
        with self._lock:
            self._running = False
            if self._thread:
                self._thread.join(timeout=2.0)
                self._thread = None

    def _scan_initial(self) -> None:
        """Perform initial scan of the directory."""
        self._file_states.clear()
        for file_path in self._iter_files():
            try:
                stat = os.stat(file_path)
                self._file_states[file_path] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "hash": self._compute_hash(file_path) if self.config.compute_hash else "",
                }
            except OSError:
                pass

    def _poll_loop(self) -> None:
        """Main polling loop for detecting changes."""
        poll_interval = max(0.1, self.config.debounce_ms / 1000.0)

        while self._running:
            try:
                self._check_for_changes()
            except Exception:
                pass  # Ignore polling errors
            time.sleep(poll_interval)

    def _check_for_changes(self) -> None:
        """Check for file changes and emit events."""
        current_files: Set[str] = set()
        changes: List[FileChange] = []

        for file_path in self._iter_files():
            current_files.add(file_path)
            try:
                stat = os.stat(file_path)
                current_state = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }

                if file_path not in self._file_states:
                    # New file
                    content_hash = self._compute_hash(file_path) if self.config.compute_hash else ""
                    current_state["hash"] = content_hash
                    self._file_states[file_path] = current_state
                    changes.append(FileChange(
                        path=file_path,
                        change_type=ChangeType.CREATED,
                        size=stat.st_size,
                        content_hash=content_hash,
                    ))
                elif (current_state["mtime"] != self._file_states[file_path]["mtime"] or
                      current_state["size"] != self._file_states[file_path]["size"]):
                    # Modified file
                    content_hash = self._compute_hash(file_path) if self.config.compute_hash else ""
                    current_state["hash"] = content_hash

                    # Only report change if hash differs (handles save without change)
                    if not self.config.compute_hash or content_hash != self._file_states[file_path].get("hash", ""):
                        self._file_states[file_path] = current_state
                        changes.append(FileChange(
                            path=file_path,
                            change_type=ChangeType.MODIFIED,
                            size=stat.st_size,
                            content_hash=content_hash,
                        ))
                    else:
                        self._file_states[file_path] = current_state

            except OSError:
                pass

        # Check for deleted files
        deleted = set(self._file_states.keys()) - current_files
        for file_path in deleted:
            del self._file_states[file_path]
            changes.append(FileChange(
                path=file_path,
                change_type=ChangeType.DELETED,
            ))

        # Emit changes
        if changes and self.on_change:
            for change in changes:
                try:
                    self.on_change(change)
                except Exception:
                    pass

    def _iter_files(self):
        """Iterate over files matching the watch patterns."""
        base_path = Path(self.path)
        if not base_path.exists():
            return

        if self.config.recursive:
            walker = base_path.rglob("*")
        else:
            walker = base_path.glob("*")

        for path in walker:
            if not path.is_file():
                continue

            str_path = str(path)

            # Check ignore patterns
            if any(fnmatch.fnmatch(path.name, pat) or fnmatch.fnmatch(str_path, pat)
                   for pat in self.config.ignore_patterns):
                continue

            # Check hidden files
            if not self.config.include_hidden and path.name.startswith("."):
                continue

            # Check include patterns
            if self.config.patterns != ["*"]:
                if not any(fnmatch.fnmatch(path.name, pat) for pat in self.config.patterns):
                    continue

            # Check file size
            try:
                if path.stat().st_size > self.config.max_file_size:
                    continue
            except OSError:
                continue

            yield str_path

    def _compute_hash(self, file_path: str) -> str:
        """Compute SHA256 hash of file content."""
        try:
            hasher = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except OSError:
            return ""

    @property
    def is_running(self) -> bool:
        """Check if watcher is running."""
        return self._running

    def get_watched_files(self) -> List[str]:
        """Get list of currently watched files."""
        with self._lock:
            return list(self._file_states.keys())


class FileTracker:
    """
    Monitor and index file changes.

    Provides persistent tracking of file changes with SQLite storage,
    designed for Docker environments with proper file locking.

    Features:
    - Watch multiple directories with configurable patterns
    - Persistent change history with SQLite FTS5
    - Async sync operations
    - Change detection since arbitrary timestamps
    """

    SCHEMA_VERSION = 1

    # Default paths for Docker environments
    DEFAULT_DATA_DIR = "/app/data"
    DEFAULT_TEMP_DIR = "/tmp/file_tracker"

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        data_dir: Optional[str] = None,
    ):
        """
        Initialize the file tracker.

        Args:
            db_path: Path to SQLite database. Uses temp directory if None.
            data_dir: Base data directory for relative paths.
        """
        # Determine data directory
        if data_dir:
            self.data_dir = data_dir
        elif os.path.exists(self.DEFAULT_DATA_DIR):
            self.data_dir = self.DEFAULT_DATA_DIR
        else:
            self.data_dir = os.path.abspath(".")

        # Determine database path
        if db_path:
            self.db_path = str(db_path)
        else:
            db_dir = self.DEFAULT_TEMP_DIR if os.path.exists("/tmp") else os.path.join(self.data_dir, ".tracker")
            os.makedirs(db_dir, exist_ok=True)
            self.db_path = os.path.join(db_dir, "file_tracker.db")

        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._watchers: Dict[str, FileWatcher] = {}
        self._change_callbacks: List[Callable[[FileChange], None]] = []

        self._ensure_connection()
        self._ensure_schema()

    def _ensure_connection(self) -> sqlite3.Connection:
        """Ensure database connection with proper locking."""
        if self._conn is None:
            # Create parent directory if needed
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,  # Auto-commit mode
                timeout=30.0,  # Wait up to 30s for lock
            )
            self._conn.row_factory = sqlite3.Row

            # Enable WAL mode for better concurrency
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")

        return self._conn

    def _ensure_schema(self) -> None:
        """Create database schema if not exists."""
        conn = self._ensure_connection()
        with self._lock:
            # File index table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracked_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    watch_path TEXT NOT NULL,
                    size INTEGER DEFAULT 0,
                    content_hash TEXT,
                    first_seen REAL,
                    last_modified REAL,
                    last_synced REAL,
                    metadata TEXT DEFAULT '{}'
                )
            """)

            # Change history table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    old_path TEXT,
                    size INTEGER DEFAULT 0,
                    content_hash TEXT,
                    metadata TEXT DEFAULT '{}'
                )
            """)

            # Watch configurations table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_configs (
                    path TEXT PRIMARY KEY,
                    config TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at REAL,
                    updated_at REAL
                )
            """)

            # Create FTS5 table for content search (if available)
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS file_content_fts USING fts5(
                        path,
                        content,
                        content=tracked_files
                    )
                """)
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_path ON file_changes(path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_timestamp ON file_changes(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_watch ON tracked_files(watch_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON tracked_files(content_hash)")

            # Schema version
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("version", str(self.SCHEMA_VERSION))
            )

    def watch(self, path: str, patterns: Optional[List[str]] = None) -> None:
        """
        Start watching a directory for changes.

        Args:
            path: Directory path to watch.
            patterns: Optional list of glob patterns to match (e.g., ["*.py", "*.json"]).
        """
        normalized_path = FileWatcher._normalize_path(path)

        with self._lock:
            if normalized_path in self._watchers:
                return  # Already watching

            config = WatchConfig(patterns=patterns or ["*"])

            # Save config to database
            conn = self._ensure_connection()
            now = time.time()
            conn.execute(
                """
                INSERT OR REPLACE INTO watch_configs (path, config, enabled, created_at, updated_at)
                VALUES (?, ?, 1, COALESCE((SELECT created_at FROM watch_configs WHERE path = ?), ?), ?)
                """,
                (normalized_path, json.dumps(config.__dict__), normalized_path, now, now)
            )

            # Create and start watcher
            watcher = FileWatcher(
                path=normalized_path,
                config=config,
                on_change=self._on_file_change,
            )
            self._watchers[normalized_path] = watcher
            watcher.start()

    def unwatch(self, path: str) -> None:
        """
        Stop watching a directory.

        Args:
            path: Directory path to stop watching.
        """
        normalized_path = FileWatcher._normalize_path(path)

        with self._lock:
            if normalized_path in self._watchers:
                self._watchers[normalized_path].stop()
                del self._watchers[normalized_path]

            # Mark as disabled in database
            conn = self._ensure_connection()
            conn.execute(
                "UPDATE watch_configs SET enabled = 0, updated_at = ? WHERE path = ?",
                (time.time(), normalized_path)
            )

    async def sync(self, path: str) -> SyncResult:
        """
        Synchronize file index with filesystem.

        Performs a full scan and updates the database with current state.

        Args:
            path: Directory path to sync.

        Returns:
            SyncResult with statistics and changes.
        """
        start_time = time.time()
        normalized_path = FileWatcher._normalize_path(path)

        result = SyncResult(path=normalized_path, success=False)

        try:
            # Get current state from database
            conn = self._ensure_connection()
            with self._lock:
                rows = conn.execute(
                    "SELECT path, content_hash, size FROM tracked_files WHERE watch_path = ?",
                    (normalized_path,)
                ).fetchall()

            db_files: Dict[str, Dict[str, Any]] = {
                row["path"]: {"hash": row["content_hash"], "size": row["size"]}
                for row in rows
            }

            # Scan filesystem
            config = WatchConfig()
            watcher = FileWatcher(path=normalized_path, config=config)
            current_files: Set[str] = set()

            for file_path in watcher._iter_files():
                current_files.add(file_path)

                try:
                    stat = os.stat(file_path)
                    content_hash = watcher._compute_hash(file_path)

                    if file_path not in db_files:
                        # New file
                        result.files_added += 1
                        change = FileChange(
                            path=file_path,
                            change_type=ChangeType.CREATED,
                            size=stat.st_size,
                            content_hash=content_hash,
                        )
                        result.changes.append(change)
                        self._record_change(change)
                        self._update_file_record(file_path, normalized_path, stat.st_size, content_hash)

                    elif (content_hash != db_files[file_path]["hash"] or
                          stat.st_size != db_files[file_path]["size"]):
                        # Modified file
                        result.files_modified += 1
                        change = FileChange(
                            path=file_path,
                            change_type=ChangeType.MODIFIED,
                            size=stat.st_size,
                            content_hash=content_hash,
                        )
                        result.changes.append(change)
                        self._record_change(change)
                        self._update_file_record(file_path, normalized_path, stat.st_size, content_hash)
                    else:
                        result.files_unchanged += 1

                except OSError as e:
                    result.errors.append(f"Error reading {file_path}: {e}")

            # Check for deleted files
            deleted = set(db_files.keys()) - current_files
            for file_path in deleted:
                result.files_deleted += 1
                change = FileChange(path=file_path, change_type=ChangeType.DELETED)
                result.changes.append(change)
                self._record_change(change)
                self._delete_file_record(file_path)

            result.success = True

        except Exception as e:
            result.errors.append(f"Sync failed: {e}")

        result.duration_ms = (time.time() - start_time) * 1000
        return result

    def get_changes(self, since: datetime) -> List[FileChange]:
        """
        Get all file changes since a given timestamp.

        Args:
            since: Get changes after this datetime.

        Returns:
            List of FileChange objects.
        """
        timestamp = since.timestamp()

        conn = self._ensure_connection()
        with self._lock:
            rows = conn.execute(
                """
                SELECT path, change_type, timestamp, old_path, size, content_hash, metadata
                FROM file_changes
                WHERE timestamp > ?
                ORDER BY timestamp ASC
                """,
                (timestamp,)
            ).fetchall()

        return [
            FileChange(
                path=row["path"],
                change_type=ChangeType(row["change_type"]),
                timestamp=datetime.fromtimestamp(row["timestamp"]),
                old_path=row["old_path"],
                size=row["size"] or 0,
                content_hash=row["content_hash"] or "",
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            )
            for row in rows
        ]

    def get_tracked_files(self, watch_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get list of tracked files.

        Args:
            watch_path: Optional filter by watch path.

        Returns:
            List of file records.
        """
        conn = self._ensure_connection()
        with self._lock:
            if watch_path:
                normalized = FileWatcher._normalize_path(watch_path)
                rows = conn.execute(
                    "SELECT * FROM tracked_files WHERE watch_path = ?",
                    (normalized,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tracked_files").fetchall()

        return [dict(row) for row in rows]

    def get_watched_paths(self) -> List[str]:
        """Get list of currently watched paths."""
        with self._lock:
            return list(self._watchers.keys())

    def add_change_callback(self, callback: Callable[[FileChange], None]) -> None:
        """Add a callback for file change events."""
        self._change_callbacks.append(callback)

    def remove_change_callback(self, callback: Callable[[FileChange], None]) -> None:
        """Remove a change callback."""
        if callback in self._change_callbacks:
            self._change_callbacks.remove(callback)

    def _on_file_change(self, change: FileChange) -> None:
        """Handle file change event from watcher."""
        self._record_change(change)

        # Update file record
        if change.change_type == ChangeType.DELETED:
            self._delete_file_record(change.path)
        else:
            # Find which watch path this belongs to
            watch_path = None
            for wp in self._watchers.keys():
                if change.path.startswith(wp):
                    watch_path = wp
                    break
            if watch_path:
                self._update_file_record(change.path, watch_path, change.size, change.content_hash)

        # Notify callbacks
        for callback in self._change_callbacks:
            try:
                callback(change)
            except Exception:
                pass

    def _record_change(self, change: FileChange) -> None:
        """Record a change to the database."""
        conn = self._ensure_connection()
        with self._lock:
            conn.execute(
                """
                INSERT INTO file_changes (path, change_type, timestamp, old_path, size, content_hash, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change.path,
                    change.change_type.value,
                    change.timestamp.timestamp(),
                    change.old_path,
                    change.size,
                    change.content_hash,
                    json.dumps(change.metadata),
                )
            )

    def _update_file_record(self, path: str, watch_path: str, size: int, content_hash: str) -> None:
        """Update or insert a file record."""
        conn = self._ensure_connection()
        now = time.time()
        with self._lock:
            conn.execute(
                """
                INSERT OR REPLACE INTO tracked_files
                (path, watch_path, size, content_hash, first_seen, last_modified, last_synced)
                VALUES (?, ?, ?, ?,
                    COALESCE((SELECT first_seen FROM tracked_files WHERE path = ?), ?),
                    ?, ?)
                """,
                (path, watch_path, size, content_hash, path, now, now, now)
            )

    def _delete_file_record(self, path: str) -> None:
        """Delete a file record."""
        conn = self._ensure_connection()
        with self._lock:
            conn.execute("DELETE FROM tracked_files WHERE path = ?", (path,))

    def cleanup_old_changes(self, days: int = 30) -> int:
        """
        Remove change records older than specified days.

        Args:
            days: Maximum age of records to keep.

        Returns:
            Number of records deleted.
        """
        cutoff = time.time() - (days * 24 * 3600)
        conn = self._ensure_connection()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM file_changes WHERE timestamp < ?",
                (cutoff,)
            )
            return cursor.rowcount

    def close(self) -> None:
        """Stop all watchers and close database connection."""
        with self._lock:
            for watcher in self._watchers.values():
                watcher.stop()
            self._watchers.clear()

            if self._conn:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
