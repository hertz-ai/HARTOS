"""
Tests for File Management System.

Tests the FileManager class and related functionality.
"""

import pytest
import asyncio
import os
import sys
import time
from unittest.mock import Mock, patch, AsyncMock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.media.files import (
    FileStatus,
    StorageBackend,
    FileManager,
    FileInfo,
    DownloadResult,
    UploadResult,
)


class TestFileStatus:
    """Tests for FileStatus enum."""

    def test_all_statuses_defined(self):
        """Test all expected statuses are defined."""
        assert FileStatus.PENDING.value == "pending"
        assert FileStatus.DOWNLOADING.value == "downloading"
        assert FileStatus.UPLOADING.value == "uploading"
        assert FileStatus.COMPLETED.value == "completed"
        assert FileStatus.FAILED.value == "failed"
        assert FileStatus.EXPIRED.value == "expired"


class TestStorageBackend:
    """Tests for StorageBackend enum."""

    def test_all_backends_defined(self):
        """Test all storage backends are defined."""
        assert StorageBackend.LOCAL.value == "local"
        assert StorageBackend.S3.value == "s3"
        assert StorageBackend.GCS.value == "gcs"
        assert StorageBackend.AZURE.value == "azure"


class TestFileInfo:
    """Tests for FileInfo dataclass."""

    def test_file_info_creation(self):
        """Test creating FileInfo."""
        info = FileInfo(
            file_id="test-123",
            filename="test.pdf",
            size=1024,
            mime_type="application/pdf"
        )

        assert info.file_id == "test-123"
        assert info.filename == "test.pdf"
        assert info.size == 1024
        assert info.mime_type == "application/pdf"

    def test_file_info_to_dict(self):
        """Test FileInfo serialization."""
        info = FileInfo(
            file_id="file-1",
            filename="document.txt",
            size=500,
            mime_type="text/plain",
            url="https://example.com/file"
        )

        data = info.to_dict()
        assert data["file_id"] == "file-1"
        assert data["filename"] == "document.txt"
        assert data["size"] == 500
        assert data["url"] == "https://example.com/file"

    def test_file_info_defaults(self):
        """Test FileInfo default values."""
        info = FileInfo(
            file_id="f1",
            filename="test.txt",
            size=100,
            mime_type="text/plain"
        )

        assert info.url is None
        assert info.local_path is None
        assert info.channel is None
        assert info.expires_at is None
        assert info.checksum is None
        assert info.metadata == {}

    def test_file_info_is_expired(self):
        """Test expiration checking."""
        # Not expired (no expiry set)
        info = FileInfo(
            file_id="f1",
            filename="test.txt",
            size=100,
            mime_type="text/plain"
        )
        assert not info.is_expired()

        # Not expired (future expiry)
        info_future = FileInfo(
            file_id="f2",
            filename="test.txt",
            size=100,
            mime_type="text/plain",
            expires_at=time.time() + 3600
        )
        assert not info_future.is_expired()

        # Expired
        info_expired = FileInfo(
            file_id="f3",
            filename="test.txt",
            size=100,
            mime_type="text/plain",
            expires_at=time.time() - 100
        )
        assert info_expired.is_expired()

    def test_file_info_get_extension(self):
        """Test getting file extension."""
        info = FileInfo(
            file_id="f1",
            filename="document.pdf",
            size=100,
            mime_type="application/pdf"
        )
        assert info.get_extension() == "pdf"

        info_no_ext = FileInfo(
            file_id="f2",
            filename="README",
            size=100,
            mime_type="text/plain"
        )
        assert info_no_ext.get_extension() == ""


class TestDownloadResult:
    """Tests for DownloadResult dataclass."""

    def test_download_result_success(self):
        """Test successful download result."""
        result = DownloadResult(
            success=True,
            file_path="/tmp/file.pdf",
            download_time=1.5
        )

        assert result.success is True
        assert result.file_path == "/tmp/file.pdf"
        assert result.download_time == 1.5
        assert result.error is None

    def test_download_result_failure(self):
        """Test failed download result."""
        result = DownloadResult(
            success=False,
            error="Connection timeout"
        )

        assert result.success is False
        assert result.error == "Connection timeout"

    def test_download_result_to_dict(self):
        """Test DownloadResult serialization."""
        result = DownloadResult(
            success=True,
            file_path="/tmp/test.txt",
            download_time=2.0
        )

        data = result.to_dict()
        assert data["success"] is True
        assert data["file_path"] == "/tmp/test.txt"
        assert data["download_time"] == 2.0


