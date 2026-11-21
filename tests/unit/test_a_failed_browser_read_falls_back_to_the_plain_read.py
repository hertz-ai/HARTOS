"""A page the browser could not read is read without one.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 20:21 IST,
a test account): the research agent's creation step 2 ("crawl4ai: Fetch the
top result pages") now had crawl4ai attached and called crawl4ai_crawl about
twenty times.  Every call came back "FAILED: <url> / Error: " with nothing
after it.  crawl4ai is installed, but on Windows the process-wide
WindowsSelectorEventLoopPolicy (hart_intelligence_entry, for autobahn) gives
the crawl's loop a selector loop, which cannot start Playwright's browser
process: NotImplementedError with an empty message.  _crawl_single catches
that and RETURNS a failure, so crawl_url's fallback to the read without a
browser (which runs only when an exception escapes) never ran, and the agent
read no page at all.

THE RULE
  * crawl_url: a crawl4ai result that did not succeed falls to the existing
    browserless read (_fallback_fetch), as an escaped exception already did;
  * crawl_urls: each page the batch could not read is read without a browser,
    in its place;
  * a successful crawl4ai read is returned as before, with no second fetch;
  * an exception with no message is named (its type), never an empty error.

rv_66web's review of 58d250c50 (18 Nov 2026), and the owner's rule on
20 Nov ("There should not be any regression"): the fallback also ran when the
browser DID read the page and the page itself failed (a timeout, an error
page).  The parent failed honestly there.  The commit added a second read
(one page 1.0 to 2.1 s, a batch of 6 2.0 to 8.0 s) and, for a page that
only works with JavaScript, handed back its "enable JavaScript" notice as
the page's content.  So:
  * a page the browser read and that failed keeps its failure, with no
    second read, in a single crawl and in a batch;
  * a browser that could not run (the live NotImplementedError) still gets
    the read without one.

rv_crawl3's review of 4274c0fe1 (21 Nov 2026): in crawl4ai 0.9.6 (the
installed one) a browser that crashed or whose event loop closed comes back
through arun's catch-all as success=False with no status code, and so does
a page that timed out, after the page timeout.  4274c0fe1 kept every such
failure with no read without a browser, where 456a0b705 read the crashed
page that way; and a page read with no text showed the agent "Error: None".
So, through the real _crawl_single on a fake clock:
  * a catch-all before the page timeout (a crash) gets the read without a
    browser; one at or after the page timeout (a timeout) keeps its own
    error with no second read; a page with a status code (an error page)
    keeps its failure;
  * a page read with no text gets the read without a browser and is named
    'No content extracted', never None;
  * arun is called as before (owner, 21 Nov: "Leave as is"): its config is
    crawl4ai's default (threshold 1, cache bypassed, PAGE_TIMEOUT), checked
    on every call, with the same keywords; the timeout check compares with
    PAGE_TIMEOUT, and the wait is timed around arun only, never the browser
    start.

Nothing here reaches the network: crawl4ai and requests are replaced.

    python -m pytest tests/unit/test_a_failed_browser_read_falls_back_to_the_plain_read.py -q --noconftest
"""
import sys
import time
import types
from types import SimpleNamespace

import pytest
import requests

from integrations import web_crawler as wc

URL = 'https://www.example.org/post/outcomes'
PAGE = """<html><body><h1>Speech therapy outcomes</h1>
<p>Children who practised every day improved more than those who practised weekly.</p>
</body></html>"""


class _FakePage:
    def __init__(self, url):
        self.text = PAGE
        self.content = PAGE.encode()
        self.status_code = 200
        self.url = url

    def raise_for_status(self):
        pass


def _browser(monkeypatch, outcome):
    """crawl4ai present; each URL's browser read gives ``outcome(url)``."""
    asked = []

    async def fake_single(url, timeout, log):
        asked.append(url)
        return outcome(url)
    monkeypatch.setattr(wc, '_check_available', lambda: True)
    monkeypatch.setattr(wc, '_crawl_single', fake_single)
    return asked


def _plain(monkeypatch):
    fetched = []

    def fake_get(url, headers=None, timeout=None, **kw):
        fetched.append(url)
        return _FakePage(url)
    monkeypatch.setattr(requests, 'get', fake_get)
    return fetched


def _failed(url):
    return {'success': False, 'url': url, 'error': 'NotImplementedError'}


