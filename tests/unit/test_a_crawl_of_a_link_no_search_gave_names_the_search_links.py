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

rv_4301's review of 4301d1b26 (hartos-43, 19 Nov 2026) added:
  * M1: every search of a request ADDS its links (a second search used to
    replace the first's, so a link from the first search was called "not in
    the search results"); the query itself is never kept as a link (a model
    that searches with a URL it made up gets it echoed back);
  * S4: links are kept per person AND request;
  * S5: the Google path's links count as searched too;
  * a store failure is a warning, not a debug line.
Both M1 cases run through the real helper.top5_results and the real
crawl_url_for_agent.

Nothing here reaches the network: the read and the search are replaced.

    python -m pytest tests/unit/test_a_crawl_of_a_link_no_search_gave_names_the_search_links.py -q --noconftest
"""
import types

import hartos.helper as helper
from hartos.threadlocal import thread_local_data
from integrations import web_crawler as wc

GIVEN = 'https://speechblubs.com/blog/9-effective-apps-for-speech-delay-to-help-your-child-speak'
EDITED = 'https://speechblubs.com/apps-for-delayed-speech'
OTHER = 'https://speechtherapymagic.com/best/speech-therapy-apps-for-kids'


USER = 'search-user-1'


def _fresh(monkeypatch, request_id='req-1', user_id=USER):
    monkeypatch.setattr(wc, '_SEARCH_LINKS', {})
    thread_local_data.set_request_id(request_id)
    thread_local_data.set_user_id(user_id)
    monkeypatch.setattr(wc, 'crawl_url', lambda url, timeout=30: {
        'success': False, 'url': url, 'error': '404 Client Error: Not Found'})


def _remember(request_id, links, **kw):
    kw.setdefault('user_id', USER)
    wc.remember_search_links(request_id, links, **kw)


def _kept(request_id, user_id=USER):
    entry = wc._SEARCH_LINKS.get(wc._search_key(request_id, user_id))
    return entry[1] if entry else None


def _rig_search(monkeypatch, keyless):
    """The real helper.top5_results with the engines replaced: each call
    hands back the next search's rows."""
    said = []

    class _Log:
        def warning(self, m, *a, **k):
            said.append(str(m))

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(helper, 'current_app', types.SimpleNamespace(logger=_Log()))
    rows = iter(keyless)
    monkeypatch.setattr(helper, '_keyless_top5', lambda q: next(rows))
    return said


def test_a_failed_crawl_of_an_edited_link_names_the_links_the_search_gave(monkeypatch):
    _fresh(monkeypatch)
    _remember('req-1', [GIVEN, OTHER])
    out = wc.crawl_url_for_agent(EDITED)
    assert 'This link was not in the search results' in out, out
    assert GIVEN in out and OTHER in out, out


def test_a_failed_crawl_of_a_link_the_search_gave_has_no_such_note(monkeypatch):
    _fresh(monkeypatch)
    _remember('req-1', [GIVEN])
    out = wc.crawl_url_for_agent(GIVEN)
    assert 'not in the search results' not in out, out
    assert 'What to do' in out


def test_the_same_link_in_another_case_or_with_a_slash_counts_as_given(monkeypatch):
    _fresh(monkeypatch)
    _remember('req-1', [GIVEN])
    out = wc.crawl_url_for_agent(GIVEN.upper() + '/')
    assert 'not in the search results' not in out, out


def test_another_requests_links_are_never_shown(monkeypatch):
    _fresh(monkeypatch, request_id='req-2')
    _remember('req-1', [GIVEN])
    out = wc.crawl_url_for_agent(EDITED)
    assert GIVEN not in out and 'not in the search results' not in out, out


def test_another_persons_links_under_the_same_request_id_are_never_shown(monkeypatch):
    """S4: request ids are per turn, not per person; the store keys both."""
    _fresh(monkeypatch, request_id='req-1', user_id='someone-else')
    _remember('req-1', [GIVEN], user_id=USER)
    out = wc.crawl_url_for_agent(EDITED)
    assert GIVEN not in out and 'not in the search results' not in out, out


