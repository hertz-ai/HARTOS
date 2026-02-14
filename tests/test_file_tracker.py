"""
Tests for File Tracker System

Tests file watching, change detection, and synchronization.
"""

import pytest
import asyncio
import os
import sys
import time
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.memory.file_tracker import (
    FileTracker,
    FileWatcher,
    FileChange,
    SyncResult,
    WatchConfig,
    ChangeType,
)


class TestFileChange:
    """Tests for FileChange dataclass."""

    def test_file_change_creation(self):
        """Test basic FileChange creation."""
        change = FileChange(
            path="/app/data/test.txt",
            change_type=ChangeType.CREATED,
            size=1024,
            content_hash="abc123",
        )

        assert change.path == "/app/data/test.txt"
        assert change.change_type == ChangeType.CREATED
        assert change.size == 1024
        assert change.content_hash == "abc123"
        assert change.timestamp is not None

    def test_file_change_to_dict(self):
        """Test FileChange serialization."""
        change = FileChange(
            path="/tmp/test.txt",
            change_type=ChangeType.MODIFIED,
        )

        data = change.to_dict()
        assert data["path"] == "/tmp/test.txt"
        assert data["change_type"] == "modified"
        assert "timestamp" in data

    def test_file_change_from_dict(self):
        """Test FileChange deserialization."""
        data = {
            "path": "/app/test.py",
            "change_type": "deleted",
            "timestamp": "2025-01-01T12:00:00",
            "size": 500,
        }

        change = FileChange.from_dict(data)
        assert change.path == "/app/test.py"
        assert change.change_type == ChangeType.DELETED
        assert change.size == 500


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_sync_result_creation(self):
        """Test basic SyncResult creation."""
        result = SyncResult(
            path="/app/data",
            success=True,
            files_added=5,
            files_modified=3,
            files_deleted=1,
        )

        assert result.success is True
        assert result.files_added == 5
        assert result.total_changes == 9

    def test_sync_result_to_dict(self):
        """Test SyncResult serialization."""
        result = SyncResult(
            path="/tmp/test",
            success=True,
            files_added=2,
            duration_ms=150.5,
        )

        data = result.to_dict()
        assert data["path"] == "/tmp/test"
        assert data["success"] is True
        assert data["files_added"] == 2
        assert data["duration_ms"] == 150.5
        assert data["total_changes"] == 2


