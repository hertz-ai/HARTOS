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


logger = logging.getLogger(__name__)

# Lazy-loaded crawler instance (heavy import — Playwright/Chromium)
_crawler = None
_crawler_start = None  # its first start, while it runs (_get_crawler)
_crawler_available = None  # None = not checked yet

#: The links each request's web search gave, so a failed crawl of a link no
#: search gave can say so and name them.  Keyed by request id, bounded, and
#: dropped after a while; one request never sees another's links.  The turn's
#: window holds them too, but a tool cannot reach the window (hartos-43,
#: 2026-11-18).  Live 2026-11-18 21:05 IST the search gave
#: speechblubs.com/blog/9-effective-apps-for-speech-delay-to-help-your-child-speak
#: and the local 4B crawled speechblubs.com/apps-for-delayed-speech (404).
#: Keyed by (user id, request id) (rv_4301 S4). Every search of a request ADDS
#: its links to what the request's earlier searches gave (rv_4301 M1: a
#: replace-on-search store told the model a link from its FIRST search "was
#: not in the search results").
_SEARCH_LINKS = {}
_SEARCH_LINKS_MAX = 64
_SEARCH_LINKS_PER_REQUEST = 20
_SEARCH_LINKS_TTL_S = 900
_search_links_lock = threading.Lock()


def _link_form(url) -> str:
    """The form two links are compared in: no case, no trailing slash."""
    return str(url or '').strip().rstrip('/').lower()


def _search_key(request_id, user_id) -> tuple:
    return (str(user_id or ''), str(request_id or ''))


def remember_search_links(request_id, links, *, user_id=None, query=None) -> None:
    """Add the links a search gave this person's request to the ones its
    earlier searches gave (no repeats, newest last, at most
    _SEARCH_LINKS_PER_REQUEST); the request becomes the newest in the store.

    The query itself is never kept as a link (rv_4301 M1): a model that
    searches with a URL it made up gets that URL echoed back as a "result",
    and keeping it would let a failed crawl of the made-up page pass as one
    the search gave."""
    asked = _link_form(query) if query else ''
    links = [l for l in (links or []) if isinstance(l, str) and l
             and (not asked or _link_form(l) != asked)]
    if not request_id or not links:
        logger.info(f"search links kept: 0 for request {request_id!r}")
        return
    key = _search_key(request_id, user_id)
    with _search_links_lock:
        old = _SEARCH_LINKS.pop(key, None)
        merged = (list(old[1]) if old
                  and time.monotonic() - old[0] <= _SEARCH_LINKS_TTL_S else [])
        seen = {_link_form(l) for l in merged}
        for link in links:
            if _link_form(link) not in seen:
                merged.append(link)
                seen.add(_link_form(link))
        merged = merged[-_SEARCH_LINKS_PER_REQUEST:]
        _SEARCH_LINKS[key] = (time.monotonic(), merged)
        while len(_SEARCH_LINKS) > _SEARCH_LINKS_MAX:
            _SEARCH_LINKS.pop(next(iter(_SEARCH_LINKS)))
    logger.info(f"search links kept: {len(merged)} for request {request_id!r}")


def _searched_links() -> list:
    """The links this person's request's searches gave, or [] (none,
    expired, or no request id)."""
    try:
        from hartos.threadlocal import thread_local_data
        request_id = thread_local_data.get_request_id()
        user_id = thread_local_data.get_user_id()
    except Exception as err:
        logger.warning(f"search links: the turn's ids cannot be read ({err!r}); "
                       f"a failed crawl gets no note")
        return []
    if not request_id:
        return []
    with _search_links_lock:
        kept = _SEARCH_LINKS.get(_search_key(request_id, user_id))
    if not kept or time.monotonic() - kept[0] > _SEARCH_LINKS_TTL_S:
        logger.info(f"no search links for request {request_id!r}")
        return []
    return kept[1]


