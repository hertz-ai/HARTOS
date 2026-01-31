"""
Text-to-Speech System for audio synthesis.

Supports multiple providers: openai, elevenlabs, edge, google, amazon
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
import logging
import os

logger = logging.getLogger(__name__)

# Docker-compatible paths
TEMP_DIR = os.environ.get("TTS_TEMP_DIR", "/tmp/tts")
APP_TEMP_DIR = os.environ.get("APP_TEMP_DIR", "/app/temp")


class TTSProvider(Enum):
    """Supported TTS providers."""
    OPENAI = "openai"
    ELEVENLABS = "elevenlabs"
    EDGE = "edge"
    GOOGLE = "google"
    AMAZON = "amazon"


class AudioFormat(Enum):
    """Supported audio output formats."""
    MP3 = "mp3"
    OPUS = "opus"
    WAV = "wav"
    OGG = "ogg"
    AAC = "aac"
    FLAC = "flac"
    PCM = "pcm"


@dataclass
class VoiceInfo:
    """Information about an available voice."""
    id: str
    name: str
    language: str
    gender: Optional[str] = None
    description: Optional[str] = None
    preview_url: Optional[str] = None
    provider: Optional[str] = None
    styles: List[str] = field(default_factory=list)
    sample_rate: int = 24000
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "language": self.language,
            "gender": self.gender,
            "description": self.description,
            "preview_url": self.preview_url,
            "provider": self.provider,
            "styles": self.styles,
            "sample_rate": self.sample_rate,
            "metadata": self.metadata
        }


@dataclass
class SynthesisResult:
    """Result of a TTS synthesis operation."""
    audio: bytes
    format: AudioFormat
    duration: float
    sample_rate: int
    voice_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format.value,
            "duration": self.duration,
            "sample_rate": self.sample_rate,
            "voice_id": self.voice_id,
            "size": len(self.audio),
            "metadata": self.metadata
        }


@dataclass
class SSMLConfig:
    """SSML synthesis configuration."""
    rate: Optional[str] = None  # x-slow, slow, medium, fast, x-fast
    pitch: Optional[str] = None  # x-low, low, medium, high, x-high
    volume: Optional[str] = None  # silent, x-soft, soft, medium, loud, x-loud
    emphasis: Optional[str] = None  # strong, moderate, reduced
    language: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rate": self.rate,
            "pitch": self.pitch,
            "volume": self.volume,
            "emphasis": self.emphasis,
            "language": self.language
        }


class TTSEngine:
    """
    Text-to-Speech engine for audio synthesis.

    Supports multiple providers for converting text to speech.
    """

    # Optimal formats per channel
    CHANNEL_FORMATS = {
        "telegram": AudioFormat.OGG,
        "discord": AudioFormat.OPUS,
        "whatsapp": AudioFormat.OGG,
        "slack": AudioFormat.MP3,
        "web": AudioFormat.MP3,
        "default": AudioFormat.MP3
    }

    # Default voices per provider
    DEFAULT_VOICES = {
        TTSProvider.OPENAI: "alloy",
        TTSProvider.ELEVENLABS: "21m00Tcm4TlvDq8ikWAM",  # Rachel
        TTSProvider.EDGE: "en-US-AriaNeural",
        TTSProvider.GOOGLE: "en-US-Standard-A",
        TTSProvider.AMAZON: "Joanna"
    }

    # Models per provider
    DEFAULT_MODELS = {
        TTSProvider.OPENAI: "tts-1",
        TTSProvider.ELEVENLABS: "eleven_monolingual_v1",
        TTSProvider.EDGE: "neural",
        TTSProvider.GOOGLE: "standard",
        TTSProvider.AMAZON: "neural"
    }

    def __init__(
        self,
        provider: Union[TTSProvider, str] = TTSProvider.OPENAI,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        default_voice: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize TTS engine.

        Args:
            provider: TTS provider to use
            api_key: API key for the provider
            model: Specific model to use
            default_voice: Default voice ID
            config: Additional configuration options
        """
        if isinstance(provider, str):
            provider = TTSProvider(provider.lower())

        self.provider = provider
        self.api_key = api_key
        self.config = config or {}

        # Set default model and voice per provider
        self.model = model or self.DEFAULT_MODELS.get(provider, "default")
        self.default_voice = default_voice or self.DEFAULT_VOICES.get(provider)

        # Initialize provider-specific client
        self._client = None
        self._initialized = False

        # Cache for voices
        self._voices_cache: Optional[List[VoiceInfo]] = None
        self._cache_timestamp: float = 0

        # Ensure temp directories exist
        self._ensure_temp_dirs()

    def _ensure_temp_dirs(self):
        """Ensure temp directories exist (Docker-compatible)."""
        for dir_path in [TEMP_DIR, APP_TEMP_DIR]:
            try:
                Path(dir_path).mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError):
                # In Docker, these might already exist or need root
                pass

    async def _ensure_initialized(self):
        """Ensure provider client is initialized."""
        if self._initialized:
            return

        if self.provider == TTSProvider.OPENAI:
            # Would initialize OpenAI client
            pass
        elif self.provider == TTSProvider.ELEVENLABS:
            # Would initialize ElevenLabs client
            pass
        elif self.provider == TTSProvider.EDGE:
            # Would initialize Edge TTS client
            pass
        elif self.provider == TTSProvider.GOOGLE:
            # Would initialize Google Cloud TTS client
            pass
        elif self.provider == TTSProvider.AMAZON:
            # Would initialize Amazon Polly client
            pass

        self._initialized = True

    async def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        format: Optional[AudioFormat] = None,
        speed: float = 1.0
    ) -> bytes:
        """
        Synthesize text to speech.

        Args:
            text: Text to synthesize
            voice: Voice ID (uses default if not specified)
            format: Output audio format
            speed: Speech speed multiplier (0.5 to 2.0)

        Returns:
            Audio bytes in the specified format
        """
        await self._ensure_initialized()

        voice = voice or self.default_voice
        format = format or AudioFormat.MP3
        speed = max(0.5, min(2.0, speed))  # Clamp speed

        logger.info(f"Synthesizing {len(text)} chars with voice {voice}")

        # Provider-specific synthesis
        if self.provider == TTSProvider.OPENAI:
            return await self._synthesize_openai(text, voice, format, speed)
        elif self.provider == TTSProvider.ELEVENLABS:
            return await self._synthesize_elevenlabs(text, voice, format)
        elif self.provider == TTSProvider.EDGE:
            return await self._synthesize_edge(text, voice, format, speed)
        elif self.provider == TTSProvider.GOOGLE:
            return await self._synthesize_google(text, voice, format, speed)
        elif self.provider == TTSProvider.AMAZON:
            return await self._synthesize_amazon(text, voice, format)

        # Placeholder - actual implementation would call provider API
        return b""

    async def _synthesize_openai(
        self,
        text: str,
        voice: str,
        format: AudioFormat,
        speed: float
    ) -> bytes:
        """Synthesize using OpenAI TTS."""
        # Would use OpenAI API:
        # response = await self._client.audio.speech.create(
        #     model=self.model,
        #     voice=voice,
        #     input=text,
        #     response_format=format.value,
        #     speed=speed
        # )
        # return response.content
        return b""

    async def _synthesize_elevenlabs(
        self,
        text: str,
        voice: str,
        format: AudioFormat
    ) -> bytes:
        """Synthesize using ElevenLabs."""
        # Would use ElevenLabs API
        return b""

    async def _synthesize_edge(
        self,
        text: str,
        voice: str,
        format: AudioFormat,
        speed: float
    ) -> bytes:
        """Synthesize using Microsoft Edge TTS (free)."""
        # Would use edge-tts library
        return b""

    async def _synthesize_google(
        self,
        text: str,
        voice: str,
        format: AudioFormat,
        speed: float
    ) -> bytes:
        """Synthesize using Google Cloud TTS."""
        # Would use Google Cloud TTS API
        return b""

    async def _synthesize_amazon(
        self,
        text: str,
        voice: str,
        format: AudioFormat
    ) -> bytes:
        """Synthesize using Amazon Polly."""
        # Would use AWS Polly
        return b""

    async def synthesize_ssml(
        self,
        ssml: str,
        voice: Optional[str] = None,
        format: Optional[AudioFormat] = None
    ) -> bytes:
        """
        Synthesize SSML to speech.

        Args:
            ssml: SSML markup to synthesize
            voice: Voice ID (uses default if not specified)
            format: Output audio format

        Returns:
            Audio bytes in the specified format
        """
        await self._ensure_initialized()

        voice = voice or self.default_voice
        format = format or AudioFormat.MP3

        logger.info(f"Synthesizing SSML with voice {voice}")

        # Provider-specific SSML synthesis
        # Most providers support SSML with varying feature sets

        if self.provider == TTSProvider.OPENAI:
            # OpenAI doesn't support SSML directly, would need to parse and apply
            # Extract text and apply parameters where possible
            pass
        elif self.provider in [TTSProvider.GOOGLE, TTSProvider.AMAZON, TTSProvider.EDGE]:
            # These providers support SSML natively
            pass

        # Placeholder - actual implementation would call provider API
        return b""

    async def list_voices(
        self,
        language: Optional[str] = None,
        gender: Optional[str] = None,
        use_cache: bool = True
    ) -> List[VoiceInfo]:
        """
        List available voices.

        Args:
            language: Filter by language code (e.g., "en-US")
            gender: Filter by gender ("male", "female", "neutral")
            use_cache: Whether to use cached voice list

        Returns:
            List of available voices
        """
        await self._ensure_initialized()

        import time

        # Check cache
        if use_cache and self._voices_cache is not None:
            cache_age = time.time() - self._cache_timestamp
            if cache_age < 3600:  # 1 hour cache
                voices = self._voices_cache
                return self._filter_voices(voices, language, gender)

        # Fetch voices from provider
        voices = await self._fetch_voices()

        # Update cache
        self._voices_cache = voices
        self._cache_timestamp = time.time()

        return self._filter_voices(voices, language, gender)

    async def _fetch_voices(self) -> List[VoiceInfo]:
        """Fetch available voices from provider."""
        voices = []

        if self.provider == TTSProvider.OPENAI:
            # OpenAI has fixed voices
            voices = [
                VoiceInfo(id="alloy", name="Alloy", language="en-US", gender="neutral", provider="openai"),
                VoiceInfo(id="echo", name="Echo", language="en-US", gender="male", provider="openai"),
                VoiceInfo(id="fable", name="Fable", language="en-US", gender="neutral", provider="openai"),
                VoiceInfo(id="onyx", name="Onyx", language="en-US", gender="male", provider="openai"),
                VoiceInfo(id="nova", name="Nova", language="en-US", gender="female", provider="openai"),
                VoiceInfo(id="shimmer", name="Shimmer", language="en-US", gender="female", provider="openai"),
            ]
        elif self.provider == TTSProvider.ELEVENLABS:
            # Would fetch from ElevenLabs API
            pass
        elif self.provider == TTSProvider.EDGE:
            # Would fetch from Edge TTS
            pass
        elif self.provider == TTSProvider.GOOGLE:
            # Would fetch from Google Cloud TTS API
            pass
        elif self.provider == TTSProvider.AMAZON:
            # Would fetch from Amazon Polly
            pass

        return voices

    def _filter_voices(
        self,
        voices: List[VoiceInfo],
        language: Optional[str],
        gender: Optional[str]
    ) -> List[VoiceInfo]:
        """Filter voices by criteria."""
        filtered = voices

        if language:
            filtered = [v for v in filtered if v.language.lower().startswith(language.lower())]

        if gender:
            filtered = [v for v in filtered if v.gender and v.gender.lower() == gender.lower()]

        return filtered

    def get_optimal_format(self, channel: str) -> str:
        """
        Get optimal audio format for a channel.

        Args:
            channel: Channel name (telegram, discord, etc.)

        Returns:
            Optimal format string (opus, mp3, wav, ogg)
        """
        format_enum = self.CHANNEL_FORMATS.get(
            channel.lower(),
            self.CHANNEL_FORMATS["default"]
        )
        return format_enum.value

    def get_supported_formats(self) -> List[str]:
        """Get list of supported output formats."""
        formats = {
            TTSProvider.OPENAI: ["mp3", "opus", "aac", "flac", "wav", "pcm"],
            TTSProvider.ELEVENLABS: ["mp3", "wav", "ogg"],
            TTSProvider.EDGE: ["mp3", "wav", "ogg"],
            TTSProvider.GOOGLE: ["mp3", "wav", "ogg"],
            TTSProvider.AMAZON: ["mp3", "ogg", "pcm"]
        }
        return formats.get(self.provider, ["mp3", "wav"])

    def get_max_text_length(self) -> int:
        """Get maximum text length for single request."""
        limits = {
            TTSProvider.OPENAI: 4096,
            TTSProvider.ELEVENLABS: 5000,
            TTSProvider.EDGE: 10000,
            TTSProvider.GOOGLE: 5000,
            TTSProvider.AMAZON: 3000
        }
        return limits.get(self.provider, 4096)

    async def synthesize_long_text(
        self,
        text: str,
        voice: Optional[str] = None,
        format: Optional[AudioFormat] = None
    ) -> bytes:
        """
        Synthesize long text by chunking.

        Args:
            text: Text to synthesize (can exceed max length)
            voice: Voice ID
            format: Output audio format

        Returns:
            Combined audio bytes
        """
        max_length = self.get_max_text_length()

        if len(text) <= max_length:
            return await self.synthesize(text, voice, format)

        # Split text into chunks at sentence boundaries
        chunks = self._split_text(text, max_length)

        # Synthesize each chunk
        audio_parts = []
        for chunk in chunks:
            audio = await self.synthesize(chunk, voice, format)
            audio_parts.append(audio)

        # Combine audio parts
        return self._combine_audio(audio_parts, format or AudioFormat.MP3)

    def _split_text(self, text: str, max_length: int) -> List[str]:
        """Split text into chunks at sentence boundaries."""
        sentences = []
        current = ""

        # Simple sentence splitting
        for char in text:
            current += char
            if char in ".!?" and len(current) > 0:
                sentences.append(current.strip())
                current = ""

        if current.strip():
            sentences.append(current.strip())

        # Combine sentences into chunks
        chunks = []
        current_chunk = ""

        for sentence in sentences:
            if len(current_chunk) + len(sentence) + 1 <= max_length:
                current_chunk += (" " if current_chunk else "") + sentence
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = sentence

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _combine_audio(self, parts: List[bytes], format: AudioFormat) -> bytes:
        """Combine multiple audio parts."""
        # For MP3/OGG, simple concatenation often works
        # For WAV, would need proper header handling
        if format in [AudioFormat.MP3, AudioFormat.OGG, AudioFormat.OPUS]:
            return b"".join(parts)

        # For WAV, would need proper combination
        # This is a placeholder - real implementation would use audio library
        return b"".join(parts)

    async def save_to_file(
        self,
        text: str,
        file_path: str,
        voice: Optional[str] = None,
        format: Optional[AudioFormat] = None
    ) -> str:
        """
        Synthesize and save to file.

        Args:
            text: Text to synthesize
            file_path: Output file path
            voice: Voice ID
            format: Output audio format

        Returns:
            Path to saved file
        """
        audio = await self.synthesize(text, voice, format)

        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, 'wb') as f:
            f.write(audio)

        return str(path)

    def build_ssml(
        self,
        text: str,
        config: Optional[SSMLConfig] = None
    ) -> str:
        """
        Build SSML from text and configuration.

        Args:
            text: Plain text
            config: SSML configuration options

        Returns:
            SSML markup string
        """
        config = config or SSMLConfig()

        ssml_parts = ['<speak>']

        # Add prosody if configured
        prosody_attrs = []
        if config.rate:
            prosody_attrs.append(f'rate="{config.rate}"')
        if config.pitch:
            prosody_attrs.append(f'pitch="{config.pitch}"')
        if config.volume:
            prosody_attrs.append(f'volume="{config.volume}"')

        if prosody_attrs:
            ssml_parts.append(f'<prosody {" ".join(prosody_attrs)}>')

        # Add emphasis if configured
        if config.emphasis:
            ssml_parts.append(f'<emphasis level="{config.emphasis}">')
            ssml_parts.append(text)
            ssml_parts.append('</emphasis>')
        else:
            ssml_parts.append(text)

        if prosody_attrs:
            ssml_parts.append('</prosody>')

        ssml_parts.append('</speak>')

        return "".join(ssml_parts)

    def estimate_duration(self, text: str, speed: float = 1.0) -> float:
        """
        Estimate audio duration for text.

        Args:
            text: Text to estimate
            speed: Speech speed multiplier

        Returns:
            Estimated duration in seconds
        """
        # Average speaking rate is ~150 words per minute
        words = len(text.split())
        base_duration = (words / 150) * 60  # seconds
        return base_duration / speed

    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about the current provider."""
        return {
            "provider": self.provider.value,
            "model": self.model,
            "default_voice": self.default_voice,
            "max_text_length": self.get_max_text_length(),
            "supported_formats": self.get_supported_formats(),
            "ssml_support": self.provider in [
                TTSProvider.GOOGLE,
                TTSProvider.AMAZON,
                TTSProvider.EDGE
            ]
        }
