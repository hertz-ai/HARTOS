"""
Link Processor for URL handling.

Provides URL detection, fetching, preview generation, and summarization.
"""

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from urllib.parse import urlparse, urljoin
import logging

logger = logging.getLogger(__name__)


class LinkType(Enum):
    """Types of links."""
    WEBPAGE = "webpage"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    SOCIAL = "social"
    EMBED = "embed"
    UNKNOWN = "unknown"


@dataclass
class OpenGraphData:
    """Open Graph metadata from a URL."""
    title: Optional[str] = None
    description: Optional[str] = None
    image: Optional[str] = None
    url: Optional[str] = None
    type: Optional[str] = None
    site_name: Optional[str] = None
    locale: Optional[str] = None
    video: Optional[str] = None
    audio: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "image": self.image,
            "url": self.url,
            "type": self.type,
            "site_name": self.site_name,
            "locale": self.locale,
            "video": self.video,
            "audio": self.audio
        }


@dataclass
class LinkPreview:
    """Preview data for a link."""
    url: str
    final_url: str
    title: Optional[str] = None
    description: Optional[str] = None
    image: Optional[str] = None
    favicon: Optional[str] = None
    site_name: Optional[str] = None
    link_type: LinkType = LinkType.WEBPAGE
    open_graph: Optional[OpenGraphData] = None
    twitter_card: Optional[Dict[str, str]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "description": self.description,
            "image": self.image,
            "favicon": self.favicon,
            "site_name": self.site_name,
            "link_type": self.link_type.value,
            "open_graph": self.open_graph.to_dict() if self.open_graph else None,
            "twitter_card": self.twitter_card,
            "metadata": self.metadata
        }


@dataclass
class FetchedContent:
    """Fetched content from a URL."""
    url: str
    final_url: str
    status_code: int
    content_type: str
    content: Union[str, bytes]
    headers: Dict[str, str] = field(default_factory=dict)
    encoding: Optional[str] = None
    size: int = 0
    load_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "encoding": self.encoding,
            "size": self.size,
            "load_time": self.load_time,
            "headers": self.headers
        }


@dataclass
class LinkSummary:
    """Summary of link content."""
    url: str
    title: str
    summary: str
    key_points: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    word_count: int = 0
    reading_time: int = 0  # in minutes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "summary": self.summary,
            "key_points": self.key_points,
            "topics": self.topics,
            "word_count": self.word_count,
            "reading_time": self.reading_time
        }


@dataclass
class DetectedLink:
    """A detected link in text."""
    url: str
    start: int
    end: int
    text: Optional[str] = None
    link_type: LinkType = LinkType.UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "link_type": self.link_type.value
        }