def _not_searched_note(url: str) -> str:
    """'This link was not in the search results ...' when this request
    searched and the failed link is not one any of its searches gave; ''
    otherwise."""
    links = _searched_links()
    if not links or _link_form(url) in {_link_form(l) for l in links}:
        return ''
    return ("This link was not in the search results. The search gave: "
            + ", ".join(links[-5:]) + ". Crawl one of these exactly.")


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
# owner on the one setup card; on yes, ONE background setup runs.  This crawl
# is answered by the fallback either way: nothing waits on the card or the
# setup.  A failed setup is not retried until the app starts again.
#
# The setup itself is RuntimeToolManager's 'crawl4ai' row (owner ruling A16:
# the one place a pip-in-isolated-env setup belongs): its consent card
# (ask_tool_consent, the row's 'crawl:crawl4ai' scope and words), its one
# setup in flight per tool (setup_tool_async), its pip step with the host's
# runner and Playwright's Chromium after it.  This module carried its own
# installer for the same steps (audit row H3); what stays here is the
# crawler's part: when to offer, and what a finished setup means for the
# next crawl.
_SETUP_TOOL = 'crawl4ai'
_setup_lock = threading.Lock()
_setup_future = None
_setup_failed = False
#: Why the setup failed, as RuntimeToolManager answered it (its redacted
#: reason and what to do), for the agent whose crawl is read without a
#: browser.  Owner, 2026-11-22: "Real failure reasons should be available to
#: the agent so that it can ask correct questions".
_setup_failure = ''


def _offer_setup(log: '_ProgressLog') -> None:
    """crawl4ai is missing: ask the owner (one card, deduped) and, once they
    have said yes, start the one background setup.  Never raises."""
    global _setup_future
    if _setup_failed:
        # In the crawl's progress, which every crawl tool hands the agent:
        # it knew only that the page came without JavaScript, never why.
        log.step(setup_failure_note())
        return
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        answer = runtime_tool_manager.ask_tool_consent(_SETUP_TOOL)
    except Exception as e:
        logger.debug(f"crawl4ai setup not offered: {e}")
        return
    if answer != 'granted':
        if answer == 'asked':
            log.step("Asked the owner to set crawl4ai up (JavaScript pages)")
        return
    with _setup_lock:
        if _setup_failed:       # it failed while this crawl was asking
            return
        # The manager hands back the setup already running, if one is.
        future = runtime_tool_manager.setup_tool_async(_SETUP_TOOL)
        if future is _setup_future:
            return
        _setup_future = future
    future.add_done_callback(_setup_finished)
    log.step("Setting crawl4ai up in the background (the owner allowed it)")


def _setup_finished(future) -> None:
    """The background setup ended: a failed one is not offered again in this
    run of the app; after a good one the next crawl imports crawl4ai."""
    global _crawler_available, _setup_failed, _setup_failure
    try:
        result = future.result()
    except BaseException as e:
        logger.warning(f"crawl4ai setup failed: {type(e).__name__}: {e}")
        try:
            from integrations.service_tools.runtime_manager import setup_error
            result = {'error': setup_error(_SETUP_TOOL, e)}
        except Exception:
            result = {'error': ''}
    if not isinstance(result, dict) or result.get('error') or not result.get('ready'):
        # The manager's answer is already made safe for a pip step
        # (runtime_manager._setup_reason); whatever else arrives here goes
        # through the same rule before an agent reads it.
        _setup_failure = _safe_setup_failure(
            result.get('error') if isinstance(result, dict) else '')
        _setup_failed = True
        return
    importlib.invalidate_caches()
    _crawler_available = None   # the next crawl imports it
    logger.info("crawl4ai setup: done; the next crawl uses it")


def _safe_setup_failure(error) -> str:
    """A failed setup's answer as one line an agent may read: the reason
    RuntimeToolManager gave, held to its own rule for a reason
    (runtime_manager._setup_reason: secrets replaced by
    security.secret_redactor, absolute paths cut to the file name), and a
    plain sentence when there is none or it cannot be made safe."""
    reason = ''
    try:
        from integrations.service_tools.runtime_manager import _setup_reason
        reason = _setup_reason(' '.join(str(error or '').split()), limit=None)
        if reason:
            # A stored credential's value reads as its alias, as in every
            # tool result (core.tool_logging.credential_vault).
            from core.tool_logging import credential_vault
            vault = credential_vault()
            if vault is not None:
                reason = str(vault.mask_secrets(reason))
    except Exception as e:
        logger.debug(f"crawl4ai setup failure not quoted: {e}")
        reason = ''
    if len(reason) > _SETUP_FAILURE_MAX:
        # Whatever answered: its beginning (what failed and why) is kept,
        # and the rest is pointed at, never poured into a turn.
        reason = (reason[:_SETUP_FAILURE_MAX].rstrip(' .')
                  + ' ... (cut here; the whole error is in this computer\'s log)')
    return (reason + '.') if reason else 'Why is in this computer\'s log.'


#: The most a failed setup's answer takes of a crawl's progress.  The
#: manager's own answers fit: "setting crawl4ai up failed: <step>: <reason,
#: 300 at most>. <what to do>" is under 500.
_SETUP_FAILURE_MAX = 500


