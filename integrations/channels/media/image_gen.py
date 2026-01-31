"""
Image Generator for AI image generation.

Supports multiple providers: openai, stability, midjourney
"""

import asyncio
import base64
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
import logging
import os
import time
import hashlib

logger = logging.getLogger(__name__)

# Docker-compatible paths
TEMP_DIR = os.environ.get("IMAGE_TEMP_DIR", "/tmp/images")
APP_TEMP_DIR = os.environ.get("APP_TEMP_DIR", "/app/temp")


class ImageProvider(Enum):
    """Supported image generation providers."""
    OPENAI = "openai"
    STABILITY = "stability"
    MIDJOURNEY = "midjourney"


class ImageSize(Enum):
    """Standard image sizes."""
    SQUARE_SMALL = "256x256"
    SQUARE_MEDIUM = "512x512"
    SQUARE_LARGE = "1024x1024"
    LANDSCAPE = "1792x1024"
    PORTRAIT = "1024x1792"
    HD_LANDSCAPE = "1920x1080"
    HD_PORTRAIT = "1080x1920"


class ImageStyle(Enum):
    """Image style presets."""
    VIVID = "vivid"
    NATURAL = "natural"
    ANIME = "anime"
    PHOTOGRAPHIC = "photographic"
    DIGITAL_ART = "digital-art"
    CINEMATIC = "cinematic"
    FANTASY = "fantasy"
    NEON_PUNK = "neon-punk"
    ISOMETRIC = "isometric"
    ORIGAMI = "origami"


@dataclass
class GeneratedImage:
    """A generated image result."""
    data: bytes
    format: str  # png, jpg, webp
    width: int
    height: int
    prompt: str
    revised_prompt: Optional[str] = None
    seed: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "prompt": self.prompt,
            "revised_prompt": self.revised_prompt,
            "seed": self.seed,
            "provider": self.provider,
            "model": self.model,
            "size": len(self.data),
            "metadata": self.metadata
        }

    def to_base64(self) -> str:
        """Convert image data to base64 string."""
        return base64.b64encode(self.data).decode('utf-8')

    def to_data_url(self) -> str:
        """Convert to data URL for embedding."""
        mime_types = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp"
        }
        mime = mime_types.get(self.format.lower(), "image/png")
        return f"data:{mime};base64,{self.to_base64()}"


@dataclass
class EditResult:
    """Result of an image edit operation."""
    original_size: int
    edited_size: int
    edited_image: GeneratedImage
    operation: str  # "edit", "inpaint", "outpaint"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_size": self.original_size,
            "edited_size": self.edited_size,
            "edited_image": self.edited_image.to_dict(),
            "operation": self.operation,
            "metadata": self.metadata
        }


@dataclass
class VariationResult:
    """Result of image variation operation."""
    original_prompt: Optional[str]
    variations: List[GeneratedImage]
    count: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_prompt": self.original_prompt,
            "variations": [v.to_dict() for v in self.variations],
            "count": self.count,
            "metadata": self.metadata
        }


