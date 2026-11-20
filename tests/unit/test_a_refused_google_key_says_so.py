"""A refused Google search key answers the model in words; it never crashes.

WHAT WENT WRONG (live on central, 19 Nov 2026 16:22Z and 17:01Z, daemon goal
turns daemon_716cbcea and daemon_561bcef9): every google_search of a daemon
goal failed.  The keyless engines answered nothing (measured from central's
container the same evening: mojeek HTTP 403, html.duckduckgo.com connect
timeout, six of ddgs's seven text engines 'No results found', only yahoo
answering, and that 7 times in 10), so hartos.helper.top5_results fell to
the Google CSE.  search.results() raised HttpError 401 before
top_2_search_res_link was bound; the except branch, labelled "Crawl4AI API
method failed", logged the error (whose text carries the request URL, and
with it the key), both fallbacks hit
"local variable 'top_2_search_res_link' referenced before assignment", and
the last `return search.results(query, 4)` raised the 401 again out of the
tool.

The google_search tool agents call is core/agent_tools.google_search ->
hartos.helper.top5_results.  hart_intelligence_entry.top5_results is the
casual LangChain agent's copy; it already catches the raise, and now answers
with the same refusal instead of the error's text.

THE RULE
  * A Google call that raises never escapes top5_results and never reaches
    an unbound name.
  * The model gets a refusal (core.tool_refusal.refuse) that says what was
    wrong (the key was refused, HTTP 401; the quota is used up, HTTP 429) and
    how to fix it (GOOGLE_API_KEY / GOOGLE_CSE_ID, or SEARXNG_URL for the
    keyless search), and never the error's text: it carries the key.
  * A keyless search that found results is returned and Google is not asked.

    python -m pytest tests/unit/test_a_refused_google_key_says_so.py -q --noconftest
"""
import logging
import types

import pytest

import hartos.helper as helper
from core.constants import tool_reply_failed
from core.tool_refusal import refusal_has_hint

SECRET = 'AIzaFAKEKEY_never_shown_123'
ROWS = [{'text': 'Rice exporters of India - list (https://example.org/rice)',
         'source': ['https://example.org/rice'], 'method': 'serp_snippets'}]


def _http_error(status, reason):
    from googleapiclient.errors import HttpError
    resp = types.SimpleNamespace(status=status, reason=reason)
    body = (b'{"error": {"code": %d, "message": "%s", "status": "X"}}'
            % (status, reason.encode()))
    uri = ('https://customsearch.googleapis.com/customsearch/v1?q=rice'
           f'&cx=cse123&key={SECRET}&alt=json')
    return HttpError(resp, body, uri=uri)


class _Search:
    """The GoogleSearchAPIWrapper boundary: each results() call pops the
    next answer; an exception is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def results(self, query, num):
        self.calls.append(num)
        if not self.answers:
            raise AssertionError(f'Google asked a call too many: {self.calls}')
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def rig(monkeypatch):
    records = []

    class _Keep(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    log = logging.getLogger('test_a_refused_google_key_says_so')
    log.addHandler(_Keep())
    log.setLevel(logging.DEBUG)
    monkeypatch.setattr(helper, 'current_app', types.SimpleNamespace(logger=log))
    monkeypatch.setattr(helper.time, 'sleep', lambda s: None)

    def _set(keyless, search):
        monkeypatch.setattr(helper, '_keyless_top5', lambda q: keyless)
        monkeypatch.setattr(helper, 'search', search)
        return records
    return _set


def test_a_refused_key_is_answered_in_words_not_raised(rig):
    search = _Search(_http_error(401, 'Unauthorized'),
                     _http_error(401, 'Unauthorized'))
    rig([], search)
    out = helper.top5_results('rice exporters India')   # raised before the fix
    assert isinstance(out, str), out
    assert tool_reply_failed(out) and refusal_has_hint(out), out
    assert '401' in out and 'refused' in out
    assert 'GOOGLE_API_KEY' in out and 'GOOGLE_CSE_ID' in out
    assert 'SEARXNG_URL' in out
    assert 'make up a link' in out
    assert search.calls == [2], "Google is asked once, not again after a refusal"


def test_the_key_never_reaches_the_model_or_the_log(rig):
    records = rig([], _Search(_http_error(401, 'Unauthorized')))
    out = helper.top5_results('rice exporters India')
    assert SECRET not in str(out)
    assert not [m for m in records if SECRET in m], records


def test_a_spent_quota_says_so(rig):
    rig([], _Search(_http_error(429, 'Too Many Requests')))
    out = helper.top5_results('rice exporters India')
    assert isinstance(out, str) and '429' in out and 'quota' in out, out
    assert refusal_has_hint(out)


def test_a_network_failure_names_its_kind(rig):
    rig([], _Search(ConnectionError('connect timed out')))
    out = helper.top5_results('rice exporters India')
    assert isinstance(out, str) and 'ConnectionError' in out, out
    assert refusal_has_hint(out)


def test_a_keyless_result_is_used_and_google_is_not_asked(rig, monkeypatch):
    monkeypatch.setattr(helper, '_remember_links', lambda q, l: None, raising=False)
    search = _Search(_http_error(401, 'Unauthorized'))
    rig(ROWS, search)
    assert helper.top5_results('rice exporters India') is ROWS
    assert search.calls == []


def test_a_google_answer_with_no_link_never_raises(rig):
    """langchain's wrapper answers an empty search with
    [{'Result': 'No good Google Search Result was found'}]: no 'link'."""
    sentinel = [{'Result': 'No good Google Search Result was found'}]
    search = _Search(sentinel, sentinel)
    records = rig([], search)
    out = helper.top5_results('rice exporters India')
    assert out == sentinel
    # The unbound name, in the words of Python 3.10 (central) and 3.12.
    assert not [m for m in records if 'top_2_search_res_link' in m], records


def test_a_failed_crawl_then_a_refused_second_search_keeps_the_first_results(
        rig, monkeypatch):
    """The search answered, every crawl came back empty, and the final
    search.results(query, 4) raised: the first results are returned."""
    first = [{'title': 'A', 'link': 'https://example.org/a', 'snippet': 'a'},
             {'title': 'B', 'link': 'https://example.org/b', 'snippet': 'b'}]
    monkeypatch.setattr(helper, 'crawl4ai_batch_fetch', lambda urls, max_concurrent=2: [''] * len(urls))
    monkeypatch.setattr(helper, 'fallback_fetch', lambda url: '')

    def _no_async(urls):
        raise RuntimeError('no loop')
    monkeypatch.setattr(helper, 'async_main', _no_async)
    monkeypatch.setattr(helper, '_remember_links', lambda q, l: None, raising=False)
    search = _Search(first, _http_error(401, 'Unauthorized'))
    rig([], search)
    out = helper.top5_results('rice exporters India')
    assert out == first
    assert search.calls == [2, 4]
