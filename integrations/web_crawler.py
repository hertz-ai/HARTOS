"""
Native web crawler — in-process crawl4ai, no HTTP API middleman.

Every step is logged into a progress buffer that gets returned as part of
the tool output, so the LangChain/autogen agent sees intermediate progress
(connecting, rendering, extracting, word count) alongside the final content.

Falls back to requests+BeautifulSoup if crawl4ai not installed.

Consumers:
- LangChain Data_Extraction_From_URL tool (hart_intelligence)
- Google search enrichment top5_results (helper.py)
- autogen service tools (reuse_recipe.py)
"""

import asyncio
import logging
import time
from typing import List, Optional

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

async def _crawl_with_cookies(url: str, cookies: list, timeout: int,
                              cdp_endpoint: Optional[str], log: '_ProgressLog') -> dict:
    """Crawl one URL with optional cookie jar + optional CDP attach.

    `cookies` is a list of dicts: [{'name', 'value', 'domain', 'path', ...}].
    `cdp_endpoint` is an "http://127.0.0.1:9222"-style URL.  When provided,
    Playwright connects to that running browser (B2 mode — attaches to the
    user's already-logged-in Chrome) instead of launching a new one.

    Falls back to a fresh Obscura/crawl4ai instance (B1 mode) if cdp_endpoint
    is None or unreachable.  Audit + connection_mechanism are added to the
    result so the caller can propagate which path was taken.
    """
    log.step(f"Connecting to {url}...")
    connection_mechanism = 'obscura_b1_headless_profile'
    try:
        if cdp_endpoint:
            log.step(f"Attempting B2 attach via CDP at {cdp_endpoint}")
            try:
                from playwright.async_api import async_playwright
                async with async_playwright() as p:
                    browser = await p.chromium.connect_over_cdp(cdp_endpoint)
                    connection_mechanism = 'obscura_b2_cdp_user_chrome'
                    log.step("CDP attached to user's Chrome")
                    contexts = browser.contexts
                    context = contexts[0] if contexts else await browser.new_context()
                    if cookies:
                        try:
                            await context.add_cookies(cookies)
                            log.step(f"Injected {len(cookies)} cookies into existing session")
                        except Exception as exc:
                            log.step(f"cookie injection skipped (already logged in?): {exc}")
                    page = await context.new_page()
                    try:
                        await page.goto(url, timeout=timeout * 1000)
                        content = await page.content()
                        word_count = len(content.split())
                        return {
                            'success': True,
                            'url': url,
                            'markdown': content,
                            'word_count': word_count,
                            'connection_mechanism': connection_mechanism,
                        }
                    finally:
                        await page.close()
            except Exception as exc:
                log.step(f"B2 attach failed: {exc} — falling back to B1")
                connection_mechanism = 'obscura_b1_headless_profile'

        # B1: standard Crawl4AI path — same crawler the public fetch uses,
        # cookies optionally injected via Playwright session config.
        crawler = await _get_crawler(log)
        log.step(f"Rendering page in B1 profile (timeout={timeout}s)...")
        # crawl4ai's `arun` accepts a `cookies` kwarg in newer versions; older
        # versions silently ignore it — that's acceptable degradation.
        try:
            result = await crawler.arun(
                url=url,
                word_count_threshold=50,
                timeout=timeout * 1000,
                bypass_cache=True,
                cookies=cookies if cookies else None,
            )
        except TypeError:
            # Older crawl4ai: no `cookies` kwarg — run without; the caller
            # gets a degraded (unauthenticated) crawl rather than a hard error.
            log.step("crawl4ai version does not accept cookies kwarg; running unauthenticated")
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
                'connection_mechanism': connection_mechanism,
            }
        error = getattr(result, 'error_message', 'No content extracted')
        return {'success': False, 'url': url, 'error': error,
                'connection_mechanism': connection_mechanism}
    except Exception as e:
        log.step(f"Crawl with cookies error: {e}")
        return {'success': False, 'url': url, 'error': str(e),
                'connection_mechanism': connection_mechanism}


def crawl_url_with_cookies(url: str, cookies: Optional[list] = None,
                           timeout: int = 30,
                           cdp_endpoint: Optional[str] = None) -> dict:
    """Public sync entry: crawl `url` with the supplied cookie jar.

    Used by browser_research T2 platform scripts (Twitter, Reddit, LinkedIn,
    Bilibili, XHS, Weibo, ...) — every per-platform script in
    integrations/browser_research/scripts/ funnels through here.  No second
    Chromium process; same canonical crawler as `crawl_url`.

    Args:
        url: full http(s) URL.
        cookies: list of cookie dicts (Playwright format).  Empty list / None
            yields an unauthenticated crawl.
        timeout: per-page seconds.
        cdp_endpoint: if set (e.g. "http://127.0.0.1:9222"), Playwright attaches
            to a running Chrome instead of launching one (B2 mode).  Allows the
            agent to drive the user's already-logged-in browser, looking like a
            human session.

    Returns dict with success/url/markdown/word_count/connection_mechanism.
    """
    if not _check_available() and not cdp_endpoint:
        # crawl4ai unavailable AND no CDP attach requested — fall back to plain
        # requests with cookie jar.  Loses JS execution but better than failing.
        return _fallback_fetch_with_cookies(url, cookies or [], timeout)

    log = _ProgressLog()
    try:
        result = _run_async(_crawl_with_cookies(url, cookies or [], timeout,
                                                cdp_endpoint, log))
    except Exception as e:
        return {'success': False, 'url': url, 'error': str(e),
                'progress': log.text(),
                'connection_mechanism': 'obscura_b1_headless_profile'}
    result['progress'] = log.text()
    return result


def _fallback_fetch_with_cookies(url: str, cookies: list, timeout: int) -> dict:
    """Last-resort: requests + cookie jar, no JS execution."""
    import requests as _req
    log = _ProgressLog()
    log.step(f"Fetching {url} (requests + cookies, no JS)...")
    try:
        sess = _req.Session()
        for c in cookies:
            if c.get('name') and c.get('value'):
                sess.cookies.set(c['name'], c['value'],
                                 domain=c.get('domain'), path=c.get('path', '/'))
        resp = sess.get(url, timeout=timeout,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            text = ' '.join(soup.get_text().split())
        except Exception:
            text = resp.text
        return {
            'success': resp.status_code == 200,
            'url': url,
            'markdown': text[:50000],
            'word_count': len(text.split()),
            'status_code': resp.status_code,
            'connection_mechanism': 'public_http',
            'progress': log.text(),
        }
    except Exception as e:
        return {'success': False, 'url': url, 'error': str(e),
                'progress': log.text(),
                'connection_mechanism': 'public_http'}


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
