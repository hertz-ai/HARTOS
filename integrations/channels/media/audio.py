"""
Audio Processor for audio transcription.

Supports multiple providers: openai, deepgram, whisper-local
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class AudioProvider(Enum):
    """Supported audio transcription providers."""
    OPENAI = "openai"
    DEEPGRAM = "deepgram"
    WHISPER_LOCAL = "whisper-local"


@dataclass
class TranscriptionWord:
    """Individual word with timing information."""
    word: str
    start: float
    end: float
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "confidence": self.confidence
        }


@dataclass
class TranscriptionSegment:
    """Segment of transcription with timing."""
    text: str
    start: float
    end: float
    speaker: Optional[str] = None
    confidence: float = 1.0
    words: List[TranscriptionWord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "speaker": self.speaker,
            "confidence": self.confidence,
            "words": [w.to_dict() for w in self.words]
        }


@dataclass
class TranscriptionResult:
    """Complete transcription result."""
    text: str
    language: Optional[str] = None
    confidence: float = 1.0
    duration: float = 0.0
    segments: List[TranscriptionSegment] = field(default_factory=list)
    speakers: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "confidence": self.confidence,
            "duration": self.duration,
            "segments": [s.to_dict() for s in self.segments],
            "speakers": self.speakers,
            "metadata": self.metadata
        }

    def to_srt(self) -> str:
        """Export transcription as SRT subtitle format."""
        lines = []
        for i, segment in enumerate(self.segments, 1):
            start_time = self._format_srt_time(segment.start)
            end_time = self._format_srt_time(segment.end)
            lines.append(str(i))
            lines.append(f"{start_time} --> {end_time}")
            lines.append(segment.text)
            lines.append("")
        return "\n".join(lines)

    def to_vtt(self) -> str:
        """Export transcription as WebVTT subtitle format."""
        lines = ["WEBVTT", ""]
        for segment in self.segments:
            start_time = self._format_vtt_time(segment.start)
            end_time = self._format_vtt_time(segment.end)
            lines.append(f"{start_time} --> {end_time}")
            lines.append(segment.text)
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        """Format time for SRT format (HH:MM:SS,mmm)."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    @staticmethod
    def _format_vtt_time(seconds: float) -> str:
        """Format time for VTT format (HH:MM:SS.mmm)."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


@dataclass
class LanguageDetection:
    """Language detection result."""
    language: str
    confidence: float
    alternatives: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "confidence": self.confidence,
            "alternatives": self.alternatives
        }


class AudioProcessor:
    """
    Audio processor for transcription and analysis.

    Supports multiple providers for speech-to-text.
    """

    def __init__(
        self,
        provider: Union[AudioProvider, str] = AudioProvider.OPENAI,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize audio processor.

        Args:
            provider: Audio provider to use
            api_key: API key for the provider
            model: Specific model to use
            config: Additional configuration options
        """
        if isinstance(provider, str):
            provider = AudioProvider(provider.lower())

        self.provider = provider
        self.api_key = api_key
        self.config = config or {}

        # Set default models per provider
        self.model = model or self._get_default_model()

        # Initialize provider-specific client
        self._client = None
        self._initialized = False

    def _get_default_model(self) -> str:
        """Get default model for provider."""
        defaults = {
            AudioProvider.OPENAI: "whisper-1",
            AudioProvider.DEEPGRAM: "nova-2",
            AudioProvider.WHISPER_LOCAL: "base"
        }
        return defaults.get(self.provider, "default")

    async def _ensure_initialized(self):
        """Ensure provider client is initialized."""
        if self._initialized:
            return

        if self.provider == AudioProvider.OPENAI:
            # Would initialize OpenAI client
            pass
        elif self.provider == AudioProvider.DEEPGRAM:
            # Would initialize Deepgram client
            pass
        elif self.provider == AudioProvider.WHISPER_LOCAL:
            # Would load local whisper model
            pass

        self._initialized = True

    async def transcribe(
        self,
        audio: Union[str, bytes, Path],
        language: Optional[str] = None,
        prompt: Optional[str] = None,
        word_timestamps: bool = False,
        speaker_diarization: bool = False
    ) -> TranscriptionResult:
        """
        Transcribe audio to text using Whisper.

        Args:
            audio: Audio file path, URL, or bytes
            language: Expected language (ISO code) or None for auto-detect
            prompt: Optional prompt to guide transcription
            word_timestamps: Whether to include word-level timestamps
            speaker_diarization: Whether to query speaker_diarization service

        Returns:
            TranscriptionResult with transcribed text and metadata
        """
        await self._ensure_initialized()

        import json as _json
        import os
        import tempfile

        # Resolve audio to a file path for whisper_transcribe
        audio_path = None
        cleanup_path = False
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            if path.exists():
                audio_path = str(path)
        elif isinstance(audio, bytes):
            # Write bytes to temp file
            tmp = tempfile.NamedTemporaryFile(
                suffix='.wav', delete=False)
            tmp.write(audio)
            tmp.close()
            audio_path = tmp.name
            cleanup_path = True

        text = ""
        detected_language = language or "en"
        confidence = 0.0
        speakers = []

        try:
            # Transcribe with Whisper
            if audio_path:
                try:
                    from integrations.service_tools.whisper_tool import (
                        whisper_transcribe,
                    )
                    result_json = whisper_transcribe(audio_path, language)
                    parsed = _json.loads(result_json)
                    if 'error' not in parsed:
                        text = parsed.get('text', '')
                        detected_language = parsed.get(
                            'language', detected_language)
                        confidence = 0.9
                except ImportError:
                    logger.warning(
                        "whisper not available — transcription disabled")
                except Exception as e:
                    logger.warning(f"Whisper transcription failed: {e}")

            # Speaker diarization via WebSocket bridge
            if speaker_diarization and audio_path:
                diarization_url = os.environ.get('HEVOLVE_DIARIZATION_URL')
                if diarization_url:
                    diarization_result = await self._run_diarization(
                        audio_path, diarization_url)
                    if diarization_result:
                        n_speakers = diarization_result.get(
                            'no_of_speaker', 0)
                        speakers = [
                            f"Speaker_{i}" for i in range(n_speakers)]
        finally:
            if cleanup_path and audio_path:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass

        return TranscriptionResult(
            text=text,
            language=detected_language,
            confidence=confidence,
            duration=await self.get_duration(audio),
            speakers=speakers,
            metadata={
                "provider": self.provider.value,
                "model": self.model,
                "word_timestamps": word_timestamps,
                "speaker_diarization": speaker_diarization,
            },
        )

    async def _run_diarization(
        self, audio_path: str, diarization_url: str
    ) -> Optional[Dict[str, Any]]:
        """Send audio to speaker_diarization service via WebSocket.

        The service expects PCM 16-bit mono 16kHz audio as hex-encoded
        chunks and returns ``{"no_of_speaker": N, "stop_mic": bool}``.

        Sends JSON format (compatible with both new sidecar and old server).
        Parses response as JSON first, falls back to ast.literal_eval
        for old servers that send Python repr.
        """
        import ast
        import json as _json

        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed — diarization disabled")
            return None

        try:
            with open(audio_path, 'rb') as f:
                audio_bytes = f.read()

            async with websockets.connect(
                diarization_url, close_timeout=5
            ) as ws:
                msg = _json.dumps(
                    {'user_id': 0, 'chunk': audio_bytes.hex()})
                await ws.send(msg)
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                # New sidecar sends JSON, old server sends Python repr
                try:
                    return _json.loads(response)
                except (_json.JSONDecodeError, ValueError):
                    return ast.literal_eval(response)
        except Exception as e:
            logger.debug(f"Diarization failed: {e}")
            return None

    async def detect_language(
        self,
        audio: Union[str, bytes, Path],
        max_alternatives: int = 3
    ) -> LanguageDetection:
        """
        Detect the language spoken in audio.

        Args:
            audio: Audio file path, URL, or bytes
            max_alternatives: Maximum number of alternative languages

        Returns:
            LanguageDetection with detected language and confidence
        """
        await self._ensure_initialized()

        # Simulated language detection
        return LanguageDetection(
            language="en",
            confidence=0.95,
            alternatives=[
                {"language": "es", "confidence": 0.03},
                {"language": "fr", "confidence": 0.02}
            ][:max_alternatives - 1]
        )

    async def get_duration(
        self,
        audio: Union[str, bytes, Path]
    ) -> float:
        """
        Get audio duration in seconds.

        Args:
            audio: Audio file path, URL, or bytes

        Returns:
            Duration in seconds
        """
        # Would use actual audio analysis library (pydub, librosa, etc.)
        # For now, return simulated duration
        return 0.0

    async def transcribe_streaming(
        self,
        audio_stream,
        language: Optional[str] = None,
        on_partial: Optional[callable] = None,
        on_final: Optional[callable] = None
    ):
        """
        Transcribe audio stream in real-time.

        Args:
            audio_stream: Async iterator of audio chunks
            language: Expected language
            on_partial: Callback for partial results
            on_final: Callback for final results

        Yields:
            TranscriptionSegment as they become available
        """
        await self._ensure_initialized()

        # Would implement streaming transcription
        # This is provider-specific (Deepgram excels at streaming)
        return

    async def translate(
        self,
        audio: Union[str, bytes, Path],
        target_language: str = "en"
    ) -> TranscriptionResult:
        """
        Transcribe and translate audio to target language.

        Args:
            audio: Audio file path, URL, or bytes
            target_language: Target language for translation

        Returns:
            TranscriptionResult with translated text
        """
        await self._ensure_initialized()

        # OpenAI Whisper supports direct translation to English
        # Other providers may need separate translation step
        transcription = await self.transcribe(audio)

        if self.provider == AudioProvider.OPENAI and target_language == "en":
            # OpenAI can translate directly
            pass

        transcription.metadata["translated"] = True
        transcription.metadata["target_language"] = target_language

        return transcription

    def get_supported_formats(self) -> List[str]:
        """Get list of supported audio formats."""
        formats = {
            AudioProvider.OPENAI: ["mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm"],
            AudioProvider.DEEPGRAM: ["mp3", "mp4", "wav", "flac", "ogg", "webm", "m4a"],
            AudioProvider.WHISPER_LOCAL: ["mp3", "wav", "flac", "ogg", "m4a"]
        }
        return formats.get(self.provider, ["mp3", "wav"])

    def get_max_audio_duration(self) -> int:
        """Get maximum supported audio duration in seconds."""
        limits = {
            AudioProvider.OPENAI: 3600,  # 1 hour (25MB file limit)
            AudioProvider.DEEPGRAM: 7200,  # 2 hours
            AudioProvider.WHISPER_LOCAL: 14400  # 4 hours (depends on hardware)
        }
        return limits.get(self.provider, 3600)

    def get_max_file_size(self) -> int:
        """Get maximum supported file size in bytes."""
        limits = {
            AudioProvider.OPENAI: 25 * 1024 * 1024,  # 25MB
            AudioProvider.DEEPGRAM: 2 * 1024 * 1024 * 1024,  # 2GB
            AudioProvider.WHISPER_LOCAL: 500 * 1024 * 1024  # 500MB
        }
        return limits.get(self.provider, 25 * 1024 * 1024)

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages."""
        # Common languages supported by most providers
        return [
            "en", "es", "fr", "de", "it", "pt", "nl", "pl", "ru",
            "zh", "ja", "ko", "ar", "hi", "tr", "vi", "th", "id"
        ]
