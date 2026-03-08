"""
Tests for Image Generation System.

Tests the ImageGenerator class and related functionality.
"""

import pytest
import asyncio
import os
import sys
import time
from unittest.mock import Mock, patch, AsyncMock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.media.image_gen import (
    ImageProvider,
    ImageGenerator,
    ImageSize,
    ImageStyle,
    GeneratedImage,
    EditResult,
    VariationResult,
)


class TestImageProvider:
    """Tests for ImageProvider enum."""

    def test_all_providers_defined(self):
        """Test all expected providers are defined."""
        assert ImageProvider.OPENAI.value == "openai"
        assert ImageProvider.STABILITY.value == "stability"
        assert ImageProvider.MIDJOURNEY.value == "midjourney"

    def test_provider_from_string(self):
        """Test creating provider from string."""
        assert ImageProvider("openai") == ImageProvider.OPENAI
        assert ImageProvider("stability") == ImageProvider.STABILITY


class TestImageSize:
    """Tests for ImageSize enum."""

    def test_all_sizes_defined(self):
        """Test all standard sizes are defined."""
        assert ImageSize.SQUARE_SMALL.value == "256x256"
        assert ImageSize.SQUARE_MEDIUM.value == "512x512"
        assert ImageSize.SQUARE_LARGE.value == "1024x1024"
        assert ImageSize.LANDSCAPE.value == "1792x1024"
        assert ImageSize.PORTRAIT.value == "1024x1792"


class TestImageStyle:
    """Tests for ImageStyle enum."""

    def test_all_styles_defined(self):
        """Test all style presets are defined."""
        styles = [s.value for s in ImageStyle]
        assert "vivid" in styles
        assert "natural" in styles
        assert "photographic" in styles
        assert "digital-art" in styles


class TestGeneratedImage:
    """Tests for GeneratedImage dataclass."""

    def test_generated_image_creation(self):
        """Test creating GeneratedImage."""
        image = GeneratedImage(
            data=b"test image data",
            format="png",
            width=1024,
            height=1024,
            prompt="A test image"
        )

        assert image.data == b"test image data"
        assert image.format == "png"
        assert image.width == 1024
        assert image.height == 1024
        assert image.prompt == "A test image"

    def test_generated_image_to_dict(self):
        """Test GeneratedImage serialization."""
        image = GeneratedImage(
            data=b"test",
            format="png",
            width=512,
            height=512,
            prompt="Test",
            seed=12345
        )

        data = image.to_dict()
        assert data["format"] == "png"
        assert data["width"] == 512
        assert data["height"] == 512
        assert data["prompt"] == "Test"
        assert data["seed"] == 12345
        assert data["size"] == 4  # len(b"test")

    def test_generated_image_to_base64(self):
        """Test base64 conversion."""
        image = GeneratedImage(
            data=b"hello",
            format="png",
            width=100,
            height=100,
            prompt="Test"
        )

        b64 = image.to_base64()
        assert b64 == "aGVsbG8="  # base64 of "hello"

    def test_generated_image_to_data_url(self):
        """Test data URL generation."""
        image = GeneratedImage(
            data=b"test",
            format="png",
            width=100,
            height=100,
            prompt="Test"
        )

        url = image.to_data_url()
        assert url.startswith("data:image/png;base64,")

    def test_generated_image_defaults(self):
        """Test GeneratedImage default values."""
        image = GeneratedImage(
            data=b"",
            format="png",
            width=1024,
            height=1024,
            prompt="Test"
        )

        assert image.revised_prompt is None
        assert image.seed is None
        assert image.provider is None
        assert image.metadata == {}


class TestEditResult:
    """Tests for EditResult dataclass."""

    def test_edit_result_creation(self):
        """Test creating EditResult."""
        edited_image = GeneratedImage(
            data=b"edited",
            format="png",
            width=1024,
            height=1024,
            prompt="Edit prompt"
        )

        result = EditResult(
            original_size=1000,
            edited_size=1200,
            edited_image=edited_image,
            operation="edit"
        )

        assert result.original_size == 1000
        assert result.edited_size == 1200
        assert result.operation == "edit"

    def test_edit_result_to_dict(self):
        """Test EditResult serialization."""
        edited_image = GeneratedImage(
            data=b"x",
            format="png",
            width=512,
            height=512,
            prompt="Test"
        )

        result = EditResult(
            original_size=100,
            edited_size=200,
            edited_image=edited_image,
            operation="inpaint"
        )

        data = result.to_dict()
        assert data["original_size"] == 100
        assert data["edited_size"] == 200
        assert data["operation"] == "inpaint"
        assert "edited_image" in data