class TestUploadResult:
    """Tests for UploadResult dataclass."""

    def test_upload_result_success(self):
        """Test successful upload result."""
        result = UploadResult(
            success=True,
            url="https://storage.example.com/file.pdf",
            file_id="file-123",
            upload_time=3.0
        )

        assert result.success is True
        assert result.url == "https://storage.example.com/file.pdf"
        assert result.file_id == "file-123"

    def test_upload_result_to_dict(self):
        """Test UploadResult serialization."""
        result = UploadResult(
            success=True,
            url="https://example.com/file",
            file_id="abc123"
        )

        data = result.to_dict()
        assert data["success"] is True
        assert data["url"] == "https://example.com/file"
        assert data["file_id"] == "abc123"


class TestFileManager:
    """Tests for FileManager class."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create file manager for testing."""
        return FileManager(
            storage_backend=StorageBackend.LOCAL,
            temp_dir=str(tmp_path / "temp"),
            upload_dir=str(tmp_path / "uploads")
        )

    @pytest.fixture
    def manager_s3(self, tmp_path):
        """Create S3 file manager."""
        return FileManager(
            storage_backend=StorageBackend.S3,
            config={"s3_bucket": "test-bucket"}
        )

    def test_manager_initialization(self, manager):
        """Test manager initialization."""
        assert manager.storage_backend == StorageBackend.LOCAL
        assert Path(manager.temp_dir).exists()
        assert Path(manager.upload_dir).exists()

    def test_manager_initialization_from_string(self, tmp_path):
        """Test manager initialization from string."""
        mgr = FileManager(
            storage_backend="local",
            temp_dir=str(tmp_path / "temp"),
            upload_dir=str(tmp_path / "uploads")
        )
        assert mgr.storage_backend == StorageBackend.LOCAL

    def test_channel_max_sizes(self, manager):
        """Test channel max size constants."""
        sizes = manager.CHANNEL_MAX_SIZES
        assert sizes["telegram"] == 50 * 1024 * 1024  # 50MB
        assert sizes["discord"] == 8 * 1024 * 1024   # 8MB
        assert "default" in sizes

    def test_channel_allowed_extensions(self, manager):
        """Test channel allowed extensions."""
        extensions = manager.CHANNEL_ALLOWED_EXTENSIONS
        assert "jpg" in extensions["telegram"]
        assert "pdf" in extensions["slack"]
        assert "default" in extensions

    def test_get_mime_type(self, manager):
        """Test MIME type detection."""
        assert manager._get_mime_type("test.pdf") == "application/pdf"
        assert manager._get_mime_type("image.jpg") == "image/jpeg"
        assert manager._get_mime_type("document.txt") == "text/plain"
        assert manager._get_mime_type("unknown") == "application/octet-stream"

    def test_get_filename_from_url(self, manager):
        """Test extracting filename from URL."""
        assert manager._get_filename_from_url(
            "https://example.com/files/doc.pdf"
        ) == "doc.pdf"

        assert manager._get_filename_from_url(
            "https://example.com/path/to/image.jpg?token=abc"
        ) == "image.jpg"

        # URL with encoded characters
        result = manager._get_filename_from_url(
            "https://example.com/my%20file.txt"
        )
        assert "file.txt" in result

    def test_generate_file_id(self, manager):
        """Test file ID generation."""
        id1 = manager._generate_file_id(filename="test.txt")
        id2 = manager._generate_file_id(filename="test.txt")

        # Should be unique
        assert id1 != id2
        assert len(id1) == 16

    def test_validate_for_channel(self, manager, tmp_path):
        """Test file validation for channels."""
        # Valid file
        manager._validate_for_channel("test.jpg", 1024, "telegram")

        # File too large
        with pytest.raises(ValueError, match="too large"):
            manager._validate_for_channel(
                "big.pdf",
                100 * 1024 * 1024,  # 100MB
                "telegram"
            )

        # Invalid extension
        with pytest.raises(ValueError, match="not allowed"):
            manager._validate_for_channel("test.exe", 1024, "telegram")

    def test_format_size(self, manager):
        """Test size formatting."""
        assert manager.format_size(512) == "512.0 B"
        assert manager.format_size(1024) == "1.0 KB"
        assert manager.format_size(1024 * 1024) == "1.0 MB"
        assert manager.format_size(1024 * 1024 * 1024) == "1.0 GB"

    def test_get_channel_limits(self, manager):
        """Test getting channel limits."""
        limits = manager.get_channel_limits("telegram")

        assert limits["max_size"] == 50 * 1024 * 1024
        assert "50" in limits["max_size_formatted"]
        assert "jpg" in limits["allowed_extensions"]

    def test_get_temp_path(self, manager):
        """Test temporary path generation."""
        path1 = manager.get_temp_path()
        path2 = manager.get_temp_path()

        # Should be unique
        assert path1 != path2
        assert manager.temp_dir in path1

    def test_get_temp_path_with_extension(self, manager):
        """Test temp path with extension."""
        path = manager.get_temp_path(prefix="doc", extension="pdf")
        assert path.endswith(".pdf")
        assert "doc_" in path

    def test_get_storage_info(self, manager):
        """Test getting storage information."""
        info = manager.get_storage_info()

        assert info["backend"] == "local"
        assert info["temp_dir"] == manager.temp_dir
        assert info["upload_dir"] == manager.upload_dir
        assert "tracked_files" in info
        assert "supported_channels" in info

    @pytest.mark.asyncio
    async def test_download_basic(self, manager):
        """Test basic file download."""
        url = "https://example.com/test.pdf"
        result = await manager.download(url)

        assert result is not None
        assert Path(result).exists()

    @pytest.mark.asyncio
    async def test_download_with_destination(self, manager, tmp_path):
        """Test download with custom destination."""
        url = "https://example.com/file.txt"
        dest = str(tmp_path / "custom" / "file.txt")

        result = await manager.download(url, destination=dest)

        assert result == dest
        assert Path(result).parent.exists()

    @pytest.mark.asyncio
    async def test_upload_basic(self, manager, tmp_path):
        """Test basic file upload."""
        # Create test file
        test_file = tmp_path / "upload_test.txt"
        test_file.write_text("Test content")

        url = await manager.upload(
            str(test_file),
            channel="telegram"
        )

        assert url is not None
        assert "/files/" in url

    @pytest.mark.asyncio
    async def test_upload_validates_channel(self, manager, tmp_path):
        """Test upload validates file for channel."""
        # Create large file (simulated)
        large_file = tmp_path / "large.pdf"
        large_file.write_bytes(b"x" * (100 * 1024 * 1024))  # 100MB

        with pytest.raises(ValueError, match="too large"):
            await manager.upload(str(large_file), channel="discord")

    @pytest.mark.asyncio
    async def test_upload_validates_extension(self, manager, tmp_path):
        """Test upload validates file extension."""
        exe_file = tmp_path / "program.exe"
        exe_file.write_bytes(b"MZ")  # PE header start

        with pytest.raises(ValueError, match="not allowed"):
            await manager.upload(str(exe_file), channel="telegram")

    @pytest.mark.asyncio
    async def test_get_info(self, manager, tmp_path):
        """Test getting file info."""
        # Upload a file first
        test_file = tmp_path / "info_test.txt"
        test_file.write_text("Content")

        await manager.upload(str(test_file), channel="telegram")

        # Get files
        files = manager.list_files()
        if files:
            info = await manager.get_info(files[0].file_id)
            assert info.filename == "info_test.txt"

    @pytest.mark.asyncio
    async def test_get_info_from_path(self, manager, tmp_path):
        """Test getting file info from path."""
        test_file = tmp_path / "local_file.pdf"
        test_file.write_bytes(b"PDF content")

        info = await manager.get_info_from_path(str(test_file))

        assert info.filename == "local_file.pdf"
        assert info.size == 11  # len(b"PDF content")
        assert info.mime_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_delete(self, manager, tmp_path):
        """Test file deletion."""
        # Upload file
        test_file = tmp_path / "delete_test.txt"
        test_file.write_text("To delete")

        await manager.upload(str(test_file), channel="telegram")

        files = manager.list_files()
        if files:
            file_id = files[0].file_id
            result = await manager.delete(file_id)
            assert result is True

            # Should be gone
            assert file_id not in [f.file_id for f in manager.list_files()]

    @pytest.mark.asyncio
    async def test_copy(self, manager, tmp_path):
        """Test file copying."""
        source = tmp_path / "source.txt"
        source.write_text("Source content")

        dest = tmp_path / "dest" / "copy.txt"

        result = await manager.copy(str(source), str(dest))

        assert result == str(dest)
        assert Path(dest).exists()
        assert Path(dest).read_text() == "Source content"

    @pytest.mark.asyncio
    async def test_move(self, manager, tmp_path):
        """Test file moving."""
        source = tmp_path / "to_move.txt"
        source.write_text("Move me")

        dest = tmp_path / "moved" / "moved.txt"

        result = await manager.move(str(source), str(dest))

        assert result == str(dest)
        assert Path(dest).exists()
        assert not Path(source).exists()

    def test_cleanup_temp(self, manager, tmp_path):
        """Test temporary file cleanup."""
        # Create some old temp files
        temp_dir = Path(manager.temp_dir)
        old_file = temp_dir / "old_file.txt"
        old_file.write_text("Old")

        # Set modification time to past
        old_time = time.time() - (25 * 3600)  # 25 hours ago
        os.utime(old_file, (old_time, old_time))

        # Create recent file
        new_file = temp_dir / "new_file.txt"
        new_file.write_text("New")

        deleted = manager.cleanup_temp(max_age_hours=24)

        assert not old_file.exists()
        assert new_file.exists()
        assert deleted >= 1

    def test_list_files(self, manager):
        """Test listing tracked files."""
        files = manager.list_files()
        assert isinstance(files, list)

    def test_list_files_with_channel_filter(self, manager):
        """Test listing files with channel filter."""
        files = manager.list_files(channel="telegram")
        assert isinstance(files, list)
        for f in files:
            assert f.channel == "telegram" or f.channel is None