def setup_failure_note() -> str:
    """One line for an agent whose page was read without a browser because
    crawl4ai's setup failed in this run of the app: that it failed, why
    (the kept reason) and what to do; '' when no setup has failed.  A crawl
    writes it in its progress (_offer_setup), and the tools that answer a
    page without that progress add it to their answer (core.agent_tools
    .data_extraction_from_url, the LangChain Data_Extraction_From_URL's
    failed page, hartos.helper.crawl4ai_batch_fetch)."""
    if not _setup_failed:
        return ''
    return ("crawl4ai could not be set up on this computer, so this page is "
            "read without JavaScript. " + _setup_failure)


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
    """Lazy-init singleton AsyncWebCrawler.

    The other pages of the first batch, asking while the one start still
    runs, wait for it: given the crawler before it was ready, crawl4ai
    0.9.6's arun started it again inside the page's timed wait, and a slow
    start read as a page timeout (rv_fa0b: a start of twice the page
    timeout kept pages 2 and 3 as timed out).  The start of an earlier
    crawl, in its own loop, is not waited for: one that failed there is
    tried again by arun, as before, and one still running there cannot be
    waited for from this loop."""
    global _crawler, _crawler_start
    if _crawler is not None:
        start = _crawler_start
        if start is not None and start.get_loop() is asyncio.get_running_loop():
            await start
        return _crawler
    log.step("Initializing browser engine (first run)...")
    _use_installed_browsers()
    from crawl4ai import AsyncWebCrawler
    _crawler = AsyncWebCrawler(
        headless=True,
        browser_type='chromium',
        verbose=False,
    )
    _crawler_start = asyncio.ensure_future(_crawler.start())
    await _crawler_start
    log.step("Browser engine ready")
    return _crawler


def _read_text(result) -> tuple:
    """(the page's text, None) when crawl4ai's ``result`` has words, else
    (None, the error to show): crawl4ai's own message, or 'No content
    extracted' when it gave none.  A page read with no text (success with
    empty or blank markdown) carries error_message None, and the agent was
    shown "FAILED ... Error: None" (rv_crawl3).  Both browser reads name it
    this way (_crawl_single, _crawl_with_cookies)."""
    if result.success and result.markdown and result.markdown.strip():
        return result.markdown, None
    if result.success:
        return None, 'No content extracted'
    return None, getattr(result, 'error_message', None) or 'No content extracted'


def _crawl4ai_page_timeout_ms() -> int:
    """The page timeout crawl4ai's arun applies when given no config:
    crawl4ai.config.PAGE_TIMEOUT, 60000 in 0.9.6.  Read after arun, and it
    never raises: the setup installs crawl4ai unpinned, and a crawl4ai
    without that name keeps 0.9.6's 60000 instead of sending every page to
    the read without a browser (rv_fa0b: read before arun, a missing name
    meant arun was never called)."""
    try:
        from crawl4ai import config
        return getattr(config, 'PAGE_TIMEOUT', 60000)
    except Exception:
        return 60000


