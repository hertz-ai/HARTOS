"""
Text-to-Speech System for audio synthesis.

Active providers: LuxTTS (offline, 24kHz, voice cloning), Pocket TTS (offline, CPU, MIT).
Cloud providers (openai, elevenlabs, edge, google, amazon) are disabled — HART OS is
offline-first with no closed-source TTS dependencies.
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
    LUXTTS = "luxtts"                # Offline: LuxTTS 24kHz — GPU/CPU, voice cloning, Apache 2.0
    POCKET = "pocket"                # Offline: Pocket TTS (Kyutai) — 100M params, CPU, MIT
    CHATTERBOX = "chatterbox"        # GPU: English, emotional, voice cloning, 3.8GB VRAM
    CHATTERBOX_ML = "chatterbox_ml"  # GPU: 23 languages, voice cloning, 12GB VRAM
    COSYVOICE = "cosyvoice"          # GPU: 9 languages (zh/ja/ko/de/es/fr/it/ru/en), 3.5GB
    F5 = "f5_tts"                    # GPU: English + Chinese, voice cloning, 1.3GB VRAM
    INDIC_PARLER = "indic_parler"    # GPU: 22 Indic languages + English, 1.8GB VRAM
    ESPEAK = "espeak"                # CPU: 100+ languages, robotic quality, instant
    # Cloud providers — kept for config compatibility, disabled at runtime
    OPENAI = "openai"           # Disabled: closed-source cloud API
    ELEVENLABS = "elevenlabs"   # Disabled: closed-source cloud API
    EDGE = "edge"               # Disabled: closed-source cloud API
    GOOGLE = "google"           # Disabled: closed-source cloud API
    AMAZON = "amazon"           # Disabled: closed-source cloud API


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
        TTSProvider.LUXTTS: "default",
        TTSProvider.POCKET: "alba",
        TTSProvider.OPENAI: "alloy",
        TTSProvider.ELEVENLABS: "21m00Tcm4TlvDq8ikWAM",  # Rachel
        TTSProvider.EDGE: "en-US-AriaNeural",
        TTSProvider.GOOGLE: "en-US-Standard-A",
        TTSProvider.AMAZON: "Joanna"
    }

    # Models per provider
    DEFAULT_MODELS = {
        TTSProvider.LUXTTS: "luxtts-48k",
        TTSProvider.POCKET: "pocket-100m",
        TTSProvider.OPENAI: "tts-1",
        TTSProvider.ELEVENLABS: "eleven_monolingual_v1",
        TTSProvider.EDGE: "neural",
        TTSProvider.GOOGLE: "standard",
        TTSProvider.AMAZON: "neural"
    }

    def __init__(
        self,
        provider: Union[TTSProvider, str] = TTSProvider.POCKET,
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

        if self.provider in (TTSProvider.OPENAI, TTSProvider.ELEVENLABS,
                             TTSProvider.EDGE, TTSProvider.GOOGLE,
                             TTSProvider.AMAZON):
            logger.info("%s provider selected but disabled (closed-source). "
                        "Synthesis calls will return empty audio.", self.provider.value)

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
        if self.provider == TTSProvider.LUXTTS:
            return await self._synthesize_luxtts(text, voice, format, speed)
        elif self.provider == TTSProvider.POCKET:
            return await self._synthesize_pocket(text, voice, format, speed)
        elif self.provider in (TTSProvider.OPENAI, TTSProvider.ELEVENLABS,
                               TTSProvider.EDGE, TTSProvider.GOOGLE,
                               TTSProvider.AMAZON):
            return await self._synthesize_cloud_disabled(self.provider.value)

        return b""

    async def _synthesize_luxtts(
        self,
        text: str,
        voice: str,
        format: AudioFormat,
        speed: float
    ) -> bytes:
        """Synthesize using LuxTTS (offline, GPU/CPU, 48kHz, voice cloning).

        Uses integrations.service_tools.luxtts_tool for actual synthesis,
        then reads the output WAV file and returns raw bytes.
        """
        import json as _json
        try:
            from integrations.service_tools.luxtts_tool import luxtts_synthesize
            result = _json.loads(luxtts_synthesize(
                text,
                voice_audio=voice if voice != "default" else None,
                speed=speed,
            ))
            if 'error' in result:
                logger.warning(f"LuxTTS error: {result['error']}")
                return b""
            wav_path = result.get('path', '')
            if wav_path and os.path.isfile(wav_path):
                with open(wav_path, 'rb') as f:
                    return f.read()
            return b""
        except ImportError:
            logger.warning("luxtts_tool not available")
            return b""
        except Exception as e:
            logger.warning(f"LuxTTS synthesis failed: {e}")
            return b""

    async def _synthesize_pocket(
        self,
        text: str,
        voice: str,
        format: AudioFormat,
        speed: float
    ) -> bytes:
        """Synthesize using Pocket TTS (offline, CPU, 100M params).

        Uses integrations.service_tools.pocket_tts_tool for actual synthesis,
        then reads the output WAV file and returns raw bytes.
        """
        import json as _json
        try:
            from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
            result = _json.loads(pocket_tts_synthesize(text, voice))
            if 'error' in result:
                logger.warning(f"Pocket TTS error: {result['error']}")
                return b""
            wav_path = result.get('path', '')
            if wav_path and os.path.isfile(wav_path):
                with open(wav_path, 'rb') as f:
                    audio_bytes = f.read()
                # WAV is native format; convert if needed
                if format == AudioFormat.WAV:
                    return audio_bytes
                # For other formats, return WAV (caller can convert)
                return audio_bytes
            return b""
        except ImportError:
            logger.warning("pocket_tts_tool not available")
            return b""
        except Exception as e:
            logger.warning(f"Pocket TTS synthesis failed: {e}")
            return b""

    async def _synthesize_cloud_disabled(self, provider_name: str) -> bytes:
        """Return empty bytes for disabled cloud TTS providers.

        HART OS is offline-first — no closed-source TTS APIs.
        Use TTSProvider.POCKET or TTSProvider.LUXTTS instead.
        """
        logger.warning(
            "%s TTS is disabled (closed-source cloud API). "
            "Use POCKET or LUXTTS for offline synthesis.", provider_name
        )
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

        if self.provider in (TTSProvider.LUXTTS, TTSProvider.POCKET):
            # LuxTTS and Pocket TTS don't support SSML — strip tags, synthesize plain text
            import re
            plain = re.sub(r'<[^>]+>', '', ssml).strip()
            if plain:
                return await self.synthesize(plain, voice, format)
            return b""

        # Cloud providers: disabled
        logger.warning("SSML synthesis not available (cloud providers disabled)")
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

        if self.provider == TTSProvider.LUXTTS:
            # LuxTTS cloned voices
            try:
                import json as _json
                from integrations.service_tools.luxtts_tool import luxtts_list_voices
                data = _json.loads(luxtts_list_voices())
                for v in data.get('voices', []):
                    voices.append(VoiceInfo(
                        id=v['id'], name=v['name'], language="en",
                        provider="luxtts", sample_rate=24000,
                        metadata={"type": v.get('type', 'cloned')},
                    ))
            except (ImportError, Exception):
                voices.append(VoiceInfo(
                    id="default", name="Default", language="en",
                    provider="luxtts", sample_rate=24000,
                ))
        elif self.provider == TTSProvider.POCKET:
            # Pocket TTS built-in + cloned voices
            try:
                import json as _json
                from integrations.service_tools.pocket_tts_tool import pocket_tts_list_voices
                data = _json.loads(pocket_tts_list_voices())
                for v in data.get('voices', []):
                    voices.append(VoiceInfo(
                        id=v['id'], name=v['name'], language="en",
                        provider="pocket", metadata={"type": v.get('type', 'builtin')},
                    ))
            except (ImportError, Exception):
                # Fallback: correct 8 built-in voices (pocket-tts 1.1.1)
                for name in ["alba", "marius", "javert", "jean",
                             "fantine", "cosette", "eponine", "azelma"]:
                    voices.append(VoiceInfo(
                        id=name, name=name.title(), language="en", provider="pocket",
                    ))
        # Cloud providers: disabled, no voices to list

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
            TTSProvider.LUXTTS: ["wav"],
            TTSProvider.POCKET: ["wav"],
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
            TTSProvider.LUXTTS: 10000,   # Local — no API limits, just memory
            TTSProvider.POCKET: 10000,   # Local — no API limits, just memory
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
        _cloud = (TTSProvider.OPENAI, TTSProvider.ELEVENLABS,
                  TTSProvider.EDGE, TTSProvider.GOOGLE, TTSProvider.AMAZON)
        return {
            "provider": self.provider.value,
            "model": self.model,
            "default_voice": self.default_voice,
            "max_text_length": self.get_max_text_length(),
            "supported_formats": self.get_supported_formats(),
            "ssml_support": False,  # No active provider supports SSML
            "offline": self.provider not in _cloud,
            "disabled": self.provider in _cloud,
        }
