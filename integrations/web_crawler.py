"""
Native web crawler — in-process crawl4ai, no HTTP API middleman.

Every step is logged into a progress buffer that gets returned as part of
the tool output, so the LangChain/autogen agent sees intermediate progress
(connecting, rendering, extracting, word count) alongside the final content.

Falls back to requests+BeautifulSoup if crawl4ai not installed.

Consumers:
- LangChain Data_Extraction_From_URL tool (hart_intelligence (langchain_gpt_api.py))
- Google search enrichment top5_results (helper.py)
- autogen service tools (reuse_recipe.py)
"""

import asyncio
import logging
import time
from typing import List

logger = logging.getLogger(__name__)

# Lazy-loaded crawler instance (heavy import — Playwright/Chromium)
_crawler = None
_crawler_available = None  # None = not checked yet


def _check_available() -> bool:
    """Check if crawl4ai library is installed (cached)."""
    global _crawler_available
    if _crawler_available is not None:
        return _crawler_available
    try:
        import crawl4ai  # noqa: F401
        _crawler_available = True
    except ImportError:
        logger.info("crawl4ai not installed, using requests+BeautifulSoup fallback")
        _crawler_available = False
    return _crawler_available


class _ProgressLog:
    """Accumulates intermediate step messages the agent sees in tool output."""

    def __init__(self):
        self._lines = []
        self._start = time.time()

    def step(self, msg: str):
        elapsed = round(time.time() - self._start, 2)
        line = f"[{elapsed}s] {msg}"
        self._lines.append(line)
        logger.info(msg)

    def text(self) -> str:
        return "\n".join(self._lines)


async def _get_crawler(log: _ProgressLog):
    """Lazy-init singleton AsyncWebCrawler."""
    global _crawler
    if _crawler is not None:
        return _crawler
    log.step("Initializing browser engine (first run)...")
    from crawl4ai import AsyncWebCrawler
    _crawler = AsyncWebCrawler(
        headless=True,
        browser_type='chromium',
        verbose=False,
    )
    await _crawler.start()
    log.step("Browser engine ready")
    return _crawler


async def _crawl_single(url: str, timeout: int, log: _ProgressLog) -> dict:
    """Crawl one URL with intermediate progress logging."""
    log.step(f"Connecting to {url}...")
    try:
        crawler = await _get_crawler(log)
        log.step(f"Rendering page (timeout={timeout}s)...")
        result = await crawler.arun(
            url=url,
            word_count_threshold=50,
            timeout=timeout * 1000,
            bypass_cache=True,
        )
        if result.success and result.markdown:
            word_count = len(result.markdown.split())
            log.step(f"Extracted {word_count} words from {url}")
            return {
                'success': True,
                'url': url,
                'markdown': result.markdown,
                'word_count': word_count,
            }
        error = getattr(result, 'error_message', 'No content extracted')
        log.step(f"Crawl returned no content: {error}")
        return {'success': False, 'url': url, 'error': error}
    except Exception as e:
        log.step(f"Crawl error: {e}")
        return {'success': False, 'url': url, 'error': str(e)}


def _fallback_fetch(url: str, timeout: int, log: _ProgressLog) -> dict:
    """Fallback: requests + BeautifulSoup. No browser needed."""
    import requests as _req
    log.step(f"Fetching {url} (requests fallback)...")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        resp = _req.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        log.step(f"HTTP {resp.status_code}, {len(resp.content)} bytes received")

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        cleaned = ' '.join(c for c in chunks if c)

        if not cleaned or len(cleaned) < 50:
            log.step("Too little content after cleanup")
            return {'success': False, 'url': url, 'error': 'Too little content extracted'}

        word_count = len(cleaned.split())
        log.step(f"Extracted {word_count} words (BeautifulSoup)")
        return {'success': True, 'url': url, 'markdown': cleaned, 'word_count': word_count}
    except Exception as e:
        log.step(f"Fallback error: {e}")
        return {'success': False, 'url': url, 'error': str(e)}