def test_kept_links_expire(monkeypatch):
    _fresh(monkeypatch)
    _remember('req-1', [GIVEN])
    now = wc.time.monotonic()
    monkeypatch.setattr(wc.time, 'monotonic', lambda: now + wc._SEARCH_LINKS_TTL_S + 1)
    assert 'not in the search results' not in wc.crawl_url_for_agent(EDITED)


def test_an_expired_entry_is_not_merged_into_a_new_search(monkeypatch):
    _fresh(monkeypatch)
    _remember('req-1', [GIVEN])
    now = wc.time.monotonic()
    monkeypatch.setattr(wc.time, 'monotonic', lambda: now + wc._SEARCH_LINKS_TTL_S + 1)
    _remember('req-1', [OTHER])
    assert _kept('req-1') == [OTHER]


def test_the_store_is_bounded(monkeypatch):
    _fresh(monkeypatch)
    for i in range(wc._SEARCH_LINKS_MAX + 5):
        _remember(f'r{i}', [GIVEN])
    assert len(wc._SEARCH_LINKS) == wc._SEARCH_LINKS_MAX
    assert _kept('r0') is None and _kept(f'r{wc._SEARCH_LINKS_MAX + 4}') == [GIVEN]


def test_a_request_that_searches_again_becomes_the_newest(monkeypatch):
    """A long research turn searching again is not the first one evicted."""
    _fresh(monkeypatch)
    _remember('r0', [GIVEN])
    for i in range(1, wc._SEARCH_LINKS_MAX):
        _remember(f'r{i}', [GIVEN])
    _remember('r0', [OTHER])
    _remember('one-more', [GIVEN])
    assert _kept('r0') == [GIVEN, OTHER] and _kept('r1') is None


def test_one_requests_links_are_bounded_keeping_the_newest(monkeypatch):
    _fresh(monkeypatch)
    links = [f'https://example.org/page-{i}' for i in range(wc._SEARCH_LINKS_PER_REQUEST + 3)]
    _remember('req-1', links[:10])
    _remember('req-1', links[10:])
    assert _kept('req-1') == links[-wc._SEARCH_LINKS_PER_REQUEST:]


def test_the_note_names_the_newest_searchs_links(monkeypatch):
    """After several searches the note lists the latest links, the ones the
    model was just reading."""
    _fresh(monkeypatch)
    _remember('req-1', [f'https://example.org/old-{i}' for i in range(5)])
    _remember('req-1', [GIVEN, OTHER])
    out = wc.crawl_url_for_agent(EDITED)
    assert GIVEN in out and OTHER in out, out


def test_unreadable_turn_ids_give_no_note_and_say_so(monkeypatch):
    _fresh(monkeypatch)
    _remember('req-1', [GIVEN])
    said = []
    monkeypatch.setattr(wc.logger, 'warning', lambda m, *a, **k: said.append(str(m)))

    def _broken():
        raise RuntimeError('thread state gone')

    monkeypatch.setattr(thread_local_data, 'get_user_id', _broken)
    out = wc.crawl_url_for_agent(EDITED)
    assert 'not in the search results' not in out, out
    assert [m for m in said if 'cannot be read' in m], said


def test_several_pages_get_the_note_per_failed_page(monkeypatch):
    _fresh(monkeypatch)
    _remember('req-1', [GIVEN])
    monkeypatch.setattr(wc, 'crawl_urls', lambda urls, timeout=30: [
        {'success': False, 'url': u, 'error': '404'} for u in urls])
    out = wc.crawl_urls_for_agent([GIVEN, EDITED])
    assert out.count('This link was not in the search results') == 1, out