def test_a_page_the_browser_could_not_read_is_read_without_one(monkeypatch):
    _browser(monkeypatch, _failed)
    fetched = _plain(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is True, result
    assert 'practised every day' in result['markdown']
    assert fetched == [URL]
    assert 'NotImplementedError' in result['progress'], result['progress']


def test_a_page_the_browser_read_is_not_fetched_again(monkeypatch):
    _browser(monkeypatch, lambda u: {'success': True, 'url': u,
                                     'markdown': 'Rendered words of the page.',
                                     'word_count': 5})
    fetched = _plain(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['markdown'] == 'Rendered words of the page.' and fetched == []


def test_the_agent_tool_reads_the_page_when_the_browser_cannot(monkeypatch):
    _browser(monkeypatch, _failed)
    _plain(monkeypatch)
    out = wc.crawl_url_for_agent(URL)
    assert 'FAILED' not in out, out
    assert 'practised every day' in out, out


def test_each_page_of_a_batch_the_browser_could_not_read_is_read_in_its_place(monkeypatch):
    good = 'https://www.example.org/rendered'
    _browser(monkeypatch, lambda u: ({'success': True, 'url': u, 'markdown': 'Rendered.',
                                      'word_count': 1} if u == good else _failed(u)))
    fetched = _plain(monkeypatch)
    results = wc.crawl_urls([URL, good])
    assert [r['url'] for r in results] == [URL, good]
    assert results[0]['success'] is True and 'practised every day' in results[0]['markdown']
    assert results[1]['markdown'] == 'Rendered.'
    assert fetched == [URL]


def test_a_browser_error_with_no_message_is_named(monkeypatch):
    async def no_browser(log):
        raise NotImplementedError()
    monkeypatch.setattr(wc, '_get_crawler', no_browser)
    result = wc._run_async(wc._crawl_single(URL, 5, wc._ProgressLog()))
    assert result == {'success': False, 'url': URL, 'error': 'NotImplementedError'}, result


JS_ONLY = ('<html><body><div id="root"></div><noscript>You need to enable JavaScript '
           'to run this app. This site uses cookies to improve your experience and '
           'our privacy policy explains how.</noscript></body></html>')


def _crawl4ai_096(monkeypatch, page_timeout=60000):
    """crawl4ai 0.9.6 as the read depends on it: crawl4ai.config's
    PAGE_TIMEOUT (60000) and MIN_WORD_THRESHOLD (1), and CrawlerRunConfig
    with 0.9.6's defaults for word_count_threshold (MIN_WORD_THRESHOLD),
    cache_mode (CacheMode.BYPASS) and page_timeout (PAGE_TIMEOUT)."""
    pkg = types.ModuleType('crawl4ai')
    config = types.ModuleType('crawl4ai.config')
    config.PAGE_TIMEOUT, config.MIN_WORD_THRESHOLD = page_timeout, 1

    class CacheMode:
        ENABLED, BYPASS = 'enabled', 'bypass'

    class CrawlerRunConfig:
        def __init__(self, word_count_threshold=config.MIN_WORD_THRESHOLD,
                     cache_mode=CacheMode.BYPASS, page_timeout=config.PAGE_TIMEOUT, **settings):
            self.word_count_threshold, self.cache_mode = word_count_threshold, cache_mode
            self.page_timeout, self.settings = page_timeout, settings
    pkg.CrawlerRunConfig, pkg.CacheMode, pkg.config = CrawlerRunConfig, CacheMode, config
    monkeypatch.setitem(sys.modules, 'crawl4ai', pkg)
    monkeypatch.setitem(sys.modules, 'crawl4ai.config', config)


def _page_timeout(config):
    """The wait of a page that timed out: the page timeout that applied."""
    return config.page_timeout / 1000


def _the_default():
    """(threshold, cache mode, page timeout, anything else set) of crawl4ai's
    default run config: what the read used before (owner: "Leave as is")."""
    pkg = sys.modules['crawl4ai']
    return (pkg.config.MIN_WORD_THRESHOLD, pkg.CacheMode.BYPASS, pkg.config.PAGE_TIMEOUT, {})


def _real_browser(monkeypatch, arun=None, start_error=None, takes=0.0, start_takes=0.0):
    """crawl4ai 0.9.6 present and the REAL _crawl_single, on a fake clock
    (web_crawler's time.monotonic); the browser either cannot start
    (``start_error``) or starts in ``start_takes`` seconds, and reads each
    page with ``arun`` in ``takes`` seconds (a number, or a function of the
    config that applied: the one given, else crawl4ai's default, as 0.9.6's
    arun does).  Every arun call fails the test unless the config that
    applied is crawl4ai's default (threshold 1, cache bypassed, PAGE_TIMEOUT),
    so none of them drifts unseen.  Returns (config given, config that
    applied, keywords) for each arun call."""
    clock = [1000.0]
    fake_time = types.ModuleType('time')
    fake_time.__dict__.update(time.__dict__)
    fake_time.monotonic = lambda: clock[0]
    monkeypatch.setattr(wc, 'time', fake_time)
    _crawl4ai_096(monkeypatch)
    given = []

    class _Crawler:
        async def arun(self, url, config=None, **kw):
            applied = config or sys.modules['crawl4ai'].CrawlerRunConfig()
            given.append((config, applied, kw))
            ran_with = (applied.word_count_threshold, applied.cache_mode,
                        applied.page_timeout, applied.settings)
            if ran_with != _the_default():
                # A BaseException: _crawl_single's "except Exception" cannot
                # turn it into a failed read.
                pytest.fail(f'arun ran with {ran_with}, not the default {_the_default()}')
            clock[0] += takes(applied) if callable(takes) else takes
            return arun(url)

    async def fake_get_crawler(log):
        clock[0] += start_takes
        if start_error is not None:
            raise start_error
        return _Crawler()
    monkeypatch.setattr(wc, '_check_available', lambda: True)
    monkeypatch.setattr(wc, '_get_crawler', fake_get_crawler)
    return given


def _timed_out(url):
    """crawl4ai 0.9.6: a page timeout comes back through arun's catch-all,
    with no status code (it takes the page timeout: ``_page_timeout``)."""
    return SimpleNamespace(success=False, markdown=None, html='', status_code=None,
                           error_message='Timeout 30000ms exceeded')


def _serve_js_only(monkeypatch):
    fetched = []

    def fake_get(url, headers=None, timeout=None, **kw):
        fetched.append(url)
        page = _FakePage(url)
        page.text, page.content = JS_ONLY, JS_ONLY.encode()
        return page
    monkeypatch.setattr(requests, 'get', fake_get)
    return fetched


def test_a_page_the_browser_read_and_that_timed_out_fails_honestly(monkeypatch):
    _real_browser(monkeypatch, arun=_timed_out, takes=_page_timeout)
    fetched = _serve_js_only(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is False, result
    assert 'Timeout 30000ms exceeded' in result['error'], result
    assert fetched == [], 'the page was read a second time'
    out = wc.crawl_url_for_agent(URL)
    assert 'FAILED' in out and 'enable JavaScript' not in out, out


def test_a_batch_keeps_each_failure_the_browser_read(monkeypatch):
    _real_browser(monkeypatch, arun=_timed_out, takes=_page_timeout)
    fetched = _serve_js_only(monkeypatch)
    other = 'https://www.example.org/other'
    results = wc.crawl_urls([URL, other])
    assert [r['url'] for r in results] == [URL, other]
    assert [r['success'] for r in results] == [False, False], results
    assert fetched == []


def test_a_browser_that_cannot_start_still_gets_the_read_without_one(monkeypatch):
    """The live shape (18 Nov): the browser process cannot start on a
    selector loop, NotImplementedError with no message, through the real
    _crawl_single."""
    _real_browser(monkeypatch, start_error=NotImplementedError())
    fetched = _plain(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is True and 'practised every day' in result['markdown'], result
    assert fetched == [URL]
    fetched.clear()
    results = wc.crawl_urls([URL, URL + '/2'])
    assert [r['success'] for r in results] == [True, True] and fetched == [URL, URL + '/2']


def _catch_all(error):
    """crawl4ai 0.9.6's catch-all in arun (async_webcrawler.py 719-737):
    success=False, html "", no status code, "Unexpected error in _crawl_web"."""
    return lambda url: SimpleNamespace(
        success=False, markdown=None, html='', status_code=None,
        error_message=f'Unexpected error in _crawl_web at line 470 in arun '
                      f'(async_webcrawler.py):\nError: {error}')


_crashed = _catch_all('Event loop is closed')
_goto_timed_out = _catch_all('Failed on navigating ACS-GOTO:\nPage.goto: Timeout 60000ms exceeded.')


def test_a_browser_that_crashed_inside_arun_gets_the_read_without_one(monkeypatch):
    """rv_crawl3 M: the catch-all 0.01 s into arun, long before the page
    timeout, is a crash (a closed event loop), not the page's answer:
    456a0b705 read the page without a browser, single and in a batch."""
    _real_browser(monkeypatch, arun=_crashed, takes=0.01)
    fetched = _plain(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is True and 'practised every day' in result['markdown'], result
    assert fetched == [URL] and 'Event loop is closed' in result['progress'], result['progress']
    fetched.clear()
    results = wc.crawl_urls([URL, URL + '/2'])
    assert [r['success'] for r in results] == [True, True] and fetched == [URL, URL + '/2']


@pytest.mark.parametrize('after_s', [0.0, 25.0])
def test_a_catch_all_at_or_after_the_page_timeout_is_the_page_timing_out(monkeypatch, after_s):
    """The same catch-all once arun used up the page timeout it was given
    (exactly, or 25 s more): the page timed out.  It fails with its own
    error, single and in a batch, and is not read again (4274c0fe1)."""
    _real_browser(monkeypatch, arun=_goto_timed_out,
                  takes=lambda config: _page_timeout(config) + after_s)
    fetched = _serve_js_only(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is False and 'Timeout 60000ms exceeded' in result['error'], result
    assert fetched == [], 'the page was read a second time'
    out = wc.crawl_url_for_agent(URL)
    assert 'FAILED' in out and 'Timeout 60000ms exceeded' in out, out
    assert 'enable JavaScript' not in out, out
    results = wc.crawl_urls([URL, URL + '/2'])
    assert [r['success'] for r in results] == [False, False] and fetched == [], results


def test_a_page_with_a_status_code_keeps_its_failure(monkeypatch):
    """A page that answered (crawl4ai 0.9.6 judged its HTTP 500 page a
    block) keeps its failure with no second read, however fast."""
    _real_browser(monkeypatch, takes=0.2, arun=lambda url: SimpleNamespace(
        success=False, markdown=None, status_code=500,
        html='<html><body><h1>Access Denied</h1></body></html>',
        error_message='Blocked by anti-bot protection: Access Denied on short page '
                      '(HTTP 500, 49 bytes)'))
    fetched = _serve_js_only(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is False and 'HTTP 500' in result['error'], result
    assert fetched == []
    results = wc.crawl_urls([URL, URL + '/2'])
    assert [r['success'] for r in results] == [False, False] and fetched == [], results


@pytest.mark.parametrize('markdown', ['', '  \n '])
def test_a_page_read_with_no_text_gets_the_read_without_a_browser(monkeypatch, markdown):
    """rv_crawl3 M: crawl4ai read the page (success) and kept no text; its
    error_message is None there, and the agent was shown "FAILED ... Error:
    None".  456a0b705 read such a page without a browser.  It is named
    'No content extracted' and read that way."""
    _real_browser(monkeypatch, arun=lambda url: SimpleNamespace(
        success=True, markdown=markdown, html='<html><body><div id="root"></div></body></html>',
        status_code=200, error_message=None))
    result = wc._run_async(wc._crawl_single(URL, 30, wc._ProgressLog()))
    assert result == {'success': False, 'url': URL, 'error': 'No content extracted'}, result
    fetched = _plain(monkeypatch)
    out = wc.crawl_url_for_agent(URL)
    assert 'FAILED' not in out and 'practised every day' in out, out
    assert 'None' not in out and '(No content extracted)' in out, out
    assert fetched == [URL]


def test_arun_runs_with_crawl4ai_s_default_config_and_the_same_keywords(monkeypatch):
    """The read is unchanged (owner, 21 Nov: "Leave as is"): crawl4ai 0.9.6
    ignores arun's word_count_threshold=, timeout= and bypass_cache=
    keywords, so the config that applies is its default: threshold 1
    (MIN_WORD_THRESHOLD), the cache bypassed, the page timeout PAGE_TIMEOUT
    (60000), the wait the timeout check compares with.  The keywords are as
    before."""
    given = _real_browser(monkeypatch, arun=_crashed)
    wc._run_async(wc._crawl_single(URL, 30, wc._ProgressLog()))
    [(_, applied, kw)] = given
    assert (applied.word_count_threshold, applied.cache_mode, applied.page_timeout,
            applied.settings) == (1, 'bypass', 60000, {})
    assert kw == {'word_count_threshold': 50, 'timeout': 30000, 'bypass_cache': True}, kw


@pytest.mark.parametrize('waited_s, timed_out', [(44.99, False), (45.0, True)])
def test_the_timeout_check_uses_crawl4ai_s_page_timeout(monkeypatch, waited_s, timed_out):
    """With crawl4ai's PAGE_TIMEOUT at 45 s (the page timeout arun applies),
    a catch-all after 44.99 s is a crash (read without a browser) and one
    after 45 s a timeout (kept): the check follows crawl4ai's constant."""
    given = _real_browser(monkeypatch, arun=_goto_timed_out, takes=waited_s)
    _crawl4ai_096(monkeypatch, page_timeout=45000)
    fetched = _plain(monkeypatch)
    result = wc.crawl_url(URL)
    assert given[0][1].page_timeout == 45000, given
    assert result['success'] is (not timed_out), result
    assert fetched == ([] if timed_out else [URL])


def test_the_browser_start_is_not_the_page_s_wait(monkeypatch):
    """Only arun's own time is the page's: the first crawl starts the browser
    first (seconds), and crawl_urls waits for its turn before _crawl_single.
    A browser that took the whole page timeout to start and then crashed at
    once is still read without a browser."""
    _real_browser(monkeypatch, arun=_crashed, takes=0.01, start_takes=60.0)
    fetched = _plain(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is True and fetched == [URL], result
