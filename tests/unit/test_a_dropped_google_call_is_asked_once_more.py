"""A Google search call that failed for a passing reason is asked once more;
a refused key is asked once and answered in words.

WHAT WENT WRONG (review of a59caf223, 20 Nov 2026, F1 and F2):
  F1  Before a59caf223, when hartos.helper.top5_results's first Google call
      failed (a dropped connection, HTTP 503) and the next one worked, the
      tool returned that call's rows: results(query, 2) raised, and the
      crash path ended at `return search.results(query, 4)`.  a59caf223
      answered every raise with a refusal after one call, so a passing
      failure lost rows its parent returned (measured with ConnectionError
      and HTTP 503: calls [2, 4] and rows before, [2] and a refusal after).
  F2  hart_intelligence_entry.top5_results, the casual agent's google_search,
      asks Google only (no keyless search).  It answered a 401 with "Search
      again with different words" and "Call google_search again only with
      that changed", and every such call gets the same 401.

THE RULE
  * A ConnectionError, a timeout, an HTTP 5xx, or a 429 that does not say
    the quota is spent: Google is asked once more, results(query, 4) as the
    parent asked, and that call's rows are returned.  Two calls at most, as
    before a59caf223.
  * A refused key (HTTP 400, 401, 403) or a 429 that says the quota is spent:
    one call and the plain-words refusal, so a refused key is never asked
    again (a59caf223's point).
  * The key rides in the error's url; it reaches neither the result nor a
    log line on any path.
  * The casual agent's copy answers a refused key with "do not call
    google_search again" and names what the owner sets: GOOGLE_API_KEY and
    GOOGLE_CSE_ID, or SEARXNG_URL.

    python -m pytest tests/unit/test_a_dropped_google_call_is_asked_once_more.py -q --noconftest
"""
import ast
import json
import logging
import os
import re
import types

import pytest

import hartos.helper as helper
from core.constants import tool_reply_failed
from core.tool_refusal import refusal_has_hint

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET = 'AIzaFAKEKEY_dropped_call_456'
ROWS = [{'title': 'Rice exporters of India', 'link': 'https://example.org/rice',
         'snippet': 'A list of rice exporters'},
        {'title': 'APEDA rice', 'link': 'https://example.org/apeda',
         'snippet': 'Basmati and non-basmati exporters'}]
_QUOTA_SPENT = ("Quota exceeded for quota metric 'Queries' and limit 'Queries "
                "per day' of service 'customsearch.googleapis.com'")


def _http_error(status, message='x'):
    """A real googleapiclient HttpError; its text carries the request url,
    and the url carries the key."""
    from googleapiclient.errors import HttpError
    resp = types.SimpleNamespace(status=status, reason='x')
    body = json.dumps({'error': {'code': status, 'message': message,
                                 'status': 'X'}}).encode()
    uri = ('https://customsearch.googleapis.com/customsearch/v1?q=rice'
           f'&cx=cse123&key={SECRET}&alt=json')
    return HttpError(resp, body, uri=uri)


