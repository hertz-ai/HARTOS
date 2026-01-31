"""
Media Limiter for size/type limits.

Provides configuration and checking of media limits.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List, Set
import logging

logger = logging.getLogger(__name__)


class MediaType(Enum):
    """Types of media."""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    ARCHIVE = "archive"
    OTHER = "other"


@dataclass
class MediaLimits:
    """
    Media limits configuration.

    Defines maximum sizes and allowed types for different media.
    """
    # Maximum file sizes in bytes
    max_image_size: int = 10 * 1024 * 1024      # 10MB
    max_video_size: int = 50 * 1024 * 1024      # 50MB
    max_audio_size: int = 25 * 1024 * 1024      # 25MB
    max_document_size: int = 25 * 1024 * 1024   # 25MB
    max_archive_size: int = 50 * 1024 * 1024    # 50MB
    max_other_size: int = 10 * 1024 * 1024      # 10MB

    # Maximum dimensions for images/video
    max_image_width: int = 4096
    max_image_height: int = 4096
    max_video_width: int = 1920
    max_video_height: int = 1080

    # Duration limits in seconds
    max_video_duration: int = 600   # 10 minutes
    max_audio_duration: int = 3600  # 1 hour

    # Allowed file extensions (empty = all allowed)
    allowed_image_extensions: Set[str] = field(default_factory=lambda: {
        'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg'
    })
    allowed_video_extensions: Set[str] = field(default_factory=lambda: {
        'mp4', 'webm', 'avi', 'mov', 'mkv', 'm4v'
    })
    allowed_audio_extensions: Set[str] = field(default_factory=lambda: {
        'mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac', 'wma'
    })
    allowed_document_extensions: Set[str] = field(default_factory=lambda: {
        'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
        'txt', 'csv', 'json', 'xml', 'md', 'rtf'
    })
    allowed_archive_extensions: Set[str] = field(default_factory=lambda: {
        'zip', 'tar', 'gz', 'rar', '7z', 'bz2'
    })

    # Blocked extensions (always blocked regardless of allowed)
    blocked_extensions: Set[str] = field(default_factory=lambda: {
        'exe', 'dll', 'bat', 'cmd', 'sh', 'ps1', 'vbs',
        'scr', 'msi', 'jar', 'app', 'dmg'
    })

    # MIME type restrictions
    allowed_mime_types: Set[str] = field(default_factory=set)
    blocked_mime_types: Set[str] = field(default_factory=lambda: {
        'application/x-executable',
        'application/x-msdownload',
        'application/x-sh'
    })

    # Additional constraints
    max_files_per_message: int = 10
    max_total_size: int = 100 * 1024 * 1024  # 100MB total

    def to_dict(self) -> Dict[str, Any]:
        """Convert limits to dictionary."""
        return {
            "max_image_size": self.max_image_size,
            "max_video_size": self.max_video_size,
            "max_audio_size": self.max_audio_size,
            "max_document_size": self.max_document_size,
            "max_archive_size": self.max_archive_size,
            "max_other_size": self.max_other_size,
            "max_image_width": self.max_image_width,
            "max_image_height": self.max_image_height,
            "max_video_width": self.max_video_width,
            "max_video_height": self.max_video_height,
            "max_video_duration": self.max_video_duration,
            "max_audio_duration": self.max_audio_duration,
            "allowed_image_extensions": list(self.allowed_image_extensions),
            "allowed_video_extensions": list(self.allowed_video_extensions),
            "allowed_audio_extensions": list(self.allowed_audio_extensions),
            "allowed_document_extensions": list(self.allowed_document_extensions),
            "allowed_archive_extensions": list(self.allowed_archive_extensions),
            "blocked_extensions": list(self.blocked_extensions),
            "max_files_per_message": self.max_files_per_message,
            "max_total_size": self.max_total_size
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MediaLimits':
        """Create MediaLimits from dictionary."""
        # Convert lists back to sets for extensions
        for key in ['allowed_image_extensions', 'allowed_video_extensions',
                    'allowed_audio_extensions', 'allowed_document_extensions',
                    'allowed_archive_extensions', 'blocked_extensions',
                    'allowed_mime_types', 'blocked_mime_types']:
            if key in data and isinstance(data[key], list):
                data[key] = set(data[key])
        return cls(**data)


@dataclass
class LimitCheckResult:
    """Result of a limit check."""
    allowed: bool
    reason: Optional[str] = None
    media_type: Optional[MediaType] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "media_type": self.media_type.value if self.media_type else None,
            "details": self.details
        }


class MediaLimiter:
    """
    Media limiter for checking size/type limits.

    Validates media against configured limits.
    """

    # Extension to media type mapping
    EXTENSION_TYPE_MAP = {
        # Images
        'jpg': MediaType.IMAGE, 'jpeg': MediaType.IMAGE, 'png': MediaType.IMAGE,
        'gif': MediaType.IMAGE, 'webp': MediaType.IMAGE, 'bmp': MediaType.IMAGE,
        'svg': MediaType.IMAGE, 'ico': MediaType.IMAGE, 'tiff': MediaType.IMAGE,
        # Video
        'mp4': MediaType.VIDEO, 'webm': MediaType.VIDEO, 'avi': MediaType.VIDEO,
        'mov': MediaType.VIDEO, 'mkv': MediaType.VIDEO, 'm4v': MediaType.VIDEO,
        'wmv': MediaType.VIDEO, 'flv': MediaType.VIDEO,
        # Audio
        'mp3': MediaType.AUDIO, 'wav': MediaType.AUDIO, 'ogg': MediaType.AUDIO,
        'flac': MediaType.AUDIO, 'm4a': MediaType.AUDIO, 'aac': MediaType.AUDIO,
        'wma': MediaType.AUDIO, 'opus': MediaType.AUDIO,
        # Documents
        'pdf': MediaType.DOCUMENT, 'doc': MediaType.DOCUMENT, 'docx': MediaType.DOCUMENT,
        'xls': MediaType.DOCUMENT, 'xlsx': MediaType.DOCUMENT, 'ppt': MediaType.DOCUMENT,
        'pptx': MediaType.DOCUMENT, 'txt': MediaType.DOCUMENT, 'csv': MediaType.DOCUMENT,
        'json': MediaType.DOCUMENT, 'xml': MediaType.DOCUMENT, 'md': MediaType.DOCUMENT,
        'rtf': MediaType.DOCUMENT, 'odt': MediaType.DOCUMENT,
        # Archives
        'zip': MediaType.ARCHIVE, 'tar': MediaType.ARCHIVE, 'gz': MediaType.ARCHIVE,
        'rar': MediaType.ARCHIVE, '7z': MediaType.ARCHIVE, 'bz2': MediaType.ARCHIVE
    }

    # MIME type to media type mapping
    MIME_TYPE_MAP = {
        'image/': MediaType.IMAGE,
        'video/': MediaType.VIDEO,
        'audio/': MediaType.AUDIO,
        'application/pdf': MediaType.DOCUMENT,
        'application/msword': MediaType.DOCUMENT,
        'application/vnd.': MediaType.DOCUMENT,
        'text/': MediaType.DOCUMENT,
        'application/zip': MediaType.ARCHIVE,
        'application/x-rar': MediaType.ARCHIVE,
        'application/x-7z': MediaType.ARCHIVE,
        'application/gzip': MediaType.ARCHIVE
    }

    def __init__(self, limits: Optional[MediaLimits] = None):
        """
        Initialize media limiter.

        Args:
            limits: Media limits configuration (uses defaults if not provided)
        """
        self._limits = limits or MediaLimits()

    def check(
        self,
        filename: Optional[str] = None,
        size: Optional[int] = None,
        mime_type: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        duration: Optional[float] = None
    ) -> LimitCheckResult:
        """
        Check if media meets limits.

        Args:
            filename: File name (for extension check)
            size: File size in bytes
            mime_type: MIME type of the file
            width: Width (for images/video)
            height: Height (for images/video)
            duration: Duration in seconds (for audio/video)

        Returns:
            LimitCheckResult indicating if media is allowed
        """
        # Determine media type
        media_type = self._get_media_type(filename, mime_type)

        # Check extension
        if filename:
            ext = self._get_extension(filename)

            # Check if blocked
            if ext in self._limits.blocked_extensions:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"File extension '{ext}' is blocked",
                    media_type=media_type,
                    details={"extension": ext}
                )

            # Check if allowed for media type
            allowed_exts = self._get_allowed_extensions(media_type)
            if allowed_exts and ext not in allowed_exts:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"File extension '{ext}' not allowed for {media_type.value}",
                    media_type=media_type,
                    details={"extension": ext, "allowed": list(allowed_exts)}
                )

        # Check MIME type
        if mime_type:
            if mime_type in self._limits.blocked_mime_types:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"MIME type '{mime_type}' is blocked",
                    media_type=media_type,
                    details={"mime_type": mime_type}
                )

            if self._limits.allowed_mime_types:
                if mime_type not in self._limits.allowed_mime_types:
                    return LimitCheckResult(
                        allowed=False,
                        reason=f"MIME type '{mime_type}' not in allowed list",
                        media_type=media_type,
                        details={"mime_type": mime_type}
                    )

        # Check size
        if size is not None:
            max_size = self._get_max_size(media_type)
            if size > max_size:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"File size ({size} bytes) exceeds limit ({max_size} bytes)",
                    media_type=media_type,
                    details={"size": size, "max_size": max_size}
                )

        # Check dimensions
        if media_type == MediaType.IMAGE:
            if width and width > self._limits.max_image_width:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Image width ({width}px) exceeds limit ({self._limits.max_image_width}px)",
                    media_type=media_type,
                    details={"width": width, "max_width": self._limits.max_image_width}
                )
            if height and height > self._limits.max_image_height:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Image height ({height}px) exceeds limit ({self._limits.max_image_height}px)",
                    media_type=media_type,
                    details={"height": height, "max_height": self._limits.max_image_height}
                )

        if media_type == MediaType.VIDEO:
            if width and width > self._limits.max_video_width:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Video width ({width}px) exceeds limit ({self._limits.max_video_width}px)",
                    media_type=media_type,
                    details={"width": width, "max_width": self._limits.max_video_width}
                )
            if height and height > self._limits.max_video_height:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Video height ({height}px) exceeds limit ({self._limits.max_video_height}px)",
                    media_type=media_type,
                    details={"height": height, "max_height": self._limits.max_video_height}
                )

        # Check duration
        if duration is not None:
            if media_type == MediaType.VIDEO and duration > self._limits.max_video_duration:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Video duration ({duration}s) exceeds limit ({self._limits.max_video_duration}s)",
                    media_type=media_type,
                    details={"duration": duration, "max_duration": self._limits.max_video_duration}
                )
            if media_type == MediaType.AUDIO and duration > self._limits.max_audio_duration:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Audio duration ({duration}s) exceeds limit ({self._limits.max_audio_duration}s)",
                    media_type=media_type,
                    details={"duration": duration, "max_duration": self._limits.max_audio_duration}
                )

        return LimitCheckResult(
            allowed=True,
            media_type=media_type
        )

    def check_batch(
        self,
        files: List[Dict[str, Any]]
    ) -> LimitCheckResult:
        """
        Check a batch of files against limits.

        Args:
            files: List of file info dicts with keys: filename, size, mime_type, etc.

        Returns:
            LimitCheckResult for the batch
        """
        # Check file count
        if len(files) > self._limits.max_files_per_message:
            return LimitCheckResult(
                allowed=False,
                reason=f"Too many files ({len(files)}), max is {self._limits.max_files_per_message}",
                details={"count": len(files), "max_count": self._limits.max_files_per_message}
            )

        # Check total size
        total_size = sum(f.get('size', 0) for f in files)
        if total_size > self._limits.max_total_size:
            return LimitCheckResult(
                allowed=False,
                reason=f"Total size ({total_size} bytes) exceeds limit ({self._limits.max_total_size} bytes)",
                details={"total_size": total_size, "max_total_size": self._limits.max_total_size}
            )

        # Check each file
        for i, file_info in enumerate(files):
            result = self.check(**file_info)
            if not result.allowed:
                result.details["file_index"] = i
                return result

        return LimitCheckResult(allowed=True)

    def get_limits(self) -> MediaLimits:
        """Get current limits."""
        return self._limits

    def set_limits(self, limits: MediaLimits):
        """Set new limits."""
        self._limits = limits

    def update_limits(self, **kwargs):
        """Update specific limit values."""
        for key, value in kwargs.items():
            if hasattr(self._limits, key):
                setattr(self._limits, key, value)

    def _get_extension(self, filename: str) -> str:
        """Extract lowercase extension from filename."""
        if '.' in filename:
            return filename.rsplit('.', 1)[-1].lower()
        return ''

    def _get_media_type(
        self,
        filename: Optional[str],
        mime_type: Optional[str]
    ) -> MediaType:
        """Determine media type from filename or MIME type."""
        # Try extension first
        if filename:
            ext = self._get_extension(filename)
            if ext in self.EXTENSION_TYPE_MAP:
                return self.EXTENSION_TYPE_MAP[ext]

        # Try MIME type
        if mime_type:
            for prefix, media_type in self.MIME_TYPE_MAP.items():
                if mime_type.startswith(prefix):
                    return media_type

        return MediaType.OTHER

    def _get_max_size(self, media_type: MediaType) -> int:
        """Get maximum size for media type."""
        size_map = {
            MediaType.IMAGE: self._limits.max_image_size,
            MediaType.VIDEO: self._limits.max_video_size,
            MediaType.AUDIO: self._limits.max_audio_size,
            MediaType.DOCUMENT: self._limits.max_document_size,
            MediaType.ARCHIVE: self._limits.max_archive_size,
            MediaType.OTHER: self._limits.max_other_size
        }
        return size_map.get(media_type, self._limits.max_other_size)

    def _get_allowed_extensions(self, media_type: MediaType) -> Set[str]:
        """Get allowed extensions for media type."""
        ext_map = {
            MediaType.IMAGE: self._limits.allowed_image_extensions,
            MediaType.VIDEO: self._limits.allowed_video_extensions,
            MediaType.AUDIO: self._limits.allowed_audio_extensions,
            MediaType.DOCUMENT: self._limits.allowed_document_extensions,
            MediaType.ARCHIVE: self._limits.allowed_archive_extensions
        }
        return ext_map.get(media_type, set())

    def format_size(self, size: int) -> str:
        """Format size in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def get_limits_summary(self) -> Dict[str, str]:
        """Get human-readable limits summary."""
        return {
            "image": f"Max {self.format_size(self._limits.max_image_size)}, {self._limits.max_image_width}x{self._limits.max_image_height}px",
            "video": f"Max {self.format_size(self._limits.max_video_size)}, {self._limits.max_video_duration}s, {self._limits.max_video_width}x{self._limits.max_video_height}px",
            "audio": f"Max {self.format_size(self._limits.max_audio_size)}, {self._limits.max_audio_duration}s",
            "document": f"Max {self.format_size(self._limits.max_document_size)}",
            "total": f"Max {self._limits.max_files_per_message} files, {self.format_size(self._limits.max_total_size)} total"
        }