async def _crawl_single(url: str, timeout: int, log: _ProgressLog) -> dict:
    """Crawl one URL with intermediate progress logging.

    A failure is marked ``browser_ran`` (crawl_url and crawl_urls then keep
    it, with no read without a browser, 4274c0fe1) only when the page itself
    answered: crawl4ai gave the page's status code (an error page, a page
    it judged blocked or empty), or the arun call used up the page timeout
    that applied (crawl4ai 0.9.6 reports a page that timed out through
    arun's catch-all, with no status code, after that wait).  Every other
    failure gets the read without a browser, as 456a0b705 gave it: a
    browser that crashed or whose event loop closed (the same catch-all,
    returned before the page timeout), and a page read with no text
    (success with empty markdown, named 'No content extracted', never
    None).  rv_crawl3, 21 Nov 2026.

    The arun call is as it was (owner, 21 Nov: "Leave as is").  Given no
    config, crawl4ai 0.9.6's arun uses its default CrawlerRunConfig, so the
    page timeout that applies is crawl4ai.config.PAGE_TIMEOUT (60 s,
    _crawl4ai_page_timeout_ms); the ``timeout=``, ``bypass_cache=`` and
    ``word_count_threshold=`` keywords change nothing there (the cache is
    bypassed by default, the threshold stays 1).  The wait is timed around
    arun only, never the browser start (_get_crawler: the first batch waits
    for the one start) or crawl_urls' wait for its turn.  Shapes that read
    the other way:
    a crash after the page timeout reads as a timeout (no second read).
    crawl4ai's fixed wait for the page body (wait_for_selector("body",
    timeout=30000)) gives up about 30 s after the page opened, inside the
    page timeout; under this default config (ignore_body_visibility) that
    does not fail the read, and a config where it did would read as a
    crash, so the page would be read again without a browser."""
    log.step(f"Connecting to {url}...")
    try:
        crawler = await _get_crawler(log)
        log.step(f"Rendering page (timeout={timeout}s)...")
        started = time.monotonic()
        result = await crawler.arun(
            url=url,
            word_count_threshold=50,
            timeout=timeout * 1000,
            bypass_cache=True,
        )
        waited_ms = (time.monotonic() - started) * 1000
        markdown, error = _read_text(result)
        if markdown:
            word_count = len(markdown.split())
            log.step(f"Extracted {word_count} words from {url}")
            return {
                'success': True,
                'url': url,
                'markdown': markdown,
                'word_count': word_count,
            }
        # A page that loaded and gave no text is no answer of its own.
        page_answered = not result.success and (
            getattr(result, 'status_code', None) is not None
            or waited_ms >= _crawl4ai_page_timeout_ms())
        log.step(f"Crawl returned no content: {error}")
        if not page_answered:
            return {'success': False, 'url': url, 'error': error}
        # The page's own answer (a timeout, an error page), not a browser
        # that could not run.
        return {'success': False, 'url': url, 'error': error, 'browser_ran': True}
    except Exception as e:
        # An exception with no message (Windows: NotImplementedError when the
        # loop cannot start the browser) is named, never an empty error.
        reason = str(e) or type(e).__name__
        log.step(f"Crawl error: {reason}")
        return {'success': False, 'url': url, 'error': reason}


#: One picture in a page's markdown, as both read paths write it.
_PICTURE_LINK = r'!\[[^\]]*\]\([^)\s]*\)'
_PICTURE_RE = re.compile(_PICTURE_LINK)
#: A short caption: at most about 40 characters with no sentence end (each
#: figure's "Photo 3", a share icon's hidden "Share on X").
_CAPTION = r'[^!\[\].?\s][^!\[\].?\n]{0,38}'
#: A picture of a captioned row: its caption, and the next picture after it.
_CAPTIONED_PICTURE = rf'{_PICTURE_LINK}\s*{_CAPTION}\s*(?={_PICTURE_LINK})'
#: The last picture of a captioned row: no picture next to it, and the text
#: after it, a caption's length, taken along (_last_caption_end says how
#: much of that is its caption; the rest stays as it stood).
_LAST_CAPTIONED_PICTURE = rf'{_PICTURE_LINK}(?!\s*{_PICTURE_LINK})(?:\s*{_CAPTION})?'
#: Pictures standing next to each other: with no words between (a gallery,
#: a strip of thumbnails), or each with its own short caption (a captioned
#: gallery, a row of share icons).  A row is one kind or the other, so a row
#: of share icons ends with its last label where a plain gallery starts, and
#: a strip ends before a captioned gallery's first picture (rv_crawl3 on
#: 2083328e3: the icons and the gallery made one row of 44, which showed 3
#: icons and no gallery picture).
_PICTURE_RUN_RE = re.compile(
    rf'{_CAPTIONED_PICTURE}(?:(?:{_CAPTIONED_PICTURE})+(?:{_LAST_CAPTIONED_PICTURE})?'
    rf'|{_LAST_CAPTIONED_PICTURE})'
    rf'|{_PICTURE_LINK}(?:\s*{_PICTURE_LINK}(?!\s*{_CAPTION}\s*{_PICTURE_LINK}))+')
#: How many pictures of such a row the agent still sees when the page is
#: over its budget.  The first stays, so a chart leading a gallery is kept.
_RUN_PICTURES_SHOWN = 3
#: A row wholly inside the budget is kept whole while it takes at most this
#: part of the budget (a quarter); a longer one is a gallery before the
#: article and is thinned.
_ROW_KEPT_WHOLE_PART = 4
#: An unclosed picture link at the very end of a cut text (a plain cut can
#: end inside one: '![c5](https://x.exa').
_PARTIAL_LINK_AT_END = re.compile(r'!\[[^\]]*(?:\](?:\([^)\s]*)?)?$')
#: A full stop that ends a sentence: a space or the end follows it.
_SENTENCE_END = re.compile(r'\.(?=\s|$)')