class TestVariationResult:
    """Tests for VariationResult dataclass."""

    def test_variation_result_creation(self):
        """Test creating VariationResult."""
        variations = [
            GeneratedImage(data=b"v1", format="png", width=512, height=512, prompt="Test"),
            GeneratedImage(data=b"v2", format="png", width=512, height=512, prompt="Test"),
        ]

        result = VariationResult(
            original_prompt="Original",
            variations=variations,
            count=2
        )

        assert result.original_prompt == "Original"
        assert len(result.variations) == 2
        assert result.count == 2

    def test_variation_result_to_dict(self):
        """Test VariationResult serialization."""
        result = VariationResult(
            original_prompt="Test",
            variations=[],
            count=0
        )

        data = result.to_dict()
        assert data["original_prompt"] == "Test"
        assert data["variations"] == []
        assert data["count"] == 0


class TestImageGenerator:
    """Tests for ImageGenerator class."""

    @pytest.fixture
    def generator(self):
        """Create image generator for testing."""
        return ImageGenerator(provider=ImageProvider.OPENAI)

    @pytest.fixture
    def generator_stability(self):
        """Create Stability AI generator."""
        return ImageGenerator(provider=ImageProvider.STABILITY)

    def test_generator_initialization(self, generator):
        """Test generator initialization."""
        assert generator.provider == ImageProvider.OPENAI
        assert generator.model == "dall-e-3"

    def test_generator_initialization_from_string(self):
        """Test generator initialization from string."""
        gen = ImageGenerator(provider="openai")
        assert gen.provider == ImageProvider.OPENAI

    def test_generator_custom_config(self):
        """Test generator with custom configuration."""
        gen = ImageGenerator(
            provider=ImageProvider.STABILITY,
            api_key="test-key",
            model="custom-model",
            config={"min_request_interval": 2.0}
        )

        assert gen.api_key == "test-key"
        assert gen.model == "custom-model"
        assert gen._min_request_interval == 2.0

    def test_providers_list(self, generator):
        """Test providers class attribute."""
        assert "openai" in generator.providers
        assert "stability" in generator.providers
        assert "midjourney" in generator.providers

    def test_get_supported_sizes(self, generator):
        """Test getting supported sizes."""
        sizes = generator.get_supported_sizes()
        assert isinstance(sizes, list)
        assert "1024x1024" in sizes

    def test_get_supported_sizes_stability(self, generator_stability):
        """Test Stability AI supported sizes."""
        sizes = generator_stability.get_supported_sizes()
        assert "512x512" in sizes

    def test_get_supported_styles(self, generator):
        """Test getting supported styles."""
        styles = generator.get_supported_styles()
        assert isinstance(styles, list)
        assert "vivid" in styles

    def test_get_max_prompt_length(self, generator):
        """Test getting max prompt length."""
        assert generator.get_max_prompt_length() == 4000

    def test_normalize_size(self, generator):
        """Test size normalization."""
        # Exact match
        assert generator._normalize_size("1024x1024") == "1024x1024"

        # ImageSize enum
        assert generator._normalize_size(ImageSize.SQUARE_LARGE) == "1024x1024"

        # Closest match
        normalized = generator._normalize_size("800x800")
        assert normalized in generator.get_supported_sizes()

    def test_estimate_cost(self, generator):
        """Test cost estimation."""
        cost = generator.estimate_cost(size="1024x1024", quality="standard", n=1)
        assert cost > 0

        cost_hd = generator.estimate_cost(size="1024x1024", quality="hd", n=1)
        assert cost_hd > cost

        cost_multiple = generator.estimate_cost(size="1024x1024", n=4)
        assert cost_multiple == cost * 4

    def test_get_provider_info(self, generator):
        """Test getting provider information."""
        info = generator.get_provider_info()

        assert info["provider"] == "openai"
        assert info["model"] == "dall-e-3"
        assert "supported_sizes" in info
        assert "supported_styles" in info
        assert info["supports_edit"] is True
        assert info["supports_variations"] is True

    def test_get_temp_path(self, generator):
        """Test temporary path generation."""
        path1 = generator.get_temp_path()
        time.sleep(0.002)  # Ensure different millisecond timestamp
        path2 = generator.get_temp_path()

        # Should be unique
        assert path1 != path2
        assert path1.endswith(".png")
        assert "/tmp/images" in path1 or "\\tmp\\images" in path1

    def test_get_temp_path_with_prefix(self, generator):
        """Test temp path with custom prefix."""
        path = generator.get_temp_path(prefix="custom")
        assert "custom_" in path

    @pytest.mark.asyncio
    async def test_generate_basic(self, generator):
        """Test basic image generation."""
        result = await generator.generate("A beautiful sunset")
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_generate_with_options(self, generator):
        """Test generation with options."""
        result = await generator.generate(
            "A mountain landscape",
            size="1024x1024",
            style=ImageStyle.VIVID,
            quality="hd"
        )
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_generate_multiple(self, generator):
        """Test generating multiple images."""
        results = await generator.generate_multiple(
            "A colorful abstract painting",
            n=2,
            size="512x512"
        )
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_edit_image(self, generator):
        """Test image editing."""
        original = b"original image data"
        result = await generator.edit(
            image=original,
            prompt="Add a rainbow",
            size="1024x1024"
        )
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_edit_with_mask(self, generator):
        """Test image editing with mask."""
        original = b"original image"
        mask = b"mask data"
        result = await generator.edit(
            image=original,
            prompt="Replace the sky",
            mask=mask
        )
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_variations(self, generator):
        """Test generating variations."""
        original = b"original image"
        results = await generator.variations(
            image=original,
            n=2,
            size="1024x1024"
        )
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_upscale(self, generator_stability):
        """Test image upscaling."""
        original = b"small image"
        result = await generator_stability.upscale(original, scale=2)
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_save_to_file(self, generator, tmp_path):
        """Test saving generated image to file."""
        file_path = str(tmp_path / "test_image.png")
        result = await generator.save_to_file(
            "A test image",
            file_path,
            size="512x512"
        )

        assert result == file_path
        assert Path(file_path).exists()


