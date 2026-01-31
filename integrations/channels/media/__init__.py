"""
Media Processing Module for Multi-Channel Messaging.

Provides unified media handling for:
- Vision/Image analysis (vision.py)
- Audio transcription (audio.py)
- Text-to-Speech synthesis (tts.py)
- Image generation (image_gen.py)
- Link processing (links.py)
- File management (files.py)
- Media limits (limits.py)
"""

# Vision Processing
from .vision import (
    VisionProvider,
    VisionProcessor,
    ImageAnalysis,
    DetectedObject,
    BoundingBox,
    OCRResult,
)

# Audio Processing (Speech-to-Text)
from .audio import (
    AudioProvider,
    AudioProcessor,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
    LanguageDetection,
)

# Text-to-Speech
from .tts import (
    TTSProvider,
    TTSEngine,
    VoiceInfo,
    SynthesisResult,
    SSMLConfig,
    AudioFormat,
)

# Image Generation
from .image_gen import (
    ImageProvider,
    ImageGenerator,
    ImageSize,
    ImageStyle,
    GeneratedImage,
    EditResult,
    VariationResult,
)

# Link Processing
from .links import (
    LinkType,
    LinkProcessor,
    LinkPreview,
    LinkSummary,
    FetchedContent,
    DetectedLink,
    OpenGraphData,
)

# File Management
from .files import (
    FileStatus,
    StorageBackend,
    FileManager,
    FileInfo,
    DownloadResult,
    UploadResult,
)

# Media Limits
from .limits import (
    MediaType,
    MediaLimits,
    MediaLimiter,
    LimitCheckResult,
)

__all__ = [
    # Vision
    "VisionProvider",
    "VisionProcessor",
    "ImageAnalysis",
    "DetectedObject",
    "BoundingBox",
    "OCRResult",
    # Audio (STT)
    "AudioProvider",
    "AudioProcessor",
    "TranscriptionResult",
    "TranscriptionSegment",
    "TranscriptionWord",
    "LanguageDetection",
    # TTS
    "TTSProvider",
    "TTSEngine",
    "VoiceInfo",
    "SynthesisResult",
    "SSMLConfig",
    "AudioFormat",
    # Image Generation
    "ImageProvider",
    "ImageGenerator",
    "ImageSize",
    "ImageStyle",
    "GeneratedImage",
    "EditResult",
    "VariationResult",
    # Links
    "LinkType",
    "LinkProcessor",
    "LinkPreview",
    "LinkSummary",
    "FetchedContent",
    "DetectedLink",
    "OpenGraphData",
    # Files
    "FileStatus",
    "StorageBackend",
    "FileManager",
    "FileInfo",
    "DownloadResult",
    "UploadResult",
    # Limits
    "MediaType",
    "MediaLimits",
    "MediaLimiter",
    "LimitCheckResult",
]
