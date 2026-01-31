"""
File Manager for file handling operations.

Provides download, upload, and file management functionality.
"""

import asyncio
import os
import hashlib
import mimetypes
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
from urllib.parse import urlparse, unquote
import logging

logger = logging.getLogger(__name__)

# Docker-compatible paths
TEMP_DIR = os.environ.get("FILE_TEMP_DIR", "/tmp/files")
APP_TEMP_DIR = os.environ.get("APP_TEMP_DIR", "/app/temp")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/app/uploads")


class FileStatus(Enum):
    """File operation status."""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class StorageBackend(Enum):
    """Storage backend types."""
    LOCAL = "local"
    S3 = "s3"
    GCS = "gcs"  # Google Cloud Storage
    AZURE = "azure"


@dataclass
class FileInfo:
    """Information about a file."""
    file_id: str
    filename: str
    size: int
    mime_type: str
    url: Optional[str] = None
    local_path: Optional[str] = None
    channel: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    checksum: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "size": self.size,
            "mime_type": self.mime_type,
            "url": self.url,
            "local_path": self.local_path,
            "channel": self.channel,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "checksum": self.checksum,
            "metadata": self.metadata
        }

    def is_expired(self) -> bool:
        """Check if file has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def get_extension(self) -> str:
        """Get file extension."""
        if "." in self.filename:
            return self.filename.rsplit(".", 1)[-1].lower()
        return ""


@dataclass
class DownloadResult:
    """Result of a download operation."""
    success: bool
    file_path: Optional[str] = None
    file_info: Optional[FileInfo] = None
    error: Optional[str] = None
    download_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "file_path": self.file_path,
            "file_info": self.file_info.to_dict() if self.file_info else None,
            "error": self.error,
            "download_time": self.download_time
        }


@dataclass
class UploadResult:
    """Result of an upload operation."""
    success: bool
    url: Optional[str] = None
    file_id: Optional[str] = None
    file_info: Optional[FileInfo] = None
    error: Optional[str] = None
    upload_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "url": self.url,
            "file_id": self.file_id,
            "file_info": self.file_info.to_dict() if self.file_info else None,
            "error": self.error,
            "upload_time": self.upload_time
        }


class FileManager:
    """
    File manager for handling file operations.

    Provides download, upload, and file management across channels.
    """

    # Maximum file sizes per channel (bytes)
    CHANNEL_MAX_SIZES = {
        "telegram": 50 * 1024 * 1024,  # 50MB
        "discord": 8 * 1024 * 1024,    # 8MB (without Nitro)
        "slack": 1 * 1024 * 1024 * 1024,  # 1GB
        "whatsapp": 16 * 1024 * 1024,  # 16MB
        "default": 25 * 1024 * 1024    # 25MB
    }

    # Allowed file extensions per channel
    CHANNEL_ALLOWED_EXTENSIONS = {
        "telegram": ["jpg", "jpeg", "png", "gif", "webp", "mp4", "mp3", "pdf", "doc", "docx", "zip"],
        "discord": ["jpg", "jpeg", "png", "gif", "webp", "mp4", "mp3", "wav", "pdf", "txt"],
        "slack": ["jpg", "jpeg", "png", "gif", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip"],
        "whatsapp": ["jpg", "jpeg", "png", "gif", "mp4", "mp3", "pdf", "doc", "docx"],
        "default": ["jpg", "jpeg", "png", "gif", "pdf", "txt"]
    }

    def __init__(
        self,
        storage_backend: Union[StorageBackend, str] = StorageBackend.LOCAL,
        temp_dir: Optional[str] = None,
        upload_dir: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize file manager.

        Args:
            storage_backend: Storage backend to use
            temp_dir: Temporary directory for downloads
            upload_dir: Directory for uploads
            config: Additional configuration options
        """
        if isinstance(storage_backend, str):
            storage_backend = StorageBackend(storage_backend.lower())

        self.storage_backend = storage_backend
        self.temp_dir = temp_dir or TEMP_DIR
        self.upload_dir = upload_dir or UPLOAD_DIR
        self.config = config or {}

        # File tracking
        self._files: Dict[str, FileInfo] = {}

        # Cloud storage clients (lazy initialized)
        self._s3_client = None
        self._gcs_client = None
        self._azure_client = None

        # Ensure directories exist
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Ensure required directories exist (Docker-compatible)."""
        for dir_path in [self.temp_dir, self.upload_dir, TEMP_DIR, APP_TEMP_DIR]:
            try:
                Path(dir_path).mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError) as e:
                logger.warning(f"Could not create directory {dir_path}: {e}")

    def _generate_file_id(self, content: bytes = None, filename: str = None) -> str:
        """Generate unique file ID."""
        data = f"{time.time()}{filename or ''}"
        if content:
            data += hashlib.md5(content[:1024]).hexdigest()
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def _get_mime_type(self, filename: str) -> str:
        """Get MIME type for filename."""
        mime_type, _ = mimetypes.guess_type(filename)
        return mime_type or "application/octet-stream"

    def _get_filename_from_url(self, url: str) -> str:
        """Extract filename from URL."""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        filename = os.path.basename(path)
        return filename or f"file_{int(time.time())}"

    async def download(
        self,
        url: str,
        destination: Optional[str] = None,
        timeout: int = 60,
        max_size: Optional[int] = None
    ) -> str:
        """
        Download file from URL.

        Args:
            url: URL to download from
            destination: Destination path (auto-generated if not provided)
            timeout: Download timeout in seconds
            max_size: Maximum file size to download

        Returns:
            Path to downloaded file
        """
        start_time = time.time()

        try:
            # Determine filename and destination
            filename = self._get_filename_from_url(url)
            if destination is None:
                destination = os.path.join(self.temp_dir, filename)

            logger.info(f"Downloading {url} to {destination}")

            # Ensure destination directory exists
            Path(destination).parent.mkdir(parents=True, exist_ok=True)

            # Would use aiohttp or httpx for actual download
            # async with aiohttp.ClientSession() as session:
            #     async with session.get(url, timeout=timeout) as response:
            #         if response.status != 200:
            #             raise Exception(f"Download failed: {response.status}")
            #
            #         # Check content length
            #         content_length = response.headers.get("content-length")
            #         if content_length and max_size and int(content_length) > max_size:
            #             raise Exception(f"File too large: {content_length} > {max_size}")
            #
            #         with open(destination, 'wb') as f:
            #             async for chunk in response.content.iter_chunked(8192):
            #                 f.write(chunk)

            # Placeholder - simulated successful download
            Path(destination).touch()

            # Track file
            file_id = self._generate_file_id(filename=filename)
            file_info = FileInfo(
                file_id=file_id,
                filename=filename,
                size=0,  # Would be actual size
                mime_type=self._get_mime_type(filename),
                url=url,
                local_path=destination,
                metadata={"download_time": time.time() - start_time}
            )
            self._files[file_id] = file_info

            return destination

        except Exception as e:
            logger.error(f"Download failed: {e}")
            raise

    async def upload(
        self,
        file_path: str,
        channel: str,
        filename: Optional[str] = None
    ) -> str:
        """
        Upload file to storage and return URL.

        Args:
            file_path: Path to file to upload
            channel: Target channel (for size/type validation)
            filename: Override filename

        Returns:
            URL to uploaded file
        """
        start_time = time.time()

        try:
            path = Path(file_path)
            if not path.exists():
                raise FileNotFoundError(f"File not found: {file_path}")

            filename = filename or path.name
            file_size = path.stat().st_size

            # Validate file for channel
            self._validate_for_channel(filename, file_size, channel)

            logger.info(f"Uploading {file_path} for {channel}")

            # Read file content
            with open(path, 'rb') as f:
                content = f.read()

            # Generate file ID and checksum
            file_id = self._generate_file_id(content, filename)
            checksum = hashlib.md5(content).hexdigest()

            # Upload based on backend
            if self.storage_backend == StorageBackend.LOCAL:
                url = await self._upload_local(file_id, filename, content)
            elif self.storage_backend == StorageBackend.S3:
                url = await self._upload_s3(file_id, filename, content)
            elif self.storage_backend == StorageBackend.GCS:
                url = await self._upload_gcs(file_id, filename, content)
            elif self.storage_backend == StorageBackend.AZURE:
                url = await self._upload_azure(file_id, filename, content)
            else:
                url = await self._upload_local(file_id, filename, content)

            # Track file
            file_info = FileInfo(
                file_id=file_id,
                filename=filename,
                size=file_size,
                mime_type=self._get_mime_type(filename),
                url=url,
                local_path=file_path,
                channel=channel,
                checksum=checksum,
                metadata={"upload_time": time.time() - start_time}
            )
            self._files[file_id] = file_info

            return url

        except Exception as e:
            logger.error(f"Upload failed: {e}")
            raise

    async def _upload_local(
        self,
        file_id: str,
        filename: str,
        content: bytes
    ) -> str:
        """Upload to local storage."""
        dest_path = os.path.join(self.upload_dir, file_id, filename)
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)

        with open(dest_path, 'wb') as f:
            f.write(content)

        # Return local file URL (would be served by web server)
        return f"/files/{file_id}/{filename}"

    async def _upload_s3(
        self,
        file_id: str,
        filename: str,
        content: bytes
    ) -> str:
        """Upload to Amazon S3."""
        # Would use boto3
        # s3 = boto3.client('s3')
        # bucket = self.config.get('s3_bucket')
        # key = f"{file_id}/{filename}"
        # s3.put_object(Bucket=bucket, Key=key, Body=content)
        # return f"https://{bucket}.s3.amazonaws.com/{key}"
        return f"https://s3.example.com/{file_id}/{filename}"

    async def _upload_gcs(
        self,
        file_id: str,
        filename: str,
        content: bytes
    ) -> str:
        """Upload to Google Cloud Storage."""
        # Would use google-cloud-storage
        return f"https://storage.googleapis.com/bucket/{file_id}/{filename}"

    async def _upload_azure(
        self,
        file_id: str,
        filename: str,
        content: bytes
    ) -> str:
        """Upload to Azure Blob Storage."""
        # Would use azure-storage-blob
        return f"https://account.blob.core.windows.net/container/{file_id}/{filename}"

    def _validate_for_channel(
        self,
        filename: str,
        file_size: int,
        channel: str
    ):
        """Validate file for channel restrictions."""
        # Check size
        max_size = self.CHANNEL_MAX_SIZES.get(
            channel.lower(),
            self.CHANNEL_MAX_SIZES["default"]
        )
        if file_size > max_size:
            raise ValueError(
                f"File too large for {channel}: {file_size} > {max_size}"
            )

        # Check extension
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        allowed = self.CHANNEL_ALLOWED_EXTENSIONS.get(
            channel.lower(),
            self.CHANNEL_ALLOWED_EXTENSIONS["default"]
        )
        if ext and ext not in allowed:
            raise ValueError(
                f"File type '{ext}' not allowed for {channel}"
            )

    async def get_info(
        self,
        file_id: str,
        channel: Optional[str] = None
    ) -> FileInfo:
        """
        Get information about a file.

        Args:
            file_id: File identifier
            channel: Channel context (for channel-specific info)

        Returns:
            FileInfo object
        """
        if file_id in self._files:
            return self._files[file_id]

        # Would query storage backend for file info
        raise FileNotFoundError(f"File not found: {file_id}")

    async def get_info_from_path(self, file_path: str) -> FileInfo:
        """
        Get file info from local path.

        Args:
            file_path: Path to file

        Returns:
            FileInfo object
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        stat = path.stat()

        with open(path, 'rb') as f:
            content_start = f.read(1024)
            checksum = hashlib.md5(content_start).hexdigest()

        return FileInfo(
            file_id=self._generate_file_id(content_start, path.name),
            filename=path.name,
            size=stat.st_size,
            mime_type=self._get_mime_type(path.name),
            local_path=str(path),
            created_at=stat.st_ctime,
            checksum=checksum
        )

    def cleanup_temp(self, max_age_hours: int = 24) -> int:
        """
        Clean up temporary files older than max_age.

        Args:
            max_age_hours: Maximum age in hours

        Returns:
            Number of files deleted
        """
        deleted = 0
        max_age_seconds = max_age_hours * 3600
        cutoff_time = time.time() - max_age_seconds

        for dir_path in [self.temp_dir, TEMP_DIR]:
            try:
                path = Path(dir_path)
                if not path.exists():
                    continue

                for file_path in path.rglob("*"):
                    if file_path.is_file():
                        try:
                            if file_path.stat().st_mtime < cutoff_time:
                                file_path.unlink()
                                deleted += 1
                                logger.debug(f"Deleted temp file: {file_path}")
                        except (PermissionError, OSError) as e:
                            logger.warning(f"Could not delete {file_path}: {e}")

            except Exception as e:
                logger.error(f"Error cleaning temp directory {dir_path}: {e}")

        # Also clean tracked files
        expired_ids = [
            fid for fid, info in self._files.items()
            if info.is_expired() or (info.created_at and info.created_at < cutoff_time)
        ]
        for file_id in expired_ids:
            del self._files[file_id]
            deleted += 1

        logger.info(f"Cleaned up {deleted} temporary files")
        return deleted

    async def delete(self, file_id: str) -> bool:
        """
        Delete a file.

        Args:
            file_id: File identifier

        Returns:
            True if deleted successfully
        """
        if file_id not in self._files:
            return False

        file_info = self._files[file_id]

        # Delete local file
        if file_info.local_path:
            try:
                Path(file_info.local_path).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Could not delete local file: {e}")

        # Delete from storage backend
        if self.storage_backend == StorageBackend.S3:
            # Would delete from S3
            pass
        elif self.storage_backend == StorageBackend.GCS:
            # Would delete from GCS
            pass
        elif self.storage_backend == StorageBackend.AZURE:
            # Would delete from Azure
            pass

        # Remove from tracking
        del self._files[file_id]
        return True

    async def copy(
        self,
        source_path: str,
        dest_path: str
    ) -> str:
        """
        Copy a file.

        Args:
            source_path: Source file path
            dest_path: Destination file path

        Returns:
            Destination path
        """
        import shutil

        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(source, dest)
        return str(dest)

    async def move(
        self,
        source_path: str,
        dest_path: str
    ) -> str:
        """
        Move a file.

        Args:
            source_path: Source file path
            dest_path: Destination file path

        Returns:
            Destination path
        """
        import shutil

        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(source, dest)
        return str(dest)

    def get_temp_path(self, prefix: str = "file", extension: str = "") -> str:
        """
        Get a temporary file path.

        Args:
            prefix: File name prefix
            extension: File extension (without dot)

        Returns:
            Temporary file path (Docker-compatible)
        """
        timestamp = int(time.time() * 1000)
        random_hash = hashlib.md5(str(timestamp).encode()).hexdigest()[:8]
        filename = f"{prefix}_{timestamp}_{random_hash}"
        if extension:
            filename += f".{extension}"
        return os.path.join(self.temp_dir, filename)

    def format_size(self, size: int) -> str:
        """Format size in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def get_channel_limits(self, channel: str) -> Dict[str, Any]:
        """Get file limits for a channel."""
        return {
            "max_size": self.CHANNEL_MAX_SIZES.get(
                channel.lower(),
                self.CHANNEL_MAX_SIZES["default"]
            ),
            "max_size_formatted": self.format_size(
                self.CHANNEL_MAX_SIZES.get(
                    channel.lower(),
                    self.CHANNEL_MAX_SIZES["default"]
                )
            ),
            "allowed_extensions": self.CHANNEL_ALLOWED_EXTENSIONS.get(
                channel.lower(),
                self.CHANNEL_ALLOWED_EXTENSIONS["default"]
            )
        }

    def list_files(
        self,
        channel: Optional[str] = None,
        include_expired: bool = False
    ) -> List[FileInfo]:
        """
        List tracked files.

        Args:
            channel: Filter by channel
            include_expired: Include expired files

        Returns:
            List of FileInfo objects
        """
        files = list(self._files.values())

        if channel:
            files = [f for f in files if f.channel == channel]

        if not include_expired:
            files = [f for f in files if not f.is_expired()]

        return sorted(files, key=lambda f: f.created_at, reverse=True)

    def get_storage_info(self) -> Dict[str, Any]:
        """Get information about storage configuration."""
        return {
            "backend": self.storage_backend.value,
            "temp_dir": self.temp_dir,
            "upload_dir": self.upload_dir,
            "tracked_files": len(self._files),
            "supported_channels": list(self.CHANNEL_MAX_SIZES.keys())
        }