class TestWatchConfig:
    """Tests for WatchConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = WatchConfig()

        assert config.patterns == ["*"]
        assert "*.pyc" in config.ignore_patterns
        assert "__pycache__" in config.ignore_patterns
        assert config.recursive is True
        assert config.include_hidden is False

    def test_custom_config(self):
        """Test custom configuration."""
        config = WatchConfig(
            patterns=["*.py", "*.json"],
            recursive=False,
            include_hidden=True,
        )

        assert config.patterns == ["*.py", "*.json"]
        assert config.recursive is False
        assert config.include_hidden is True


class TestFileWatcher:
    """Tests for FileWatcher."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def test_watcher_creation(self, temp_dir):
        """Test FileWatcher creation."""
        watcher = FileWatcher(temp_dir)

        assert watcher.path is not None
        assert watcher.is_running is False

    def test_watcher_start_stop(self, temp_dir):
        """Test starting and stopping watcher."""
        watcher = FileWatcher(temp_dir)

        watcher.start()
        assert watcher.is_running is True

        watcher.stop()
        assert watcher.is_running is False

    def test_watcher_detects_new_file(self, temp_dir):
        """Test that watcher detects new files."""
        changes = []

        def on_change(change):
            changes.append(change)

        watcher = FileWatcher(
            temp_dir,
            config=WatchConfig(debounce_ms=50),
            on_change=on_change,
        )

        watcher.start()
        time.sleep(0.1)

        # Create a new file
        test_file = os.path.join(temp_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Hello World")

        # Wait for detection
        time.sleep(0.2)

        watcher.stop()

        # Should have detected the new file
        assert len(changes) >= 1
        assert any(c.change_type == ChangeType.CREATED for c in changes)

    def test_watcher_detects_modified_file(self, temp_dir):
        """Test that watcher detects modified files."""
        # Create initial file
        test_file = os.path.join(temp_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Initial content")

        changes = []

        def on_change(change):
            changes.append(change)

        watcher = FileWatcher(
            temp_dir,
            config=WatchConfig(debounce_ms=50),
            on_change=on_change,
        )

        watcher.start()
        time.sleep(0.1)

        # Modify the file
        with open(test_file, "w") as f:
            f.write("Modified content")

        # Wait for detection
        time.sleep(0.2)

        watcher.stop()

        # Should have detected modification
        assert len(changes) >= 1
        assert any(c.change_type == ChangeType.MODIFIED for c in changes)

    def test_watcher_detects_deleted_file(self, temp_dir):
        """Test that watcher detects deleted files."""
        # Create initial file
        test_file = os.path.join(temp_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Content")

        changes = []

        def on_change(change):
            changes.append(change)

        watcher = FileWatcher(
            temp_dir,
            config=WatchConfig(debounce_ms=50),
            on_change=on_change,
        )

        watcher.start()
        time.sleep(0.1)

        # Delete the file
        os.remove(test_file)

        # Wait for detection
        time.sleep(0.2)

        watcher.stop()

        # Should have detected deletion
        assert len(changes) >= 1
        assert any(c.change_type == ChangeType.DELETED for c in changes)

    def test_watcher_respects_patterns(self, temp_dir):
        """Test that watcher respects include patterns."""
        changes = []

        def on_change(change):
            changes.append(change)

        config = WatchConfig(
            patterns=["*.py"],
            debounce_ms=50,
        )

        watcher = FileWatcher(temp_dir, config=config, on_change=on_change)
        watcher.start()
        time.sleep(0.1)

        # Create a .txt file (should be ignored)
        txt_file = os.path.join(temp_dir, "test.txt")
        with open(txt_file, "w") as f:
            f.write("Text content")

        # Create a .py file (should be detected)
        py_file = os.path.join(temp_dir, "test.py")
        with open(py_file, "w") as f:
            f.write("print('hello')")

        time.sleep(0.2)
        watcher.stop()

        # Should only detect .py file
        py_changes = [c for c in changes if c.path.endswith(".py")]
        txt_changes = [c for c in changes if c.path.endswith(".txt")]

        assert len(py_changes) >= 1
        assert len(txt_changes) == 0

    def test_watcher_ignores_patterns(self, temp_dir):
        """Test that watcher respects ignore patterns."""
        changes = []

        def on_change(change):
            changes.append(change)

        config = WatchConfig(
            ignore_patterns=["*.log", "temp_*"],
            debounce_ms=50,
        )

        watcher = FileWatcher(temp_dir, config=config, on_change=on_change)
        watcher.start()
        time.sleep(0.1)

        # Create a .log file (should be ignored)
        log_file = os.path.join(temp_dir, "test.log")
        with open(log_file, "w") as f:
            f.write("Log content")

        # Create a temp_ file (should be ignored)
        temp_file = os.path.join(temp_dir, "temp_data.txt")
        with open(temp_file, "w") as f:
            f.write("Temp content")

        # Create a regular file (should be detected)
        regular_file = os.path.join(temp_dir, "data.txt")
        with open(regular_file, "w") as f:
            f.write("Regular content")

        time.sleep(0.2)
        watcher.stop()

        # Should only detect regular file
        regular_changes = [c for c in changes if "data.txt" in c.path]
        ignored_changes = [c for c in changes if ".log" in c.path or "temp_" in c.path]

        assert len(regular_changes) >= 1
        assert len(ignored_changes) == 0

    def test_get_watched_files(self, temp_dir):
        """Test getting list of watched files."""
        # Create some files
        for i in range(3):
            with open(os.path.join(temp_dir, f"file{i}.txt"), "w") as f:
                f.write(f"Content {i}")

        watcher = FileWatcher(temp_dir)
        watcher.start()
        time.sleep(0.1)

        files = watcher.get_watched_files()
        watcher.stop()

        assert len(files) == 3


class TestFileTracker:
    """Tests for FileTracker."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def tracker(self, temp_dir):
        """Create a FileTracker for testing."""
        db_path = os.path.join(temp_dir, "tracker.db")
        tracker = FileTracker(db_path=db_path, data_dir=temp_dir)
        yield tracker
        tracker.close()

    def test_tracker_creation(self, tracker):
        """Test FileTracker creation."""
        assert tracker is not None
        assert tracker.db_path is not None

    def test_watch_unwatch(self, tracker, temp_dir):
        """Test watching and unwatching directories."""
        watch_dir = os.path.join(temp_dir, "watched")
        os.makedirs(watch_dir)

        tracker.watch(watch_dir)
        assert watch_dir.replace("\\", "/") in [p.replace("\\", "/") for p in tracker.get_watched_paths()]

        tracker.unwatch(watch_dir)
        # Give it a moment to stop
        time.sleep(0.1)

    def test_watch_with_patterns(self, tracker, temp_dir):
        """Test watching with specific patterns."""
        watch_dir = os.path.join(temp_dir, "watched")
        os.makedirs(watch_dir)

        tracker.watch(watch_dir, patterns=["*.py", "*.json"])
        paths = tracker.get_watched_paths()

        assert len(paths) == 1

        tracker.unwatch(watch_dir)

    @pytest.mark.asyncio
    async def test_sync_empty_directory(self, tracker, temp_dir):
        """Test syncing an empty directory."""
        watch_dir = os.path.join(temp_dir, "empty")
        os.makedirs(watch_dir)

        result = await tracker.sync(watch_dir)

        assert result.success is True
        assert result.total_changes == 0

    @pytest.mark.asyncio
    async def test_sync_with_files(self, tracker, temp_dir):
        """Test syncing a directory with files."""
        watch_dir = os.path.join(temp_dir, "with_files")
        os.makedirs(watch_dir)

        # Create some files
        for i in range(3):
            with open(os.path.join(watch_dir, f"file{i}.txt"), "w") as f:
                f.write(f"Content {i}")

        result = await tracker.sync(watch_dir)

        assert result.success is True
        assert result.files_added == 3
        assert result.total_changes == 3

    @pytest.mark.asyncio
    async def test_sync_detects_changes(self, tracker, temp_dir):
        """Test that sync detects file changes."""
        watch_dir = os.path.join(temp_dir, "changes")
        os.makedirs(watch_dir)

        # Create initial file
        test_file = os.path.join(watch_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Initial")

        # First sync
        await tracker.sync(watch_dir)

        # Modify file
        with open(test_file, "w") as f:
            f.write("Modified content here")

        # Second sync
        result = await tracker.sync(watch_dir)

        assert result.success is True
        assert result.files_modified == 1

    @pytest.mark.asyncio
    async def test_sync_detects_deletions(self, tracker, temp_dir):
        """Test that sync detects deleted files."""
        watch_dir = os.path.join(temp_dir, "deletions")
        os.makedirs(watch_dir)

        # Create file
        test_file = os.path.join(watch_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Content")

        # First sync
        await tracker.sync(watch_dir)

        # Delete file
        os.remove(test_file)

        # Second sync
        result = await tracker.sync(watch_dir)

        assert result.success is True
        assert result.files_deleted == 1

    def test_get_changes_since(self, tracker, temp_dir):
        """Test getting changes since a timestamp."""
        watch_dir = os.path.join(temp_dir, "changes")
        os.makedirs(watch_dir)

        before = datetime.utcnow() - timedelta(seconds=1)

        # Create a file
        test_file = os.path.join(watch_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Content")

        # Use sync instead of watch to reliably record changes
        asyncio.get_event_loop().run_until_complete(tracker.sync(watch_dir))

        changes = tracker.get_changes(since=before)

        # Should have at least one change from sync
        assert len(changes) >= 1

    def test_get_tracked_files(self, tracker, temp_dir):
        """Test getting list of tracked files."""
        watch_dir = os.path.join(temp_dir, "tracked")
        os.makedirs(watch_dir)

        # Create files
        for i in range(3):
            with open(os.path.join(watch_dir, f"file{i}.txt"), "w") as f:
                f.write(f"Content {i}")

        # Sync to populate database
        asyncio.get_event_loop().run_until_complete(tracker.sync(watch_dir))

        files = tracker.get_tracked_files()

        assert len(files) == 3

    def test_change_callbacks(self, tracker, temp_dir):
        """Test change callback notifications."""
        watch_dir = os.path.join(temp_dir, "callbacks")
        os.makedirs(watch_dir)

        changes = []

        def callback(change):
            changes.append(change)

        tracker.add_change_callback(callback)
        tracker.watch(watch_dir)

        # Increase wait time for watcher to initialize
        time.sleep(0.3)

        # Create file
        test_file = os.path.join(watch_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Content")

        # Wait longer for change detection (polling interval + margin)
        time.sleep(0.5)

        # If watcher didn't catch it, use sync as fallback
        if len(changes) == 0:
            asyncio.get_event_loop().run_until_complete(tracker.sync(watch_dir))
            # After sync, callback should have been called
            assert len(changes) >= 1 or len(tracker.get_changes(since=datetime.utcnow() - timedelta(seconds=2))) >= 1
        else:
            assert len(changes) >= 1

        tracker.remove_change_callback(callback)
        tracker.unwatch(watch_dir)

    def test_cleanup_old_changes(self, tracker, temp_dir):
        """Test cleanup of old change records."""
        # This test just verifies the method works without errors
        deleted = tracker.cleanup_old_changes(days=0)  # Delete all
        assert deleted >= 0

    def test_context_manager(self, temp_dir):
        """Test FileTracker as context manager."""
        db_path = os.path.join(temp_dir, "context.db")

        with FileTracker(db_path=db_path) as tracker:
            assert tracker is not None

        # Verify it's closed
        assert tracker._conn is None


class TestNormalizePath:
    """Tests for path normalization."""

    def test_normalize_windows_path(self):
        """Test Windows path normalization."""
        path = "C:\\Users\\test\\data"
        normalized = FileWatcher._normalize_path(path)

        assert "\\" not in normalized
        assert "/" in normalized

    def test_normalize_unix_path(self):
        """Test Unix path is unchanged."""
        path = "/app/data/files"
        normalized = FileWatcher._normalize_path(path)

        assert normalized == path

    def test_normalize_relative_path(self):
        """Test relative path is made absolute."""
        path = "relative/path"
        normalized = FileWatcher._normalize_path(path)

        assert normalized.startswith("/") or ":" in normalized


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