def _measure(text: str) -> tuple:
    """(pictures, sentences) in a text the agent gets: whole ``![..](..)``
    links, and whole sentences ending in a full stop outside them; a half
    link at the end counts for neither.  The fit is held to the plain cut
    on exactly these two counts (rv_fbc2's probe counts the same)."""
    text = _PARTIAL_LINK_AT_END.sub('', text)
    return (len(_PICTURE_RE.findall(text)),
            len(_SENTENCE_END.findall(_PICTURE_RE.sub(' ', text))))


def _fit_for_agent(content: str, budget: int, marker: str) -> str:
    """The page's text as the agent sees it, in at most ``budget``
    characters plus the notes, and never worse than the plain cut.

    The plain cut is what the live code (9132e1d1b) gave: the first
    ``budget`` characters.  The fitted text (_thin_and_cut) is used only
    when it has at least as many pictures AND at least as many sentences
    as the plain cut (_measure); otherwise the agent gets the plain cut,
    with a half picture link at its end trimmed.  rv_fbc2, 2026-11-20: a
    row kept whole was then dropped by the sentence cut when no full stop
    followed it inside the budget (live 9 or 10 pictures, fitted 0), and a
    thinned gallery showed 3 pictures where the live cut showed 40; owner:
    not worse than before on anything measured."""
    if len(content) <= budget:
        return content
    fitted = _thin_and_cut(content, budget, marker)
    plain = _PARTIAL_LINK_AT_END.sub('', content[:budget]) + marker
    # In practice the picture count decides.  The fitted text CAN have fewer
    # sentences (the note " [1 more pictures here not shown]" is longer than
    # a tiny hidden picture, so later text moves past the budget: rv_fbc2,
    # 127 against 129), but then it has fewer pictures too: a row thinned
    # inside the budget always loses pictures the plain cut shows, and a row
    # reaching past the budget leaves the plain cut no sentence after its
    # start.  The sentence check stays because the rule is "fewer pictures
    # OR fewer sentences" (rv_fbc2: the pictures-only mutant is equivalent).
    if any(f < p for f, p in zip(_measure(fitted), _measure(plain))):
        return plain
    return fitted


def _last_caption_end(run, pics) -> int:
    """Where the last picture's caption ends in a row of pictures (``run``,
    a _PICTURE_RUN_RE match, ``pics`` its pictures), as an index in it.

    A plain row ends on its last picture.  A row of share icons followed by
    a gallery ends where the gallery starts: its last label is all the text
    after its last picture.  A captioned row that ends before other text
    took that text along, and only the caption it begins with is the last
    picture's: the caption before it again, numbers aside ("Photo 38", then
    "Photo 39").  Anything else is the page's own text and stays."""
    row = run.group(0)
    last = pics[-1].end()
    if last == len(row):
        return last
    if _PICTURE_RE.match(run.string, run.end()):
        return len(row.rstrip())
    words = row[pics[-2].end():pics[-1].start()].split()
    shape = re.compile(r'\s*' + r'\s+'.join(r'\d+' if w.isdigit() else re.escape(w)
                                            for w in words)
                       + ('' if words[-1].isdigit() else r'(?!\w)'))
    caption = shape.match(run.string, run.start() + last)
    if caption is None or caption.end() > run.end():
        return last
    return caption.end() - run.start()


def _thin_and_cut(content: str, budget: int, marker: str) -> str:
    """The page's text with its rows of pictures thinned, cut to at most
    ``budget`` characters plus the notes (_fit_for_agent decides whether
    the agent gets it).

    A page that fits is handed over whole.  One that does not first loses
    the pictures past the first few of each row of pictures that reaches
    past the budget or takes more than a quarter of it (a gallery before
    the article), and a note says how many were left out, so a picture
    standing among the words (an article's chart) stays.  A smaller row
    wholly inside the budget is not thinned.  Then the text is cut at a
    sentence end, never inside a picture link; that cut can still drop
    such a row when no full stop follows it inside the budget (rv_fbc2's
    S6 and S9), and the guard in _fit_for_agent then restores it.

    The agent gets this text only if it has at least as many pictures AND
    as many sentences as the plain cut; otherwise the plain cut
    (_fit_for_agent).  For example the captioned gallery at 8000 is
    thinned to 3 pictures and 89 sentences against the plain cut's 40 and
    49, and the agent gets 40 and 49.

    History: rv_b9 and rv_66web (2026-11-18) measured a 40-picture gallery
    before an article taking the article from 93 sentences to 54, which
    the thinning won back; rv_d0ce (2026-11-20) found five charts mid-page
    thinned to three; rv_fbc2 (2026-11-20) found that a thinned gallery
    loses pictures the live cut shows, so such a page now reaches the
    agent as the live cut gave it."""
    if len(content) <= budget:
        return content

    def _thin(run):
        row = run.group(0)
        pics = list(_PICTURE_RE.finditer(row))
        # Kept whole or not by the span of its pictures, as before a
        # captioned row took its last caption along.
        end = run.start() + pics[-1].end()
        if end <= budget and end - run.start() <= budget // _ROW_KEPT_WHOLE_PART:
            return row
        if len(pics) <= _RUN_PICTURES_SHOWN:
            return row
        # The first pictures stay as they stood, each with its caption; the
        # hidden ones go with theirs, the last one's too (rv_crawl3: "not
        # shown] Photo 39"), and what follows the row stays.
        kept = row[:pics[_RUN_PICTURES_SHOWN].start()].rstrip()
        cut = _last_caption_end(run, pics)
        rest = row[cut:]
        follows = rest + run.string[run.end():run.end() + 1]
        if cut > pics[-1].end() and follows and not follows[0].isspace():
            rest = ' ' + rest
        return kept + f' [{len(pics) - _RUN_PICTURES_SHOWN} more pictures here not shown]' + rest

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


