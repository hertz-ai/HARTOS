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

Nothing here reaches the network: crawl4ai and requests are replaced.

    python -m pytest tests/unit/test_a_failed_browser_read_falls_back_to_the_plain_read.py -q --noconftest
"""
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


def _real_browser(monkeypatch, arun=None, start_error=None):
    """crawl4ai present and the REAL _crawl_single; the browser either
    cannot start (``start_error``) or reads each page with ``arun``."""
    class _Crawler:
        async def arun(self, url, **kw):
            return arun(url)

    async def fake_get_crawler(log):
        if start_error is not None:
            raise start_error
        return _Crawler()
    monkeypatch.setattr(wc, '_check_available', lambda: True)
    monkeypatch.setattr(wc, '_get_crawler', fake_get_crawler)


def _timed_out(url):
    from types import SimpleNamespace
    return SimpleNamespace(success=False, markdown='',
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
    _real_browser(monkeypatch, arun=_timed_out)
    fetched = _serve_js_only(monkeypatch)
    result = wc.crawl_url(URL)
    assert result['success'] is False, result
    assert 'Timeout 30000ms exceeded' in result['error'], result
    assert fetched == [], 'the page was read a second time'
    out = wc.crawl_url_for_agent(URL)
    assert 'FAILED' in out and 'enable JavaScript' not in out, out


def test_a_batch_keeps_each_failure_the_browser_read(monkeypatch):
    _real_browser(monkeypatch, arun=_timed_out)
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
