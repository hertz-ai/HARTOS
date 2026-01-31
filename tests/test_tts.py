"""
Tests for Text-to-Speech System.

Tests the TTSEngine class and related functionality.
"""

import pytest
import asyncio
import os
import sys
from unittest.mock import Mock, patch, AsyncMock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.media.tts import (
    TTSProvider,
    TTSEngine,
    VoiceInfo,
    SynthesisResult,
    SSMLConfig,
    AudioFormat,
)


class TestTTSProvider:
    """Tests for TTSProvider enum."""

    def test_all_providers_defined(self):
        """Test all expected providers are defined."""
        assert TTSProvider.OPENAI.value == "openai"
        assert TTSProvider.ELEVENLABS.value == "elevenlabs"
        assert TTSProvider.EDGE.value == "edge"
        assert TTSProvider.GOOGLE.value == "google"
        assert TTSProvider.AMAZON.value == "amazon"

    def test_provider_from_string(self):
        """Test creating provider from string."""
        assert TTSProvider("openai") == TTSProvider.OPENAI
        assert TTSProvider("elevenlabs") == TTSProvider.ELEVENLABS


class TestAudioFormat:
    """Tests for AudioFormat enum."""

    def test_all_formats_defined(self):
        """Test all audio formats are defined."""
        formats = [f.value for f in AudioFormat]
        assert "mp3" in formats
        assert "opus" in formats
        assert "wav" in formats
        assert "ogg" in formats


class TestVoiceInfo:
    """Tests for VoiceInfo dataclass."""

    def test_voice_info_creation(self):
        """Test creating VoiceInfo."""
        voice = VoiceInfo(
            id="test-voice",
            name="Test Voice",
            language="en-US",
            gender="female",
            description="A test voice"
        )

        assert voice.id == "test-voice"
        assert voice.name == "Test Voice"
        assert voice.language == "en-US"
        assert voice.gender == "female"

    def test_voice_info_to_dict(self):
        """Test VoiceInfo serialization."""
        voice = VoiceInfo(
            id="voice-1",
            name="Voice One",
            language="en-US"
        )

        data = voice.to_dict()
        assert data["id"] == "voice-1"
        assert data["name"] == "Voice One"
        assert data["language"] == "en-US"

    def test_voice_info_defaults(self):
        """Test VoiceInfo default values."""
        voice = VoiceInfo(
            id="v1",
            name="Voice",
            language="en"
        )

        assert voice.gender is None
        assert voice.styles == []
        assert voice.sample_rate == 24000
        assert voice.metadata == {}


class TestSSMLConfig:
    """Tests for SSMLConfig dataclass."""

    def test_ssml_config_creation(self):
        """Test creating SSMLConfig."""
        config = SSMLConfig(
            rate="medium",
            pitch="high",
            volume="loud"
        )

        assert config.rate == "medium"
        assert config.pitch == "high"
        assert config.volume == "loud"

    def test_ssml_config_to_dict(self):
        """Test SSMLConfig serialization."""
        config = SSMLConfig(rate="fast", emphasis="strong")
        data = config.to_dict()

        assert data["rate"] == "fast"
        assert data["emphasis"] == "strong"
        assert data["pitch"] is None