def test_the_search_keeps_the_links_it_gave_for_this_request(monkeypatch):
    _fresh(monkeypatch, request_id='req-9')
    _rig_search(monkeypatch, [[
        {'text': 'Apps (x)', 'source': [GIVEN, OTHER], 'method': 'serp_snippets'}]])
    helper.top5_results('speech therapy apps for children')
    assert _kept('req-9') == [GIVEN, OTHER]


# ---- M1 through the real search and the real crawl ------------------------

def test_a_link_from_the_first_search_is_still_given_after_a_second_search(monkeypatch):
    """M1, first case: two searches in one request; the crawl of the first
    search's link must not be told it was not in the search results."""
    _fresh(monkeypatch)
    _rig_search(monkeypatch, [
        [{'text': 'Apps', 'source': [GIVEN], 'method': 'serp_snippets'}],
        [{'text': 'More', 'source': [OTHER], 'method': 'serp_snippets'}]])
    helper.top5_results('speech therapy apps for children')
    helper.top5_results('speech delay apps reviewed')
    out = wc.crawl_url_for_agent(GIVEN)
    assert 'not in the search results' not in out, out
    edited = wc.crawl_url_for_agent(EDITED)
    assert GIVEN in edited and OTHER in edited, edited


def test_a_searched_url_echoed_back_by_the_engine_is_not_a_given_link(monkeypatch):
    """M1, second case: the model searched with a URL it made up and the
    engine echoed that URL as a source; a failed crawl of it still says the
    search never gave it, and names what the search did give."""
    _fresh(monkeypatch)
    _rig_search(monkeypatch, [[
        {'text': 'echo', 'source': [EDITED, GIVEN], 'method': 'serp_snippets'}]])
    helper.top5_results(EDITED)
    out = wc.crawl_url_for_agent(EDITED)
    assert 'This link was not in the search results' in out and GIVEN in out, out


def test_a_link_the_model_handed_the_search_is_not_a_search_result(monkeypatch):
    """M1, the reviewer's probe: google_search(<a link plus words>) reads the
    link directly ('direct_url'); that read is not a search, so the links the
    earlier search gave stay and the failed crawl still names them."""
    _fresh(monkeypatch)
    _rig_search(monkeypatch, [
        [{'text': 'Apps', 'source': [GIVEN], 'method': 'serp_snippets'}],
        [{'text': '', 'source': [EDITED], 'method': 'direct_url', 'enhanced': False}]])
    helper.top5_results('speech therapy apps for children')
    helper.top5_results(EDITED + ' read this page')
    assert _kept('req-1') == [GIVEN]
    out = wc.crawl_url_for_agent(EDITED)
    assert 'This link was not in the search results' in out and GIVEN in out, out


# ---- S5 and the warning ---------------------------------------------------

def test_the_google_path_keeps_its_links_too(monkeypatch):
    _fresh(monkeypatch, request_id='req-g')
    _rig_search(monkeypatch, [[]])
    monkeypatch.setattr(helper, 'search', types.SimpleNamespace(
        results=lambda q, n: [{'link': GIVEN}, {'link': OTHER}]))
    monkeypatch.setattr(helper, 'check_crawl4ai_service', lambda: False)
    monkeypatch.setattr(helper, 'fallback_fetch', lambda url: 'words ' * 40)
    monkeypatch.setattr(helper.time, 'sleep', lambda s: None)
    helper.top5_results('speech therapy apps for children')
    assert _kept('req-g') == [GIVEN, OTHER]


def test_a_store_that_fails_says_so_as_a_warning(monkeypatch):
    _fresh(monkeypatch)
    said = _rig_search(monkeypatch, [[
        {'text': 'Apps', 'source': [GIVEN], 'method': 'serp_snippets'}]])

    def _broken(*a, **k):
        raise RuntimeError('store lock timed out')

    monkeypatch.setattr(wc, 'remember_search_links', _broken)
    helper.top5_results('speech therapy apps for children')
    assert [m for m in said if 'search links not kept' in m], said