class TestImageGeneratorProviders:
    """Tests for different image providers."""

    def test_openai_defaults(self):
        """Test OpenAI provider defaults."""
        gen = ImageGenerator(provider=ImageProvider.OPENAI)
        assert gen.model == "dall-e-3"
        assert "1024x1024" in gen.get_supported_sizes()

    def test_stability_defaults(self):
        """Test Stability AI provider defaults."""
        gen = ImageGenerator(provider=ImageProvider.STABILITY)
        assert "stable-diffusion" in gen.model.lower()

    def test_midjourney_defaults(self):
        """Test Midjourney provider defaults."""
        gen = ImageGenerator(provider=ImageProvider.MIDJOURNEY)
        assert gen.model == "v6"


class TestImageGeneratorIntegration:
    """Integration tests for image generation system."""

    @pytest.mark.asyncio
    async def test_full_generation_workflow(self, tmp_path):
        """Test complete generation workflow."""
        gen = ImageGenerator(provider=ImageProvider.OPENAI)

        # Check provider info
        info = gen.get_provider_info()
        assert info["provider"] == "openai"

        # Get supported sizes
        sizes = gen.get_supported_sizes()
        assert len(sizes) > 0

        # Estimate cost
        cost = gen.estimate_cost(size="1024x1024", n=1)
        assert cost > 0

        # Generate image
        image_data = await gen.generate(
            "A futuristic city at night",
            size="1024x1024"
        )
        assert isinstance(image_data, bytes)

    @pytest.mark.asyncio
    async def test_edit_workflow(self):
        """Test image editing workflow."""
        gen = ImageGenerator(provider=ImageProvider.OPENAI)

        # Verify provider supports editing
        info = gen.get_provider_info()
        assert info["supports_edit"] is True

        # Edit image
        original = b"original image bytes"
        edited = await gen.edit(original, "Make it brighter")
        assert isinstance(edited, bytes)

    @pytest.mark.asyncio
    async def test_variation_workflow(self):
        """Test variation generation workflow."""
        gen = ImageGenerator(provider=ImageProvider.OPENAI)

        # Verify provider supports variations
        info = gen.get_provider_info()
        assert info["supports_variations"] is True

        # Generate variations
        original = b"original image"
        variations = await gen.variations(original, n=3)
        assert isinstance(variations, list)

    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        """Test rate limiting between requests."""
        gen = ImageGenerator(
            provider=ImageProvider.OPENAI,
            config={"min_request_interval": 0.1}
        )

        import time
        start = time.time()

        await gen.generate("Test 1")
        await gen.generate("Test 2")

        elapsed = time.time() - start
        # Should have waited at least min_request_interval between calls
        assert elapsed >= 0.1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
