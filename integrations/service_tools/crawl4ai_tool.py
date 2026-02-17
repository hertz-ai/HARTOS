"""
Crawl4AI tool wrapper — web scraping to markdown conversion.

Service: Crawl4AI (https://github.com/unclecode/crawl4ai)
Default port: 11235
Deployment: docker run -d -p 11235:11235 --shm-size=1g unclecode/crawl4ai:latest
"""

import os
from .registry import ServiceToolInfo, service_tool_registry


class Crawl4AITool:
    """Thin wrapper to register Crawl4AI with the ServiceToolRegistry."""

    DEFAULT_URL = os.environ.get('CRAWL4AI_URL', "http://localhost:11235")

    @classmethod
    def create_tool_info(cls, base_url: str = None) -> ServiceToolInfo:
        return ServiceToolInfo(
            name="crawl4ai",
            description=(
                "Web scraping and content extraction. Crawls URLs and converts "
                "web pages to clean markdown optimized for LLM consumption. "
                "Supports JavaScript rendering, screenshots, and PDF generation."
            ),
            base_url=base_url or cls.DEFAULT_URL,
            endpoints={
                "crawl": {
                    "path": "/crawl",
                    "method": "POST",
                    "description": (
                        "Crawl a URL and extract content as clean markdown. "
                        "Input: JSON with 'urls' (string URL to crawl). "
                        "Returns markdown text of the page content."
                    ),
                    "params_schema": {
                        "urls": {"type": "string", "description": "URL to crawl"},
                        "word_count_threshold": {"type": "integer", "default": 10},
                        "screenshot": {"type": "boolean", "default": False},
                    },
                },
                "screenshot": {
                    "path": "/screenshot",
                    "method": "POST",
                    "description": (
                        "Take a screenshot of a web page. "
                        "Input: JSON with 'url' (string). Returns base64 image."
                    ),
                    "params_schema": {
                        "url": {"type": "string", "description": "URL to screenshot"},
                    },
                },
            },
            health_endpoint="/health",
            tags=["web", "scraping", "markdown", "crawling"],
            timeout=60,
        )

    @classmethod
    def register(cls, base_url: str = None) -> bool:
        """Register Crawl4AI with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