class ImageGenerator:
    """
    Image generator for AI image generation.

    Supports multiple providers for text-to-image generation.
    """

    # Available providers
    providers: List[str] = ["openai", "stability", "midjourney"]

    # Default models per provider
    DEFAULT_MODELS = {
        ImageProvider.OPENAI: "dall-e-3",
        ImageProvider.STABILITY: "stable-diffusion-xl-1024-v1-0",
        ImageProvider.MIDJOURNEY: "v6"
    }

    # Supported sizes per provider
    SUPPORTED_SIZES = {
        ImageProvider.OPENAI: ["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"],
        ImageProvider.STABILITY: ["512x512", "768x768", "1024x1024", "1536x1536"],
        ImageProvider.MIDJOURNEY: ["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"]
    }

    def __init__(
        self,
        provider: Union[ImageProvider, str] = ImageProvider.OPENAI,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize image generator.

        Args:
            provider: Image generation provider to use
            api_key: API key for the provider
            model: Specific model to use
            config: Additional configuration options
        """
        if isinstance(provider, str):
            provider = ImageProvider(provider.lower())

        self.provider = provider
        self.api_key = api_key
        self.config = config or {}

        # Set default model per provider
        self.model = model or self.DEFAULT_MODELS.get(provider, "default")

        # Initialize provider-specific client
        self._client = None
        self._initialized = False

        # Rate limiting
        self._last_request_time = 0
        self._min_request_interval = config.get("min_request_interval", 1.0) if config else 1.0

        # Ensure temp directories exist
        self._ensure_temp_dirs()

    def _ensure_temp_dirs(self):
        """Ensure temp directories exist (Docker-compatible)."""
        for dir_path in [TEMP_DIR, APP_TEMP_DIR]:
            try:
                Path(dir_path).mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError):
                pass

    async def _ensure_initialized(self):
        """Ensure provider client is initialized."""
        if self._initialized:
            return

        if self.provider == ImageProvider.OPENAI:
            # Would initialize OpenAI client
            pass
        elif self.provider == ImageProvider.STABILITY:
            # Would initialize Stability AI client
            pass
        elif self.provider == ImageProvider.MIDJOURNEY:
            # Would initialize Midjourney client (via Discord or API)
            pass

        self._initialized = True

    async def _rate_limit(self):
        """Apply rate limiting between requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            await asyncio.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _normalize_size(self, size: str) -> str:
        """Normalize size string to supported format."""
        # Handle ImageSize enum
        if isinstance(size, ImageSize):
            size = size.value

        # Check if size is supported
        supported = self.SUPPORTED_SIZES.get(self.provider, ["1024x1024"])
        if size in supported:
            return size

        # Find closest supported size
        try:
            w, h = map(int, size.split("x"))
            target_pixels = w * h

            best_size = supported[0]
            best_diff = float("inf")

            for s in supported:
                sw, sh = map(int, s.split("x"))
                diff = abs(sw * sh - target_pixels)
                if diff < best_diff:
                    best_diff = diff
                    best_size = s

            return best_size
        except ValueError:
            return "1024x1024"

    async def generate(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: Optional[Union[ImageStyle, str]] = None,
        quality: str = "standard",
        n: int = 1
    ) -> bytes:
        """
        Generate image from prompt.

        Args:
            prompt: Text description of the image to generate
            size: Image size (e.g., "1024x1024")
            style: Style preset (vivid, natural, etc.)
            quality: Quality level (standard, hd)
            n: Number of images to generate

        Returns:
            Image bytes (first image if n > 1)
        """
        await self._ensure_initialized()
        await self._rate_limit()

        size = self._normalize_size(size)
        if isinstance(style, ImageStyle):
            style = style.value

        logger.info(f"Generating image: {prompt[:50]}... ({size})")

        # Provider-specific generation
        if self.provider == ImageProvider.OPENAI:
            return await self._generate_openai(prompt, size, style, quality, n)
        elif self.provider == ImageProvider.STABILITY:
            return await self._generate_stability(prompt, size, style, n)
        elif self.provider == ImageProvider.MIDJOURNEY:
            return await self._generate_midjourney(prompt, size, style)

        return b""

    async def _generate_openai(
        self,
        prompt: str,
        size: str,
        style: Optional[str],
        quality: str,
        n: int
    ) -> bytes:
        """Generate using OpenAI DALL-E."""
        # Would use OpenAI API:
        # response = await self._client.images.generate(
        #     model=self.model,
        #     prompt=prompt,
        #     size=size,
        #     style=style or "vivid",
        #     quality=quality,
        #     n=n,
        #     response_format="b64_json"
        # )
        # return base64.b64decode(response.data[0].b64_json)
        return b""

    async def _generate_stability(
        self,
        prompt: str,
        size: str,
        style: Optional[str],
        n: int
    ) -> bytes:
        """Generate using Stability AI."""
        # Would use Stability API
        return b""

    async def _generate_midjourney(
        self,
        prompt: str,
        size: str,
        style: Optional[str]
    ) -> bytes:
        """Generate using Midjourney."""
        # Would use Midjourney API/Discord integration
        return b""

    async def generate_multiple(
        self,
        prompt: str,
        n: int = 4,
        size: str = "1024x1024",
        style: Optional[Union[ImageStyle, str]] = None
    ) -> List[GeneratedImage]:
        """
        Generate multiple images from prompt.

        Args:
            prompt: Text description
            n: Number of images to generate
            size: Image size
            style: Style preset

        Returns:
            List of GeneratedImage objects
        """
        await self._ensure_initialized()

        size = self._normalize_size(size)
        w, h = map(int, size.split("x"))

        images = []
        for i in range(n):
            await self._rate_limit()
            data = await self.generate(prompt, size, style, n=1)
            if data:
                images.append(GeneratedImage(
                    data=data,
                    format="png",
                    width=w,
                    height=h,
                    prompt=prompt,
                    provider=self.provider.value,
                    model=self.model,
                    metadata={"index": i}
                ))

        return images

    async def edit(
        self,
        image: bytes,
        prompt: str,
        mask: Optional[bytes] = None,
        size: str = "1024x1024"
    ) -> bytes:
        """
        Edit an existing image based on prompt.

        Args:
            image: Original image bytes
            prompt: Edit instruction
            mask: Optional mask indicating areas to edit (transparent = edit)
            size: Output size

        Returns:
            Edited image bytes
        """
        await self._ensure_initialized()
        await self._rate_limit()

        size = self._normalize_size(size)

        logger.info(f"Editing image with prompt: {prompt[:50]}...")

        # Provider-specific editing
        if self.provider == ImageProvider.OPENAI:
            return await self._edit_openai(image, prompt, mask, size)
        elif self.provider == ImageProvider.STABILITY:
            return await self._edit_stability(image, prompt, mask, size)

        return b""

    async def _edit_openai(
        self,
        image: bytes,
        prompt: str,
        mask: Optional[bytes],
        size: str
    ) -> bytes:
        """Edit using OpenAI DALL-E."""
        # Would use OpenAI API:
        # response = await self._client.images.edit(
        #     model="dall-e-2",  # Only DALL-E 2 supports edit
        #     image=image,
        #     mask=mask,
        #     prompt=prompt,
        #     size=size,
        #     response_format="b64_json"
        # )
        # return base64.b64decode(response.data[0].b64_json)
        return b""

    async def _edit_stability(
        self,
        image: bytes,
        prompt: str,
        mask: Optional[bytes],
        size: str
    ) -> bytes:
        """Edit using Stability AI."""
        # Would use Stability API inpainting
        return b""

    async def variations(
        self,
        image: bytes,
        n: int = 1,
        size: str = "1024x1024"
    ) -> List[bytes]:
        """
        Generate variations of an image.

        Args:
            image: Original image bytes
            n: Number of variations to generate
            size: Output size

        Returns:
            List of variation image bytes
        """
        await self._ensure_initialized()

        size = self._normalize_size(size)

        logger.info(f"Generating {n} variations")

        variations = []
        for _ in range(n):
            await self._rate_limit()
            var = await self._generate_variation(image, size)
            if var:
                variations.append(var)

        return variations

    async def _generate_variation(
        self,
        image: bytes,
        size: str
    ) -> bytes:
        """Generate single variation."""
        if self.provider == ImageProvider.OPENAI:
            # Would use OpenAI API:
            # response = await self._client.images.create_variation(
            #     model="dall-e-2",  # Only DALL-E 2 supports variations
            #     image=image,
            #     size=size,
            #     response_format="b64_json"
            # )
            # return base64.b64decode(response.data[0].b64_json)
            pass
        elif self.provider == ImageProvider.STABILITY:
            # Would use Stability API image-to-image
            pass

        return b""

    async def upscale(
        self,
        image: bytes,
        scale: int = 2
    ) -> bytes:
        """
        Upscale an image.

        Args:
            image: Image to upscale
            scale: Scale factor (2x, 4x)

        Returns:
            Upscaled image bytes
        """
        await self._ensure_initialized()
        await self._rate_limit()

        if self.provider == ImageProvider.STABILITY:
            # Stability AI has dedicated upscaling
            # Would use their upscale API
            pass

        # Placeholder - would use actual upscaling
        return image

    async def save_to_file(
        self,
        prompt: str,
        file_path: str,
        size: str = "1024x1024",
        style: Optional[Union[ImageStyle, str]] = None
    ) -> str:
        """
        Generate and save image to file.

        Args:
            prompt: Text description
            file_path: Output file path
            size: Image size
            style: Style preset

        Returns:
            Path to saved file
        """
        image_data = await self.generate(prompt, size, style)

        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, 'wb') as f:
            f.write(image_data)

        return str(path)

    def get_temp_path(self, prefix: str = "img") -> str:
        """
        Get a temporary file path for image storage.

        Args:
            prefix: File name prefix

        Returns:
            Temporary file path (Docker-compatible)
        """
        timestamp = int(time.time() * 1000)
        random_hash = hashlib.md5(str(timestamp).encode()).hexdigest()[:8]
        filename = f"{prefix}_{timestamp}_{random_hash}.png"
        return os.path.join(TEMP_DIR, filename)

    def get_supported_sizes(self) -> List[str]:
        """Get list of supported sizes for current provider."""
        return self.SUPPORTED_SIZES.get(self.provider, ["1024x1024"])

    def get_supported_styles(self) -> List[str]:
        """Get list of supported styles for current provider."""
        styles = {
            ImageProvider.OPENAI: ["vivid", "natural"],
            ImageProvider.STABILITY: [s.value for s in ImageStyle],
            ImageProvider.MIDJOURNEY: ["raw", "cute", "scenic", "expressive", "original"]
        }
        return styles.get(self.provider, [])

    def get_max_prompt_length(self) -> int:
        """Get maximum prompt length for current provider."""
        limits = {
            ImageProvider.OPENAI: 4000,
            ImageProvider.STABILITY: 2000,
            ImageProvider.MIDJOURNEY: 6000
        }
        return limits.get(self.provider, 2000)

    def estimate_cost(
        self,
        size: str = "1024x1024",
        quality: str = "standard",
        n: int = 1
    ) -> float:
        """
        Estimate cost for generation.

        Args:
            size: Image size
            quality: Quality level
            n: Number of images

        Returns:
            Estimated cost in USD
        """
        # Approximate pricing (may be outdated)
        pricing = {
            ImageProvider.OPENAI: {
                ("1024x1024", "standard"): 0.04,
                ("1024x1024", "hd"): 0.08,
                ("1792x1024", "standard"): 0.08,
                ("1792x1024", "hd"): 0.12,
                ("1024x1792", "standard"): 0.08,
                ("1024x1792", "hd"): 0.12,
            },
            ImageProvider.STABILITY: {
                ("512x512", "standard"): 0.002,
                ("1024x1024", "standard"): 0.008,
            }
        }

        provider_pricing = pricing.get(self.provider, {})
        cost = provider_pricing.get((size, quality), 0.04)

        return cost * n

    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about the current provider."""
        return {
            "provider": self.provider.value,
            "model": self.model,
            "supported_sizes": self.get_supported_sizes(),
            "supported_styles": self.get_supported_styles(),
            "max_prompt_length": self.get_max_prompt_length(),
            "supports_edit": self.provider in [ImageProvider.OPENAI, ImageProvider.STABILITY],
            "supports_variations": self.provider in [ImageProvider.OPENAI, ImageProvider.STABILITY],
            "supports_upscale": self.provider == ImageProvider.STABILITY
        }