class LinkProcessor:
    """
    Link processor for URL handling.

    Provides detection, fetching, preview generation, and summarization.
    """

    # URL pattern for detection
    URL_PATTERN = re.compile(
        r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:[/\w._~:/?#\[\]@!$&\'()*+,;=-]*)?',
        re.IGNORECASE
    )

    # File extension to type mapping
    EXTENSION_TYPES = {
        'jpg': LinkType.IMAGE, 'jpeg': LinkType.IMAGE, 'png': LinkType.IMAGE,
        'gif': LinkType.IMAGE, 'webp': LinkType.IMAGE, 'svg': LinkType.IMAGE,
        'mp4': LinkType.VIDEO, 'webm': LinkType.VIDEO, 'avi': LinkType.VIDEO,
        'mov': LinkType.VIDEO, 'mkv': LinkType.VIDEO,
        'mp3': LinkType.AUDIO, 'wav': LinkType.AUDIO, 'ogg': LinkType.AUDIO,
        'flac': LinkType.AUDIO, 'm4a': LinkType.AUDIO,
        'pdf': LinkType.DOCUMENT, 'doc': LinkType.DOCUMENT, 'docx': LinkType.DOCUMENT,
        'xls': LinkType.DOCUMENT, 'xlsx': LinkType.DOCUMENT, 'ppt': LinkType.DOCUMENT,
        'pptx': LinkType.DOCUMENT, 'txt': LinkType.DOCUMENT
    }

    # Social media domains
    SOCIAL_DOMAINS = {
        'twitter.com', 'x.com', 'facebook.com', 'instagram.com',
        'linkedin.com', 'tiktok.com', 'youtube.com', 'youtu.be',
        'reddit.com', 'pinterest.com', 'tumblr.com'
    }

    def __init__(
        self,
        timeout: int = 30,
        max_size: int = 10 * 1024 * 1024,  # 10MB
        user_agent: Optional[str] = None,
        follow_redirects: bool = True,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize link processor.

        Args:
            timeout: Request timeout in seconds
            max_size: Maximum content size to fetch
            user_agent: Custom user agent string
            follow_redirects: Whether to follow redirects
            config: Additional configuration options
        """
        self.timeout = timeout
        self.max_size = max_size
        self.user_agent = user_agent or "HevolveBot/1.0 LinkProcessor"
        self.follow_redirects = follow_redirects
        self.config = config or {}

    def detect(self, text: str) -> List[DetectedLink]:
        """
        Detect URLs in text.

        Args:
            text: Text to search for URLs

        Returns:
            List of detected links with positions
        """
        links = []
        for match in self.URL_PATTERN.finditer(text):
            url = match.group()
            link_type = self._determine_link_type(url)
            links.append(DetectedLink(
                url=url,
                start=match.start(),
                end=match.end(),
                text=url,
                link_type=link_type
            ))
        return links

    def _determine_link_type(self, url: str) -> LinkType:
        """Determine the type of a link."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip('www.')
        path = parsed.path.lower()

        # Check for social media
        if domain in self.SOCIAL_DOMAINS:
            return LinkType.SOCIAL

        # Check file extension
        if '.' in path:
            ext = path.rsplit('.', 1)[-1]
            if ext in self.EXTENSION_TYPES:
                return self.EXTENSION_TYPES[ext]

        return LinkType.WEBPAGE

    async def fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None
    ) -> FetchedContent:
        """
        Fetch content from a URL.

        Args:
            url: URL to fetch
            headers: Additional headers to send

        Returns:
            FetchedContent with the fetched data
        """
        import time
        start_time = time.time()

        # Would use aiohttp or httpx in real implementation
        # Simulated response for now
        return FetchedContent(
            url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            content="",
            headers={},
            encoding="utf-8",
            size=0,
            load_time=time.time() - start_time
        )

    async def preview(
        self,
        url: str,
        fetch_image: bool = True
    ) -> LinkPreview:
        """
        Generate a preview for a URL.

        Args:
            url: URL to preview
            fetch_image: Whether to validate/fetch preview image

        Returns:
            LinkPreview with metadata and preview data
        """
        link_type = self._determine_link_type(url)
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip('www.')

        # Fetch content for preview
        content = await self.fetch(url)

        # Parse Open Graph and other metadata
        # Would extract from HTML in real implementation
        open_graph = OpenGraphData(
            url=url,
            title=domain,
            type="website"
        )

        return LinkPreview(
            url=url,
            final_url=content.final_url,
            title=domain,
            description=None,
            image=None,
            favicon=f"https://{domain}/favicon.ico",
            site_name=domain,
            link_type=link_type,
            open_graph=open_graph,
            metadata={
                "status_code": content.status_code,
                "content_type": content.content_type
            }
        )

    async def summarize(
        self,
        url: str,
        max_length: int = 500,
        include_key_points: bool = True
    ) -> LinkSummary:
        """
        Fetch and summarize content from a URL.

        Args:
            url: URL to summarize
            max_length: Maximum summary length in characters
            include_key_points: Whether to extract key points

        Returns:
            LinkSummary with content summary
        """
        # Fetch content
        content = await self.fetch(url)
        preview = await self.preview(url)

        # Would use LLM to summarize in real implementation
        return LinkSummary(
            url=url,
            title=preview.title or "",
            summary="",
            key_points=[],
            topics=[],
            word_count=0,
            reading_time=0
        )

    async def extract_text(self, url: str) -> str:
        """
        Extract readable text from a URL.

        Args:
            url: URL to extract text from

        Returns:
            Extracted text content
        """
        content = await self.fetch(url)

        if isinstance(content.content, str):
            # Would use readability/trafilatura for extraction
            return content.content

        return ""

    async def validate(self, url: str) -> Dict[str, Any]:
        """
        Validate a URL (check if accessible).

        Args:
            url: URL to validate

        Returns:
            Validation results
        """
        try:
            content = await self.fetch(url)
            return {
                "valid": content.status_code < 400,
                "status_code": content.status_code,
                "final_url": content.final_url,
                "content_type": content.content_type,
                "error": None
            }
        except Exception as e:
            return {
                "valid": False,
                "status_code": None,
                "final_url": None,
                "content_type": None,
                "error": str(e)
            }

    def normalize(self, url: str) -> str:
        """
        Normalize a URL.

        Args:
            url: URL to normalize

        Returns:
            Normalized URL
        """
        parsed = urlparse(url)

        # Ensure scheme
        if not parsed.scheme:
            url = f"https://{url}"
            parsed = urlparse(url)

        # Normalize to lowercase domain
        normalized = parsed._replace(
            netloc=parsed.netloc.lower()
        )

        return normalized.geturl()

    def is_same_domain(self, url1: str, url2: str) -> bool:
        """Check if two URLs are from the same domain."""
        domain1 = urlparse(url1).netloc.lower().lstrip('www.')
        domain2 = urlparse(url2).netloc.lower().lstrip('www.')
        return domain1 == domain2

    def get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        return urlparse(url).netloc.lower().lstrip('www.')

    def is_safe(self, url: str) -> bool:
        """
        Check if a URL is potentially safe.

        Args:
            url: URL to check

        Returns:
            True if URL appears safe
        """
        parsed = urlparse(url)

        # Check for suspicious patterns
        suspicious_patterns = [
            'javascript:', 'data:', 'vbscript:',
            '.exe', '.scr', '.bat', '.cmd'
        ]

        url_lower = url.lower()
        for pattern in suspicious_patterns:
            if pattern in url_lower:
                return False

        # Must have valid scheme
        if parsed.scheme not in ('http', 'https'):
            return False

        return True