# Elements a browser starts on a line or in a box of their own: what one
# holds is not part of the word before or after it.
_BLOCK_TAGS = frozenset((
    'address', 'article', 'aside', 'blockquote', 'br', 'caption', 'dd',
    'details', 'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'form',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'main', 'ol', 'option',
    'p', 'pre', 'section', 'summary', 'table', 'tbody', 'td', 'tfoot', 'th',
    'thead', 'tr', 'ul'))


def _keep_words_apart(soup) -> None:
    """Put a space on both sides of every block element (_BLOCK_TAGS), so
    get_text() does not run the words of two boxes together.  A page sent
    without line breaks gave "ItemPriceRice42Dal95" for a table and
    "...00000Emailsales@ex.org" for two lines of labels (review of
    8c595f0db, measured on this fallback).  The spaces are collapsed with
    the rest of the page's whitespace.

    Inline elements get no space of their own: whether two of them are one
    word or two is what the page's own whitespace says, as a browser shows
    it.  A space before every element that follows another split a price
    built from spans ("Rs 1,299. 00"), highlighted code ("os . path . join
    ( a )"), a heading revealed letter by letter ("W e l c o m e"), an email
    address and a word set half bold, half italic ("Hel lo") (review of
    5fcacc57d).  A label and its value in two neighbouring spans with no
    whitespace between them read joined ("Phone:+91"), as they did before
    5fcacc57d."""
    for el in soup.find_all(True):
        if el.name in _BLOCK_TAGS:
            el.insert_before(' ')
            el.insert_after(' ')


def _page_text(html: str, page_url: str) -> str:
    """The words of an HTML page as one line of text, its pictures kept
    where they stood as ``![alt](link)``: the one reading of a page fetched
    without a browser, for the public read (_fallback_fetch) and the read
    with a cookie jar (_fallback_fetch_with_cookies), which carried a copy
    of it without the pictures or the spacing (audit row H19)."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    # A link is made whole against the page actually reached (after any
    # redirect).  A lazy picture keeps a data: stand-in in src and its
    # real link in data-src.  A picture with no web link is left out.
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
    _keep_words_apart(soup)
    text = soup.get_text()
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    return ' '.join(c for c in chunks if c)


#: Characters kept from each feed item's summary, its markup dropped.
_FEED_SUMMARY_CHARS = 120


#: A feed title is read as markup only when it looks like HTML: a closing
#: tag or an entity reference (feedparser's own test).
_LOOKS_LIKE_HTML = re.compile(r'</[A-Za-z][\w-]*\s*>|&#?\w+;')


def _plain(markup: str) -> str:
    """The words of a piece of feed HTML, one line.  Inline tags keep a word
    whole and block tags part words, as on a page (_keep_words_apart):
    get_text(' ') read "C<sub>2</sub>H<sub>4</sub>" as "C 2 H 4" (rv_feed1
    on 06d464676)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup or '', 'html.parser')
    _keep_words_apart(soup)
    return ' '.join(soup.get_text().split())


def _feed_title(title: str) -> str:
    """A feed's or an item's title, one line.  RSS titles are plain text by
    the spec: read as markup every time, a programming feed's
    "std::vector<int>" lost "<int>" and "Use the <div> tag" lost "<div>"
    (rv_feed1 on 06d464676).  So a title is read as markup only when it
    looks like HTML (_LOOKS_LIKE_HTML)."""
    if _LOOKS_LIKE_HTML.search(title or ''):
        return _plain(title)
    return ' '.join((title or '').split())