class TestFileManagerBackends:
    """Tests for different storage backends."""

    def test_local_backend(self, tmp_path):
        """Test local storage backend."""
        mgr = FileManager(
            storage_backend=StorageBackend.LOCAL,
            temp_dir=str(tmp_path / "temp"),
            upload_dir=str(tmp_path / "uploads")
        )
        assert mgr.storage_backend == StorageBackend.LOCAL

    def test_s3_backend(self, tmp_path):
        """Test S3 storage backend initialization."""
        mgr = FileManager(
            storage_backend=StorageBackend.S3,
            temp_dir=str(tmp_path / "temp"),
            config={"s3_bucket": "my-bucket"}
        )
        assert mgr.storage_backend == StorageBackend.S3

    def test_gcs_backend(self, tmp_path):
        """Test GCS storage backend initialization."""
        mgr = FileManager(
            storage_backend=StorageBackend.GCS,
            temp_dir=str(tmp_path / "temp")
        )
        assert mgr.storage_backend == StorageBackend.GCS

    def test_azure_backend(self, tmp_path):
        """Test Azure storage backend initialization."""
        mgr = FileManager(
            storage_backend=StorageBackend.AZURE,
            temp_dir=str(tmp_path / "temp")
        )
        assert mgr.storage_backend == StorageBackend.AZURE


