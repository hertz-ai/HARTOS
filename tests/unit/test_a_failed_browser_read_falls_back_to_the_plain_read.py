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