def _feed_items_text(body: str, url: str, cut: bool = False):
    """``(text, item_count)`` when ``body`` is an RSS, Atom or JSON feed
    (integrations.social.feed_import.read_feed, the one feed reader), else
    None, and the body is read as a page.  ``cut``: the body stopped at the
    read limit, so the feed is longer than its items here, and the first
    line says so; so it does when the reader stopped at a break in the
    feed's XML (metadata.stopped_early).

    One line per item, in the feed's order: its title (markup dropped),
    date and link; then up to _FEED_SUMMARY_CHARS characters of its
    summary as plain words.  Read as page text, a feed is mostly
    its items' HTML bodies, and the first few thousand characters the agent
    keeps hold one or two stories.  News Channel demo, 28 Nov 2026: The
    Verge's feed was 412,067 characters of page text, the model received
    3,027 of them (one story) and made up two more with links that do not
    exist."""
    from integrations.social.feed_import import read_feed
    feed = read_feed(body, url)
    if feed is None:
        return None
    metadata, items = feed
    if cut:
        more = ', cut at the read limit: the feed has more'
    elif metadata.stopped_early:
        more = f', read only in part: {metadata.stopped_early}'
    else:
        more = ''
    lines = [f"Feed: {_feed_title(metadata.title) or url} ({len(items)} items{more})"]
    for n, item in enumerate(items, 1):
        when = item.published.strftime('%Y-%m-%d %H:%M') if item.published else ''
        head = ' | '.join(p for p in (_feed_title(item.title), when, item.link or '') if p)
        lines.append(f"{n}. {head}")
        words = _plain(item.content)
        if words:
            lines.append('   ' + words[:_FEED_SUMMARY_CHARS]
                         + ('...' if len(words) > _FEED_SUMMARY_CHARS else ''))
    return '\n'.join(lines), len(items)