class TestTTSEngine:
    """Tests for TTSEngine class."""

    @pytest.fixture
    def engine(self):
        """Create TTS engine for testing."""
        return TTSEngine(provider=TTSProvider.OPENAI)

    @pytest.fixture
    def engine_elevenlabs(self):
        """Create ElevenLabs TTS engine."""
        return TTSEngine(provider=TTSProvider.ELEVENLABS)

    def test_engine_initialization(self, engine):
        """Test engine initialization."""
        assert engine.provider == TTSProvider.OPENAI
        assert engine.model == "tts-1"
        assert engine.default_voice == "alloy"

    def test_engine_initialization_from_string(self):
        """Test engine initialization from string."""
        engine = TTSEngine(provider="openai")
        assert engine.provider == TTSProvider.OPENAI

    def test_engine_custom_config(self):
        """Test engine with custom configuration."""
        engine = TTSEngine(
            provider=TTSProvider.ELEVENLABS,
            api_key="test-key",
            model="custom-model",
            default_voice="custom-voice"
        )

        assert engine.api_key == "test-key"
        assert engine.model == "custom-model"
        assert engine.default_voice == "custom-voice"

    def test_get_optimal_format(self, engine):
        """Test getting optimal format for channels."""
        assert engine.get_optimal_format("telegram") == "ogg"
        assert engine.get_optimal_format("discord") == "opus"
        assert engine.get_optimal_format("slack") == "mp3"
        assert engine.get_optimal_format("unknown") == "mp3"

    def test_get_supported_formats(self, engine):
        """Test getting supported formats."""
        formats = engine.get_supported_formats()
        assert isinstance(formats, list)
        assert "mp3" in formats

    def test_get_max_text_length(self, engine):
        """Test getting max text length."""
        assert engine.get_max_text_length() == 4096

    def test_estimate_duration(self, engine):
        """Test duration estimation."""
        # 150 words = 1 minute = 60 seconds
        text = " ".join(["word"] * 150)
        duration = engine.estimate_duration(text)
        assert 55 <= duration <= 65  # ~60 seconds

        # Half speed = double duration
        duration_slow = engine.estimate_duration(text, speed=0.5)
        assert duration_slow > duration

    def test_build_ssml_basic(self, engine):
        """Test basic SSML building."""
        ssml = engine.build_ssml("Hello world")
        assert "<speak>" in ssml
        assert "</speak>" in ssml
        assert "Hello world" in ssml

    def test_build_ssml_with_config(self, engine):
        """Test SSML building with configuration."""
        config = SSMLConfig(rate="fast", pitch="high")
        ssml = engine.build_ssml("Test text", config)

        assert "<prosody" in ssml
        assert 'rate="fast"' in ssml
        assert 'pitch="high"' in ssml

    def test_build_ssml_with_emphasis(self, engine):
        """Test SSML building with emphasis."""
        config = SSMLConfig(emphasis="strong")
        ssml = engine.build_ssml("Important", config)

        assert '<emphasis level="strong">' in ssml
        assert "Important" in ssml

    def test_split_text(self, engine):
        """Test text splitting for long texts."""
        long_text = "First sentence. Second sentence. Third sentence!"
        chunks = engine._split_text(long_text, max_length=30)

        assert len(chunks) > 1
        # All text should be preserved
        combined = " ".join(chunks)
        assert "First sentence" in combined
        assert "Third sentence" in combined

    def test_get_provider_info(self, engine):
        """Test getting provider information."""
        info = engine.get_provider_info()

        assert info["provider"] == "openai"
        assert info["model"] == "tts-1"
        assert "max_text_length" in info
        assert "supported_formats" in info
        assert "ssml_support" in info

    @pytest.mark.asyncio
    async def test_synthesize_basic(self, engine):
        """Test basic synthesis call."""
        # This calls the placeholder implementation
        result = await engine.synthesize("Hello world")
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_synthesize_with_options(self, engine):
        """Test synthesis with options."""
        result = await engine.synthesize(
            "Hello world",
            voice="nova",
            format=AudioFormat.MP3,
            speed=1.5
        )
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_synthesize_ssml(self, engine):
        """Test SSML synthesis."""
        ssml = "<speak>Hello <emphasis>world</emphasis></speak>"
        result = await engine.synthesize_ssml(ssml)
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_list_voices(self, engine):
        """Test listing voices."""
        voices = await engine.list_voices()
        assert isinstance(voices, list)

        # OpenAI has predefined voices
        if engine.provider == TTSProvider.OPENAI:
            assert len(voices) > 0
            assert all(isinstance(v, VoiceInfo) for v in voices)

    @pytest.mark.asyncio
    async def test_list_voices_with_filter(self, engine):
        """Test listing voices with filters."""
        voices = await engine.list_voices(language="en-US")
        assert isinstance(voices, list)

        voices_female = await engine.list_voices(gender="female")
        assert isinstance(voices_female, list)

    @pytest.mark.asyncio
    async def test_synthesize_long_text(self, engine):
        """Test synthesizing long text."""
        # Create text longer than max length
        long_text = ". ".join(["This is a sentence"] * 500)
        result = await engine.synthesize_long_text(long_text)
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_save_to_file(self, engine, tmp_path):
        """Test saving synthesis to file."""
        file_path = str(tmp_path / "test_audio.mp3")
        result = await engine.save_to_file(
            "Hello world",
            file_path
        )

        assert result == file_path
        assert Path(file_path).exists()


class TestTTSEngineProviders:
    """Tests for different TTS providers."""

    def test_openai_defaults(self):
        """Test OpenAI provider defaults."""
        engine = TTSEngine(provider=TTSProvider.OPENAI)
        assert engine.model == "tts-1"
        assert engine.default_voice == "alloy"

    def test_elevenlabs_defaults(self):
        """Test ElevenLabs provider defaults."""
        engine = TTSEngine(provider=TTSProvider.ELEVENLABS)
        assert engine.model == "eleven_monolingual_v1"
        assert engine.default_voice is not None

    def test_edge_defaults(self):
        """Test Edge TTS provider defaults."""
        engine = TTSEngine(provider=TTSProvider.EDGE)
        assert engine.model == "neural"
        assert "Neural" in engine.default_voice

    def test_google_defaults(self):
        """Test Google TTS provider defaults."""
        engine = TTSEngine(provider=TTSProvider.GOOGLE)
        assert engine.model == "standard"

    def test_amazon_defaults(self):
        """Test Amazon Polly provider defaults."""
        engine = TTSEngine(provider=TTSProvider.AMAZON)
        assert engine.model == "neural"
        assert engine.default_voice == "Joanna"


class TestTTSIntegration:
    """Integration tests for TTS system."""

    @pytest.mark.asyncio
    async def test_full_synthesis_workflow(self, tmp_path):
        """Test complete synthesis workflow."""
        engine = TTSEngine(provider=TTSProvider.OPENAI)

        # List available voices
        voices = await engine.list_voices()

        # Get optimal format for channel
        format_str = engine.get_optimal_format("telegram")

        # Build SSML
        config = SSMLConfig(rate="medium")
        ssml = engine.build_ssml("Hello, how are you?", config)

        # Estimate duration
        duration = engine.estimate_duration("Hello, how are you?")

        # Synthesize
        audio = await engine.synthesize("Hello, how are you?")

        assert isinstance(audio, bytes)

    @pytest.mark.asyncio
    async def test_voice_caching(self):
        """Test that voice list is cached."""
        engine = TTSEngine(provider=TTSProvider.OPENAI)

        # First call
        voices1 = await engine.list_voices()

        # Second call should use cache
        voices2 = await engine.list_voices(use_cache=True)

        assert voices1 == voices2

    @pytest.mark.asyncio
    async def test_voice_filtering(self):
        """Test voice filtering functionality."""
        engine = TTSEngine(provider=TTSProvider.OPENAI)

        all_voices = await engine.list_voices()
        female_voices = await engine.list_voices(gender="female")

        # Should have filtered some voices
        assert len(female_voices) <= len(all_voices)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
