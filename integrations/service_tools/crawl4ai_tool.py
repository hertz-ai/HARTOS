"""
Crawl4AI tool wrapper — web scraping to markdown conversion.

Now uses native in-process crawl4ai (no Docker/HTTP required).
Falls back to requests+BeautifulSoup if crawl4ai not installed.

The agent sees intermediate progress and full extracted content.
"""

import json
import os
from .registry import ServiceToolInfo, service_tool_registry


def _native_crawl(params_json: str) -> str:
    """Execute crawl in-process. Returns agent-visible progress + content."""
    from integrations.web_crawler import crawl_url_for_agent, crawl_urls_for_agent

    try:
        params = json.loads(params_json) if isinstance(params_json, str) else params_json
    except (json.JSONDecodeError, TypeError):
        params = {'url': str(params_json)}

    # Handle both 'url' and 'urls' param names
    url = params.get('url') or params.get('urls', '')
    if isinstance(url, list):
        return crawl_urls_for_agent(url)
    return crawl_url_for_agent(str(url))


class Crawl4AITool:
    """Register web crawling as a native tool (in-process, no Docker)."""

    @classmethod
    def create_tool_info(cls, base_url: str = None) -> ServiceToolInfo:
        return ServiceToolInfo(
            name="crawl4ai",
            description=(
                "Web scraping and content extraction. Crawls URLs and converts "
                "web pages to clean markdown optimized for LLM consumption. "
                "Supports JavaScript rendering via crawl4ai or BeautifulSoup fallback. "
                "Runs in-process — no external service needed."
            ),
            base_url="native://in-process",
            endpoints={
                "crawl": {
                    "path": "/crawl",
                    "method": "POST",
                    "description": (
                        "Crawl a URL and extract content as clean markdown. "
                        "Input: JSON with 'url' (string URL to crawl). "
                        "Returns progress log + markdown text of the page content."
                    ),
                    "params_schema": {
                        "url": {"type": "string", "description": "URL to crawl"},
                    },
                    "native_handler": _native_crawl,
                },
            },
            health_endpoint=None,  # No external service to check
            tags=["web", "scraping", "markdown", "crawling"],
            timeout=60,
        )

    @classmethod
    def register(cls, base_url: str = None) -> bool:
        """Register Crawl4AI with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
