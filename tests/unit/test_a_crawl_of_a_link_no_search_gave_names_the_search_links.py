"""A failed crawl of a link no search gave says so and names the search's links.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 21:05 IST,
a test account): the research agent searched "How do speech therapy apps help
children with speech delay?" and the search gave
speechblubs.com/blog/9-effective-apps-for-speech-delay-to-help-your-child-speak.
The model then crawled speechblubs.com/apps-for-delayed-speech, a path it had
edited, got a 404 and tried the same link about ten times.  The refusal told it
to "crawl a link exactly as the search results gave it", but not which links
those were, and the tool cannot reach the turn's window (hartos-43's review of
the design, 2026-11-18 15:21Z: a request-keyed store with a time limit, never
shared across requests).

THE RULE
  * helper.top5_results keeps the links its keyless search gave, under the
    turn's request id (integrations.web_crawler.remember_search_links);
  * when crawl_url_for_agent / crawl_urls_for_agent fail on a link that is not
    one of them, the reply says "This link was not in the search results" and
    lists them;
  * a failed link that the search did give gets no such note;
  * another request's links are never shown, and kept links expire;
  * the store is bounded.

Nothing here reaches the network: the read and the search are replaced.

    python -m pytest tests/unit/test_a_crawl_of_a_link_no_search_gave_names_the_search_links.py -q --noconftest
"""
import logging
import types

import hartos.helper as helper
from hartos.threadlocal import thread_local_data
from integrations import web_crawler as wc

GIVEN = 'https://speechblubs.com/blog/9-effective-apps-for-speech-delay-to-help-your-child-speak'
EDITED = 'https://speechblubs.com/apps-for-delayed-speech'
OTHER = 'https://speechtherapymagic.com/best/speech-therapy-apps-for-kids'


def _fresh(monkeypatch, request_id='req-1'):
    monkeypatch.setattr(wc, '_SEARCH_LINKS', {})
    thread_local_data.set_request_id(request_id)
    monkeypatch.setattr(wc, 'crawl_url', lambda url, timeout=30: {
        'success': False, 'url': url, 'error': '404 Client Error: Not Found'})


def test_a_failed_crawl_of_an_edited_link_names_the_links_the_search_gave(monkeypatch):
    _fresh(monkeypatch)
    wc.remember_search_links('req-1', [GIVEN, OTHER])
    out = wc.crawl_url_for_agent(EDITED)
    assert 'This link was not in the search results' in out, out
    assert GIVEN in out and OTHER in out, out


def test_a_failed_crawl_of_a_link_the_search_gave_has_no_such_note(monkeypatch):
    _fresh(monkeypatch)
    wc.remember_search_links('req-1', [GIVEN])
    out = wc.crawl_url_for_agent(GIVEN)
    assert 'not in the search results' not in out, out
    assert 'What to do' in out


def test_another_requests_links_are_never_shown(monkeypatch):
    _fresh(monkeypatch, request_id='req-2')
    wc.remember_search_links('req-1', [GIVEN])
    out = wc.crawl_url_for_agent(EDITED)
    assert GIVEN not in out and 'not in the search results' not in out, out


def test_kept_links_expire(monkeypatch):
    _fresh(monkeypatch)
    wc.remember_search_links('req-1', [GIVEN])
    now = wc.time.monotonic()
    monkeypatch.setattr(wc.time, 'monotonic', lambda: now + wc._SEARCH_LINKS_TTL_S + 1)
    assert 'not in the search results' not in wc.crawl_url_for_agent(EDITED)


def test_the_store_is_bounded(monkeypatch):
    _fresh(monkeypatch)
    for i in range(wc._SEARCH_LINKS_MAX + 5):
        wc.remember_search_links(f'r{i}', [GIVEN])
    assert len(wc._SEARCH_LINKS) == wc._SEARCH_LINKS_MAX
    assert 'r0' not in wc._SEARCH_LINKS and f'r{wc._SEARCH_LINKS_MAX + 4}' in wc._SEARCH_LINKS


def test_several_pages_get_the_note_per_failed_page(monkeypatch):
    _fresh(monkeypatch)
    wc.remember_search_links('req-1', [GIVEN])
    monkeypatch.setattr(wc, 'crawl_urls', lambda urls, timeout=30: [
        {'success': False, 'url': u, 'error': '404'} for u in urls])
    out = wc.crawl_urls_for_agent([GIVEN, EDITED])
    assert out.count('This link was not in the search results') == 1, out


def test_the_search_keeps_the_links_it_gave_for_this_request(monkeypatch):
    _fresh(monkeypatch, request_id='req-9')
    monkeypatch.setattr(helper, 'current_app',
                        types.SimpleNamespace(logger=logging.getLogger('t')))
    monkeypatch.setattr(helper, '_keyless_top5', lambda q: [
        {'text': 'Apps (x)', 'source': [GIVEN, OTHER], 'method': 'serp_snippets'}])
    helper.top5_results('speech therapy apps for children')
    assert wc._SEARCH_LINKS['req-9'][1] == [GIVEN, OTHER]