def _dropped():
    # requests' text for a dropped connection names the url, key included.
    return ConnectionError('Max retries exceeded with url: /customsearch/v1'
                           f'?q=rice&key={SECRET}')


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
def rig(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger('test_a_dropped_google_call_is_asked_once_more')
    monkeypatch.setattr(helper, 'current_app', types.SimpleNamespace(logger=log))
    monkeypatch.setattr(helper.time, 'sleep', lambda s: None)
    monkeypatch.setattr(helper, '_keyless_top5', lambda q: [])

    def _set(search):
        monkeypatch.setattr(helper, 'search', search)
        return search
    return _set


_PASSING = {
    'ConnectionError': _dropped,
    'timeout': lambda: TimeoutError('The read operation timed out'),
    'HTTP 500': lambda: _http_error(500, 'Backend Error'),
    'HTTP 503': lambda: _http_error(503, 'The service is currently unavailable.'),
    'HTTP 429, no quota named': lambda: _http_error(429, 'Too Many Requests'),
}


@pytest.mark.parametrize('first', sorted(_PASSING))
def test_a_passing_failure_then_a_working_call_returns_its_rows(rig, first):
    search = rig(_Search(_PASSING[first](), ROWS))
    out = helper.top5_results('rice exporters India')
    assert out == ROWS, out
    assert search.calls == [2, 4], "asked once more, as before a59caf223"


def test_two_passing_failures_end_in_words_after_two_calls(rig, caplog):
    search = rig(_Search(_dropped(), _http_error(503)))
    out = helper.top5_results('rice exporters India')
    assert isinstance(out, str), out
    assert tool_reply_failed(out) and refusal_has_hint(out), out
    assert '503' in out, "names the last call's failure"
    assert search.calls == [2, 4], "never a third call"
    assert SECRET not in out and SECRET not in caplog.text


_KEY_REFUSED = {
    'HTTP 400 invalid key': lambda: _http_error(
        400, 'API key not valid. Please pass a valid API key.'),
    'HTTP 401': lambda: _http_error(401, 'Request had invalid authentication credentials.'),
    'HTTP 403': lambda: _http_error(403, 'The caller does not have permission'),
}


@pytest.mark.parametrize('refused', sorted(_KEY_REFUSED))
def test_a_refused_key_is_asked_once(rig, refused):
    search = rig(_Search(_KEY_REFUSED[refused](), ROWS))
    out = helper.top5_results('rice exporters India')
    assert isinstance(out, str), out
    assert tool_reply_failed(out) and refusal_has_hint(out), out
    assert 'GOOGLE_API_KEY' in out and 'GOOGLE_CSE_ID' in out, out
    assert search.calls == [2], "a refused key is never asked again"


def test_a_429_that_names_the_spent_quota_is_asked_once(rig):
    search = rig(_Search(_http_error(429, _QUOTA_SPENT), ROWS))
    out = helper.top5_results('rice exporters India')
    assert isinstance(out, str) and '429' in out and 'quota' in out, out
    assert refusal_has_hint(out)
    assert search.calls == [2]


@pytest.mark.parametrize('answers', [
    ('refused key', lambda: (_http_error(401),)),
    ('passing failure, then rows', lambda: (_dropped(), ROWS)),
    ('two passing failures', lambda: (_dropped(), _http_error(503))),
], ids=lambda a: a[0])
def test_the_key_reaches_neither_the_result_nor_the_log(rig, caplog, answers):
    rig(_Search(*answers[1]()))
    out = helper.top5_results('rice exporters India')
    assert SECRET not in str(out)
    assert SECRET not in caplog.text, [r.getMessage() for r in caplog.records]


# --- hart_intelligence_entry.top5_results, the casual agent's copy ---------

def _entry_top5_results(search, log):
    """hart_intelligence_entry.top5_results itself, lifted with ast (the
    module is too heavy to import here) and run against the Google boundary;
    the same technique as test_an_expert_turn_never_dials_a_refusing_model."""
    src = open(os.path.join(_ROOT, 'hart_intelligence_entry.py'),
               encoding='utf-8').read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == 'top5_results')

    async def _no_crawl(links):
        return ['page one text', 'page two text']
    import asyncio
    ns = {'search': search, 'app': types.SimpleNamespace(logger=log),
          'asyncio': asyncio, 'async_main': _no_crawl, 're': re}
    exec(ast.get_source_segment(src, fn), ns)
    return ns['top5_results']


@pytest.mark.parametrize('refused', sorted(_KEY_REFUSED))
def test_the_casual_copy_says_not_to_search_google_again_on_a_refused_key(
        caplog, refused):
    caplog.set_level(logging.DEBUG)
    search = _Search(_KEY_REFUSED[refused]())
    top5 = _entry_top5_results(search, logging.getLogger('entry_copy'))
    out = top5('rice exporters India')
    assert isinstance(out, str), out
    assert tool_reply_failed(out) and refusal_has_hint(out), out
    assert 'do not call google_search again' in out, out
    assert 'Search again with different words' not in out, out
    assert 'only with that changed' not in out, out
    assert 'GOOGLE_API_KEY' in out and 'GOOGLE_CSE_ID' in out, out
    assert 'SEARXNG_URL' in out, out
    assert search.calls == [2]
    assert SECRET not in out and SECRET not in caplog.text


def test_the_casual_copy_still_invites_a_search_after_a_passing_failure():
    search = _Search(_dropped())
    out = _entry_top5_results(search, logging.getLogger('entry_copy'))(
        'rice exporters India')
    assert isinstance(out, str) and refusal_has_hint(out), out
    assert 'do not call google_search again' not in out, out
    assert SECRET not in out