def _bounded_text(resp) -> tuple:
    """``(text, bytes_read, cut)``: the body of a streamed requests response
    read up to core.constants.MAX_PAYLOAD_BYTES (the node's one body limit,
    2 MiB unless configured) and decoded as ``resp.text`` decodes it (the
    declared encoding, else the detected one, undecodable bytes replaced).
    ``cut`` when the page went on past the limit.  The whole body used to be
    read and parsed: a 20 MB page took 2.1 s and 255 MB (review of
    8c595f0db); what an agent is shown is a few thousand characters."""
    import requests as _req
    from core.constants import MAX_PAYLOAD_BYTES
    parts, size = [], 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            parts.append(chunk)
            size += len(chunk)
            if size > MAX_PAYLOAD_BYTES:
                break
    finally:
        resp.close()
    body = b''.join(parts)
    cut = len(body) > MAX_PAYLOAD_BYTES
    body = body[:MAX_PAYLOAD_BYTES]
    encoding = resp.encoding
    if not encoding:
        encoding = (_req.compat.chardet.detect(body) or {}).get('encoding')
    try:
        text = body.decode(encoding or 'utf-8', errors='replace')
    except LookupError:
        text = body.decode('utf-8', errors='replace')
    return text, len(body), cut


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
    import requests as _req
    log.step(f"Fetching {url} (requests fallback)...")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        resp = _req.get(url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()
        html, size, cut = _bounded_text(resp)
        log.step(f"HTTP {resp.status_code}, {size} bytes received"
                 + (" (a longer page: read up to the size limit)" if cut else ""))

        feed = _feed_items_text(html, getattr(resp, 'url', None) or url, cut=cut)
        if feed is not None:
            text, count = feed
            log.step(f"Read as a feed: {count} items")
            return {'success': True, 'url': url, 'markdown': text,
                    'word_count': len(text.split())}

        cleaned = _page_text(html, getattr(resp, 'url', None) or url)
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
        # Named as the public read names it: a page with no text is never a
        # success with 0 words or "Error: None" (rv_fa0b).
        markdown, error = _read_text(result)
        if markdown:
            word_count = len(markdown.split())
            log.step(f"Extracted {word_count} words from {url}")
            return {
                'success': True,
                'url': url,
                'markdown': markdown,
                'word_count': word_count,
                'connection_mechanism': connection_mechanism,
            }
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


def _keep_host_only_cookies_home(sess, host_only: dict) -> None:
    """Hold each cookie in ``host_only`` (name -> host) to that one host on
    every request ``sess`` sends, a redirect's included.

    requests' jar has no host-only cookie: one set for example.com is also
    sent to every subdomain of it, so a redirect from example.com to
    evil.example.com carried the person's login (review of e528a820a,
    measured).  The jar's own policy cannot say otherwise, because requests
    builds each request's cookies in a fresh jar.  So the Cookie header is
    read where every request passes, the session's transports, and a
    host-only cookie is taken out of any request to another host.  A cookie
    with a domain of its own is the jar's to place, as before."""
    if not host_only:
        return
    from urllib.parse import urlsplit

    def held(send):
        def _send(request, *args, **kwargs):
            header = request.headers.get('Cookie')
            if header:
                host = (urlsplit(request.url).hostname or '').lower()
                kept = [part for part in (p.strip() for p in header.split(';')) if part
                        and host_only.get(part.split('=', 1)[0].strip(), host).lower() == host]
                if kept:
                    request.headers['Cookie'] = '; '.join(kept)
                else:
                    del request.headers['Cookie']
            return send(request, *args, **kwargs)
        return _send
    for adapter in sess.adapters.values():
        adapter.send = held(adapter.send)


def _fallback_fetch_with_cookies(url: str, cookies: list, timeout: int) -> dict:
    """Last-resort: requests + cookie jar, no JS execution.

    The body is read and turned into text by the one plain reader
    (_bounded_text, _page_text), as the public read's is: this carried a
    copy that read a page of any size whole and ran the words of
    neighbouring boxes together.  The answer keeps its own shape: success is
    HTTP 200 whatever the page holds (its text comes back either way),
    ``status_code``, the text cut at 50000 characters, ``word_count`` of the
    text read, ``connection_mechanism`` 'public_http'."""
    import requests as _req
    log = _ProgressLog()
    log.step(f"Fetching {url} (requests + cookies, no JS)...")
    try:
        from urllib.parse import urlsplit
        sess = _req.Session()
        # name -> the one host a cookie that names no domain belongs to
        host_only = {}
        unplaced = 0
        for c in cookies:
            if c.get('name') and c.get('value'):
                # A cookie that names no domain belongs to one host: its own
                # ``url``'s (Playwright's other way to place a cookie), else
                # the page's.  Never '': that is the jar's "any host", and a
                # redirect then carried the person's saved login to whatever
                # host it led to (review of c2717ae4f, measured); None made
                # the jar raise.  A cookie that gives a ``url`` no host can
                # be read from is not sent anywhere, and the progress says
                # so: where it was meant to go is not known, and the page's
                # host would be a guess.
                domain = c.get('domain')
                if not domain:
                    placed_by = c.get('url')
                    domain = urlsplit(str(placed_by if placed_by else url)).hostname
                    if not domain:
                        unplaced += 1
                        continue
                    host_only[c['name']] = domain
                sess.cookies.set(c['name'], c['value'],
                                 domain=domain, path=c.get('path', '/'))
        if unplaced:
            log.step(f"{unplaced} cookie(s) not sent: the address each names could not be read")
        _keep_host_only_cookies_home(sess, host_only)
        resp = sess.get(url, timeout=timeout, stream=True,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        html, _size, _cut = _bounded_text(resp)
        try:
            # Every run of whitespace is one space, as this read always gave.
            text = ' '.join(_page_text(html, getattr(resp, 'url', None) or url).split())
        except Exception:
            text = html
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
            # _crawl_single reports its own failure instead of raising; a
            # browser that could not run still gets the read without one.
            # A page the browser read and that failed (a timeout, an error
            # page) fails as it did before 58d250c50: a second read only
            # added its time (one page 1.0 to 2.1 s) and handed back a
            # JavaScript-only page's "enable JavaScript" notice as its
            # content (rv_66web).
            if result.get('success') or result.get('browser_ran'):
                result['progress'] = log.text()
                return result
            log.step(f"crawl4ai could not read it ({result.get('error')}), "
                     f"falling back to requests")
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
            # A page the browser could not run for is read without one; a
            # page it read and that failed keeps its failure (crawl_url).
            results = [r if r['success'] or r.get('browser_ran')
                       else _fallback_fetch(u, timeout, log)
                       for u, r in zip(urls, results)]
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
        note = _not_searched_note(url)
        if note:
            parts.append(note)

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
            note = _not_searched_note(r['url'])
            parts.append(f"\n## {r['url']}\nFailed: {r['error']}"
                         + (f"\n{note}" if note else ''))

    header = f"Crawled {success_count}/{len(urls)} URLs successfully"
    return header + "\n" + "\n".join(parts)