class TestFileManagerIntegration:
    """Integration tests for file management system."""

    @pytest.mark.asyncio
    async def test_full_upload_download_workflow(self, tmp_path):
        """Test complete upload and download workflow."""
        mgr = FileManager(
            storage_backend=StorageBackend.LOCAL,
            temp_dir=str(tmp_path / "temp"),
            upload_dir=str(tmp_path / "uploads")
        )

        # Create test file
        original = tmp_path / "original.txt"
        original.write_text("Hello, World!")

        # Upload
        url = await mgr.upload(str(original), channel="telegram")
        assert url is not None

        # Get file info
        files = mgr.list_files()
        assert len(files) >= 1

        file_info = files[0]
        assert file_info.filename == "original.txt"
        assert file_info.channel == "telegram"

        # Download (would download from URL in real scenario)
        # Here we simulate by copying the file
        downloaded = await mgr.download(
            f"file://{original}",
            destination=str(tmp_path / "downloaded.txt")
        )

        assert Path(downloaded).exists()

    @pytest.mark.asyncio
    async def test_channel_specific_validation(self, tmp_path):
        """Test channel-specific file validation."""
        mgr = FileManager(
            storage_backend=StorageBackend.LOCAL,
            temp_dir=str(tmp_path / "temp"),
            upload_dir=str(tmp_path / "uploads")
        )

        # Create test file
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"PDF content")

        # Should work for Telegram
        limits_tg = mgr.get_channel_limits("telegram")
        assert test_file.stat().st_size < limits_tg["max_size"]
        assert "pdf" in limits_tg["allowed_extensions"]

        # Upload should succeed
        url = await mgr.upload(str(test_file), channel="telegram")
        assert url is not None

    @pytest.mark.asyncio
    async def test_file_lifecycle(self, tmp_path):
        """Test complete file lifecycle."""
        mgr = FileManager(
            storage_backend=StorageBackend.LOCAL,
            temp_dir=str(tmp_path / "temp"),
            upload_dir=str(tmp_path / "uploads")
        )

        # Create
        test_file = tmp_path / "lifecycle.txt"
        test_file.write_text("Lifecycle test")

        # Upload
        url = await mgr.upload(str(test_file), channel="telegram")

        # Get info
        files = mgr.list_files()
        file_id = files[0].file_id

        info = await mgr.get_info(file_id)
        assert info.filename == "lifecycle.txt"

        # Delete
        deleted = await mgr.delete(file_id)
        assert deleted is True

        # Verify deletion
        assert file_id not in [f.file_id for f in mgr.list_files()]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
