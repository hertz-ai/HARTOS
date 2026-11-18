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
import importlib
import logging
import os
import re
import sys
import threading
import time
from typing import List, Optional

from core.subprocess_safe import run_bounded

logger = logging.getLogger(__name__)

# Lazy-loaded crawler instance (heavy import — Playwright/Chromium)
_crawler = None
_crawler_available = None  # None = not checked yet


def _check_available() -> bool:
    """Whether crawl4ai can crawl here (cached): it imports AND a Playwright
    Chromium is installed.  The package without its browser cannot open a
    page: live 2026-11-16 the setup's pip step landed crawl4ai but stopped
    before Chromium, and an import-only check would have called it ready and
    never offered the setup again."""
    global _crawler_available
    if _crawler_available is not None:
        return _crawler_available
    if not _crawl4ai_importable():
        logger.info("crawl4ai not installed, using requests+BeautifulSoup fallback")
        _crawler_available = False
    elif not _browser_installed():
        logger.info("crawl4ai has no Chromium installed, using requests+BeautifulSoup fallback")
        _crawler_available = False
    else:
        _crawler_available = True
    return _crawler_available


def _crawl4ai_importable() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except ImportError:
        return False


def _playwright_browser_folders() -> List[str]:
    """Where Playwright keeps its browsers: PLAYWRIGHT_BROWSERS_PATH when it
    names a folder, the setup's D: folder, and the platform default."""
    folders = []
    named = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if named and named != '0':
        folders.append(named)
    try:
        from tts.package_installer import _D_DRIVE_ROOT  # type: ignore
        folders.append(os.path.join(_D_DRIVE_ROOT, 'ms-playwright'))
    except ImportError:
        pass
    if sys.platform == 'win32':
        folders.append(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ms-playwright'))
    elif sys.platform == 'darwin':
        folders.append(os.path.expanduser('~/Library/Caches/ms-playwright'))
    else:
        folders.append(os.path.join(os.environ.get('XDG_CACHE_HOME')
                                    or os.path.expanduser('~/.cache'), 'ms-playwright'))
    return folders


def _browser_installed() -> bool:
    for folder in _playwright_browser_folders():
        try:
            if any(e.startswith('chromium') for e in os.listdir(folder)):
                return True
        except OSError:
            continue
    return False


# ── Setting crawl4ai up when a crawl needs it ──────────────────────────────
# Nothing installed crawl4ai on a desktop, or offered to, so every crawl there
# ran the fallback and pages that build themselves with JavaScript came back
# empty (owner, 2026-11-15: "restore auto-setup"; with C: at 4.8 GB free,
# "install crawl4ai on D: instead").  A crawl that finds it missing asks the
# owner on the one setup card (capability_setup.ask_owner_for_setup); on yes,
# ONE background install runs with the host's pip runner (pip_install's
# frozen route, Nunba's _run_pip) and then Playwright's Chromium.  This crawl
# is answered by the fallback either way: nothing waits on the card or the
# install.  A failed install is not retried until the app starts again.
_SETUP_CAPABILITY = 'crawl:crawl4ai'
_SETUP_REASON = (
    "Reading web pages fully, including pages that build themselves with "
    "JavaScript, needs crawl4ai and a headless Chromium browser, and they are "
    "not installed on this computer. Set them up? They download and install "
    "here (a few hundred MB); until then pages are read without JavaScript.")
_setup_lock = threading.Lock()
_setup_thread: Optional[threading.Thread] = None
_setup_failed = False


def _offer_setup(log: '_ProgressLog') -> None:
    """crawl4ai is missing: ask the owner (one card, deduped) and, once they
    have said yes, start the one background install.  Never raises."""
    global _setup_thread
    if _setup_failed:
        return
    try:
        from integrations.agent_engine.capability_setup import ask_owner_for_setup
        answer = ask_owner_for_setup(_SETUP_CAPABILITY, reason=_SETUP_REASON)
    except Exception as e:
        logger.debug(f"crawl4ai setup not offered: {e}")
        return
    if answer != 'granted':
        if answer == 'asked':
            log.step("Asked the owner to set crawl4ai up (JavaScript pages)")
        return
    with _setup_lock:
        if _setup_failed or (_setup_thread is not None and _setup_thread.is_alive()):
            return
        _setup_thread = threading.Thread(
            target=_install_crawl4ai, name='crawl4ai-setup', daemon=True)
        _setup_thread.start()
    log.step("Setting crawl4ai up in the background (the owner allowed it)")


def _setup_target():
    """(site, browsers) for the install: the private D: site and a browsers
    folder beside it whenever D: can be made private (tts.package_installer,
    the CUDA-torch fallback's folder); otherwise (None, None), pip's own
    target and Playwright's default folder.

    Owner, 2026-11-16: "Always D: when possible".  The first rule (D: only
    under 5 GiB free on C:) sent the 06:28 install to C:, which showed
    5.0 GB free at that moment and 4.1 GB after."""
    if sys.platform != 'win32':
        return None, None
    try:
        from tts.package_installer import (  # type: ignore
            _D_DRIVE_ROOT, _make_private_d_drive_site)
    except ImportError:
        return None, None
    if not _make_private_d_drive_site():
        return None, None
    return (os.path.join(_D_DRIVE_ROOT, 'site-packages'),
            os.path.join(_D_DRIVE_ROOT, 'ms-playwright'))


def _pip_install_crawl4ai(site: Optional[str]) -> bool:
    """pip install crawl4ai: in a frozen host with its runner (_run_pip,
    python-embed, the HARTOS pins), into ``site`` when one is given; else
    integrations.coding_agent.installer.pip_install."""
    if getattr(sys, 'frozen', False):
        from tts.package_installer import _run_pip  # type: ignore
        args = ['install', 'crawl4ai'] + (['--target', site] if site else [])
        ok, msg = _run_pip(args, timeout=1800, stall_timeout=600)
        if not ok:
            logger.warning(f"crawl4ai setup: pip failed: {str(msg)[-500:]}")
        return bool(ok)
    from integrations.coding_agent.installer import pip_install
    result = pip_install('crawl4ai')
    if not result.get('success'):
        logger.warning(f"crawl4ai setup: pip failed: {result.get('error')}")
    return bool(result.get('success'))


def _install_chromium(browsers: Optional[str]) -> bool:
    """Playwright's Chromium, into ``browsers`` when one is given."""
    python = sys.executable
    if getattr(sys, 'frozen', False):
        from tts.package_installer import get_embed_python  # type: ignore
        python = get_embed_python()
    env = os.environ.copy()
    if browsers:
        env['PLAYWRIGHT_BROWSERS_PATH'] = browsers
    result = run_bounded([python, '-m', 'playwright', 'install', 'chromium'],
                         timeout=1800, env=env)
    if result.returncode != 0:
        logger.warning(f"crawl4ai setup: Chromium install failed "
                       f"(timed out: {result.timed_out}): {result.stderr[-500:]}")
    return result.returncode == 0


def _install_crawl4ai() -> None:
    """The one background install (see _offer_setup)."""
    global _crawler_available, _setup_failed
    try:
        site, browsers = _setup_target()
        if _crawl4ai_importable():
            # Installed already (an earlier setup's pip step): only the
            # browser is missing, and a second copy on another drive would
            # be shadowed by the first anyway.
            logger.info("crawl4ai setup: package present, installing Chromium only")
            ok = _install_chromium(browsers)
        else:
            logger.info(f"crawl4ai setup: installing into {site or 'the user site'}")
            ok = _pip_install_crawl4ai(site) and _install_chromium(browsers)
    except Exception as e:
        logger.warning(f"crawl4ai setup failed: {type(e).__name__}: {e}")
        ok = False
    if not ok:
        _setup_failed = True
        return
    importlib.invalidate_caches()
    _crawler_available = None   # the next crawl imports it
    logger.info("crawl4ai setup: done; the next crawl uses it")


def _use_installed_browsers() -> None:
    """Launch the Chromium the setup put beside the D: site, unless a
    browsers folder is already named (PLAYWRIGHT_BROWSERS_PATH)."""
    if os.environ.get('PLAYWRIGHT_BROWSERS_PATH'):
        return
    try:
        from tts.package_installer import _D_DRIVE_ROOT  # type: ignore
    except ImportError:
        return
    folder = os.path.join(_D_DRIVE_ROOT, 'ms-playwright')
    if os.path.isdir(folder) and os.listdir(folder):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = folder


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
    _use_installed_browsers()
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


#: One picture in a page's markdown, as both read paths write it.
_PICTURE_LINK = r'!\[[^\]]*\]\([^)\s]*\)'
_PICTURE_RE = re.compile(_PICTURE_LINK)
#: Pictures standing next to each other with no words between: a gallery,
#: a row of share icons, a strip of thumbnails.
_PICTURE_RUN_RE = re.compile(rf'{_PICTURE_LINK}(?:\s*{_PICTURE_LINK})+')
#: How many pictures of such a row the agent still sees when the page is
#: over its budget.  The first stays, so a chart leading a gallery is kept.
_RUN_PICTURES_SHOWN = 3


def _fit_for_agent(content: str, budget: int, marker: str) -> str:
    """The page's text as the agent sees it, in at most ``budget``
    characters plus the notes.

    A page that fits is handed over whole.  One that does not first loses
    the pictures past the first few of each row of pictures, and a note
    says how many were left out, so a picture standing among the words
    (an article's chart) stays.  Then the text is cut at a sentence end,
    never inside a picture link.  Measured 2026-11-18 (rv_b9, rv_66web): a
    40-picture gallery before an article cut it from 93 sentences to 54,
    and a cap on the first 12 pictures of the page won the words back but
    dropped the article's chart behind a header's logo, share icons and
    tracking pixel."""
    if len(content) <= budget:
        return content

    def _thin(run):
        pics = _PICTURE_RE.findall(run.group(0))
        if len(pics) <= _RUN_PICTURES_SHOWN:
            return run.group(0)
        return (' '.join(pics[:_RUN_PICTURES_SHOWN])
                + f' [{len(pics) - _RUN_PICTURES_SHOWN} more pictures here not shown]')

    content = _PICTURE_RUN_RE.sub(_thin, content)
    if len(content) <= budget:
        return content
    links = [m.span() for m in _PICTURE_RE.finditer(content, 0, budget + 2048)]

    def _link_start(i):
        return next((s for s, e in links if s <= i < e), None)

    cut = content.rfind('.', 0, budget)
    while cut != -1 and _link_start(cut) is not None:
        cut = content.rfind('.', 0, _link_start(cut))
    if cut > budget * 3 // 4:
        return content[:cut + 1] + marker
    end = budget if _link_start(budget) is None else _link_start(budget)
    return content[:end] + marker


def _fallback_fetch(url: str, timeout: int, log: _ProgressLog) -> dict:
    """Fallback: requests + BeautifulSoup. No browser needed.

    The page's pictures stay in its text as ``![alt](link)``, where they
    stood, each distinct link once, the shape crawl4ai's markdown gives, so
    a caller reads the same kind of page from either path (what the agent's
    budget leaves out is decided where the budget is, _fit_for_agent).
    Live 2026-11-17 (research agent, build 75, crawl4ai not installed) a
    step that reads a chart with get_text_from_image got page text only,
    had no picture link to pass, failed three times and asked the person
    for one."""
    from urllib.parse import urljoin
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
        # A link is made whole against the page actually reached (after any
        # redirect).  A lazy picture keeps a data: stand-in in src and its
        # real link in data-src.  A picture with no web link is left out.
        page_url = getattr(resp, 'url', None) or url
        kept = set()
        for img in soup.find_all('img'):
            src = next((v.strip() for v in (img.get('src'), img.get('data-src'))
                        if v and v.strip() and not v.strip().startswith('data:')), '')
            link = urljoin(page_url, src) if src else ''
            # A repeat (a logo on every card) is dropped.
            if not link.startswith(('http://', 'https://')) or link in kept:
                img.decompose()
                continue
            kept.add(link)
            alt = ' '.join((img.get('alt') or '').split()).replace('[', '').replace(']', '')
            link = link.replace(' ', '%20').replace('(', '%28').replace(')', '%29')
            img.replace_with(f' ![{alt}]({link}) ')
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        cleaned = ' '.join(c for c in chunks if c)
        # Too little content is judged on the words alone: a page whose HTML
        # holds only a logo still needs JavaScript to be read.
        words_only = re.sub(r' ?' + _PICTURE_LINK, '', cleaned).strip()

        if not words_only or len(words_only) < 50:
            log.step("Too little content after cleanup")
            return {'success': False, 'url': url, 'error': 'Too little content extracted'}

        word_count = len(cleaned.split())
        log.step(f"Extracted {word_count} words (BeautifulSoup)")
        return {'success': True, 'url': url, 'markdown': cleaned, 'word_count': word_count}
    except Exception as e:
        log.step(f"Fallback error: {e}")
        return {'success': False, 'url': url, 'error': str(e)}


#: Longest a crawl run from inside an already-running event loop may take.
_ASYNC_BRIDGE_TIMEOUT_S = 120


def _run_async(coro, timeout=_ASYNC_BRIDGE_TIMEOUT_S):
    """Run an async coroutine from sync context.

    Inside a running event loop the coroutine gets its own loop on a worker,
    and the caller waits at most ``timeout`` seconds (TimeoutError after
    that; every caller already turns an exception into its fallback).  This
    was `with ThreadPoolExecutor(max_workers=1) as pool:` +
    result(timeout=120), and the `with` exit joined the stuck worker, so the
    120 s never released the caller (review F7, 2026-09-27).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        from core.subprocess_safe import call_bounded
        finished, value, error = call_bounded(
            lambda: asyncio.run(coro), timeout, name='hart-crawl-async')
        if not finished:
            raise TimeoutError(f'crawl did not finish within {timeout}s')
        if error is not None:
            raise error
        return value
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
        _offer_setup(log)

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
        _offer_setup(log)

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
        # Fitted to the agent's context window
        content = _fit_for_agent(result['markdown'], 8000, "\n[Content truncated]")
        parts.append(f"URL: {url}")
        parts.append(f"Words extracted: {result['word_count']}")
        parts.append(f"Content:\n{content}")
    else:
        parts.append(f"FAILED: {url}")
        parts.append(f"Error: {result['error']}")
        # Owner rule 2026-11-09: a refusal says what to do.  Live 2026-11-16
        # the desktop 4B crawled two page paths it made up (both 404) and the
        # step was given up with nothing telling it why.
        parts.append(
            "What to do: crawl a link exactly as the search results gave it; "
            "never guess or edit a page path. If no result link can be read, "
            "say so plainly instead of answering from memory.")

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
            content = _fit_for_agent(r['markdown'], 4000, " [truncated]")
            parts.append(f"\n## {r['url']}\nWords: {r['word_count']}\n{content}")
        else:
            parts.append(f"\n## {r['url']}\nFailed: {r['error']}")

    header = f"Crawled {success_count}/{len(urls)} URLs successfully"
    return header + "\n" + "\n".join(parts)