def _run_async(coro):
    """Run an async coroutine from sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result(timeout=120)
    else:
        return asyncio.run(coro)


# ── Public API ──────────────────────────────────────────────────────

def crawl_url(url: str, timeout: int = 30) -> dict:
    """
    Crawl a single URL. Returns dict with markdown + progress log.

    Result keys: success, url, markdown, word_count, progress (str).
    """
    log = _ProgressLog()
    if _check_available():
        log.step("Using crawl4ai (in-process, JS rendering enabled)")
        try:
            result = _run_async(_crawl_single(url, timeout, log))
            result['progress'] = log.text()
            return result
        except Exception as e:
            log.step(f"crawl4ai failed: {e}, falling back to requests")
    else:
        log.step("crawl4ai not installed, using requests+BeautifulSoup")

    result = _fallback_fetch(url, timeout, log)
    result['progress'] = log.text()
    return result


def crawl_urls(urls: List[str], timeout: int = 30, max_concurrent: int = 3) -> List[dict]:
    """
    Crawl multiple URLs. Returns list of result dicts, each with progress.
    """
    if not urls:
        return []

    log = _ProgressLog()
    log.step(f"Batch crawl: {len(urls)} URLs, max_concurrent={max_concurrent}")

    if _check_available():
        log.step("Using crawl4ai (in-process)")

        async def _batch():
            sem = asyncio.Semaphore(max_concurrent)
            async def _one(u):
                async with sem:
                    return await _crawl_single(u, timeout, log)
            return await asyncio.gather(*[_one(u) for u in urls])

        try:
            results = _run_async(_batch())
            success_count = sum(1 for r in results if r['success'])
            log.step(f"Batch complete: {success_count}/{len(urls)} succeeded")
            batch_progress = log.text()
            for r in results:
                r['progress'] = batch_progress
            return results
        except Exception as e:
            log.step(f"crawl4ai batch failed: {e}, falling back")
    else:
        log.step("crawl4ai not installed, sequential fallback")

    results = []
    for u in urls:
        r = _fallback_fetch(u, timeout, log)
        results.append(r)
    batch_progress = log.text()
    for r in results:
        r['progress'] = batch_progress
    return results


def crawl_url_for_agent(url: str, timeout: int = 30) -> str:
    """
    Crawl a URL and return a string for the LangChain agent.

    The agent sees every intermediate step (progress log) followed by content.
    """
    result = crawl_url(url, timeout)
    parts = []

    # Intermediate progress — agent sees each step
    if result.get('progress'):
        parts.append("--- Progress ---")
        parts.append(result['progress'])
        parts.append("--- Result ---")

    if result['success']:
        content = result['markdown']
        # Truncate for agent context window
        if len(content) > 8000:
            truncate_pos = content.rfind('.', 0, 8000)
            if truncate_pos > 6000:
                content = content[:truncate_pos + 1] + "\n[Content truncated]"
            else:
                content = content[:8000] + "\n[Content truncated]"
        parts.append(f"URL: {url}")
        parts.append(f"Words extracted: {result['word_count']}")
        parts.append(f"Content:\n{content}")
    else:
        parts.append(f"FAILED: {url}")
        parts.append(f"Error: {result['error']}")

    return "\n".join(parts)


def crawl_urls_for_agent(urls: List[str], timeout: int = 30) -> str:
    """
    Crawl multiple URLs and return combined agent-readable output.
    Includes progress log so agent sees intermediate steps.
    """
    results = crawl_urls(urls, timeout)
    parts = []

    # Shared progress log (all results have the same batch progress)
    if results and results[0].get('progress'):
        parts.append("--- Progress ---")
        parts.append(results[0]['progress'])
        parts.append("--- Results ---")

    success_count = 0
    for r in results:
        if r['success']:
            success_count += 1
            content = r['markdown']
            if len(content) > 4000:
                truncate_pos = content.rfind('.', 0, 4000)
                if truncate_pos > 3000:
                    content = content[:truncate_pos + 1] + " [truncated]"
                else:
                    content = content[:4000] + " [truncated]"
            parts.append(f"\n## {r['url']}\nWords: {r['word_count']}\n{content}")
        else:
            parts.append(f"\n## {r['url']}\nFailed: {r['error']}")

    header = f"Crawled {success_count}/{len(urls)} URLs successfully"
    return header + "\n" + "\n".join(parts)
