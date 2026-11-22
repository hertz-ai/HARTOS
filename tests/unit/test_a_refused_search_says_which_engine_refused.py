"""A web search says what each engine answered; only a search no engine
answered is refused, and every row an engine gave reaches the agent.

WHAT WENT WRONG (owner's desktop, installed build, 22 Nov 2026, 14:29 IST)
  The research agent was asked how speech therapy apps help children with
  speech delay.  It took 187.9 s and 29 model calls and answered from its own
  guess, with no sources, naming apps that do not exist.  The log:
    * keyless search: mojeek answered HTTP 200 with a captcha page, DuckDuckGo
      HTTP 202 with an "anomaly" page, and none of ddgs's engines gave a row.
      The tool told the agent only "The web search found no results ...
      Search again with different words", so it searched five times with
      other words and crawled three links it made up.
    * the fifth search's one row was Wikipedia's page "Best".

  5c1860f48 answered both, and its review (rv3w) measured two defects in it:
    * CRITICAL: a search's one row had to hold half of the search's words or
      it was dropped and the search called refused.  The word count counted
      stopwords and read a Chinese or Japanese search as one word, so real
      answers were dropped: the page "Speech delay" for the walk's own
      question (2 of 9 words), Wikipedia's "Ibuprofen" for a side-effects
      question, any weather page for '東京の天気' or '东京天气'.  The commit
      before handed each of them on.
    * MEDIUM: one engine refusing made the whole search refused.  Mojeek
      serves this computer a captcha page on every search, so every empty
      search was refused and "search again with other words" was never said.
      And the refused sentence repeated core.tool_refusal's no-retry words
      outside refuse().

THE RULE
  * Every row an engine gives reaches the agent, one row too; whether it
    answers the search is the agent's to judge (the rows are the facts).
  * What each engine answered is kept for the search's caller
    (helper.keyless_search_report), said as it was: a bot-check page, the
    HTTP status, a search that failed, no results, and only the engines
    really asked.
  * The search is REFUSED only when no engine gave a usable answer.  Then
    the agent gets a core.tool_refusal refusal for google_search
    (core.constants.search_refused_result): what each engine answered, tell
    the person, do not make up a link or answer from memory, the step may
    finish without the search.  When an engine searched and found nothing,
    it is EMPTY_SEARCH_RESULT ("search again with different words") with the
    same line under it.  Both still read as an empty lookup to the gate
    that reads one (tool_result_is_empty).
  * SEARXNG_URL is named, never its value, and offered to the owner only
    when it is not set.

Nothing here reaches the network: ddgs and every page fetch are fakes.  The
mojeek page carries what _mojeek_serp's own measurement found on it
('captcha' and 'javascript is required', HTTP 200).

    python -m pytest tests/unit/test_a_refused_search_says_which_engine_refused.py -q --noconftest
"""
import json
import logging
import sys
import threading
import types

import pytest

import hartos.helper as helper
from core import constants
from core.constants import EMPTY_SEARCH_RESULT
from core.tool_refusal import refusal_has_hint, refusal_lets_step_finish

QUESTION = 'How do speech therapy apps help children with speech delay?'
BEST = 'best speech therapy apps for children with speech delay'
MOJEEK_CAPTCHA = (
    '<!DOCTYPE html><html lang="en"><head><title>Mojeek</title></head><body>'
    '<noscript><p>JavaScript is required to use this site.</p></noscript>'
    '<div class="captcha-container"><h1>Please confirm you are a human</h1>'
    '<form method="post" action="/captcha"><button>Continue</button></form>'
    '</div></body></html>')
ANOMALY = '<html><body><div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div></body></html>'
NOTHING = '<html><body><p>No pages were found for these words.</p></body></html>'
BEST_ROW = {'title': 'Best - Wikipedia', 'href': 'https://en.wikipedia.org/wiki/Best',
            'body': 'Best & Co., an 1879-1971 clothing chain Best Lock Corporation, a lock '
                    'manufacturer Best Manufacturing Company, a farm machinery company'}
OPEN = 'Error: the google_search call could not be done: '
REFUSED_HEAD = (OPEN + 'no search engine gave a usable answer, and no Google search key is '
                'set on this machine.  What each search engine answered: ')
FINISH = 'Do not call google_search again now; this step may finish without its result.'


class _Page:
    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def web(monkeypatch):
    """The keyless tiers with nothing real behind them.  ``web.ddgs`` is what
    each ddgs engine answers ({engine: rows}, an exception it raises, or a
    function that gives either); ``web.pages`` what mojeek, DuckDuckGo's
    plain page and a SearXNG answer ({host word: (status, body)}, or an
    exception to raise)."""
    state = types.SimpleNamespace(
        ddgs={}, asked=[], fetched=[],
        pages={'mojeek': (200, MOJEEK_CAPTCHA), 'duckduckgo': (202, ANOMALY)})

    class DDGS:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results=5, backend='auto'):
            state.asked.append(backend)
            rows = state.ddgs.get(backend) or []
            if isinstance(rows, BaseException):
                raise rows
            if callable(rows):
                rows = rows()
            if not rows:
                raise RuntimeError('No results found.')
            return rows[:max_results]

    package = types.ModuleType('ddgs')
    package.DDGS = DDGS
    engines = types.ModuleType('ddgs.engines')
    engines.ENGINES = {'text': {name: object for name in ('brave', 'wikipedia', 'yahoo')}}
    monkeypatch.setitem(sys.modules, 'ddgs', package)
    monkeypatch.setitem(sys.modules, 'ddgs.engines', engines)

    def pooled_get(url, **kwargs):
        state.fetched.append(url)
        page = next(page for word, page in state.pages.items() if word in url)
        if isinstance(page, BaseException):
            raise page
        return _Page(*page)
    monkeypatch.setattr(helper, 'pooled_get', pooled_get)
    monkeypatch.setattr(helper, 'current_app',
                        types.SimpleNamespace(logger=logging.getLogger('t')))
    monkeypatch.setattr(helper, 'search', None)
    monkeypatch.setattr(helper, '_DDGS_ANSWERED_LAST', {}, raising=False)
    monkeypatch.setattr(helper, 'crawl4ai_batch_fetch', lambda urls, max_concurrent=2: [
        '' for _ in urls])
    monkeypatch.setattr(helper, '_remember_links', lambda query, links: None)
    monkeypatch.delenv('SEARXNG_URL', raising=False)

    def no_ddgs():
        """ddgs cannot be imported (a build that shipped it without its
        parts): only mojeek, DuckDuckGo's page and a SearXNG are asked."""
        monkeypatch.setitem(sys.modules, 'ddgs', None)
        monkeypatch.setitem(sys.modules, 'duckduckgo_search', None)
    state.no_ddgs = no_ddgs
    return state


def _said(out):
    """The one line under the empty answer naming what each engine answered."""
    head, sep, said = out.partition('\n' + constants.SEARCH_ENGINES_SAID)
    assert sep, out
    return head, said


def _refused(out):
    """The refused answer: one google_search refusal; its line of what each
    engine answered."""
    assert isinstance(out, str) and out.startswith(REFUSED_HEAD), out
    assert out.endswith(FINISH) and '\n' not in out, out
    return out[len(REFUSED_HEAD):]


# -- every row an engine gave reaches the agent (CRITICAL) -------------------

SPEECH_DELAY = {'title': 'Speech delay - Wikipedia',
                'href': 'https://en.wikipedia.org/wiki/Speech_delay',
                'body': 'Speech delay, also known as alalia, refers to a delay in the '
                        'development or use of the mechanisms that produce speech.'}
IBUPROFEN = {'title': 'Ibuprofen - Wikipedia', 'href': 'https://en.wikipedia.org/wiki/Ibuprofen',
             'body': 'Ibuprofen is a nonsteroidal anti-inflammatory drug (NSAID) that is used '
                     'to relieve pain, fever, and inflammation.'}
TOKYO_JA = {'title': '東京都の天気 - 日本気象協会 tenki.jp', 'href': 'https://tenki.jp/forecast/3/16/',
            'body': '東京都の今日明日の天気、週間天気予報、雨雲レーダー'}
TOKYO_ZH = {'title': '东京天气预报 - 中国天气网', 'href': 'https://www.weather.com.cn/weather/tokyo.shtml',
            'body': '东京今天天气，东京一周天气预报'}


@pytest.mark.parametrize('query, row', [
    (QUESTION, SPEECH_DELAY),
    ('What are the side effects of ibuprofen?', IBUPROFEN),
    ('東京の天気', TOKYO_JA),
    ('东京天气', TOKYO_ZH),
    (BEST, BEST_ROW),
], ids=['speech-delay', 'ibuprofen', 'tokyo-japanese', 'tokyo-chinese', 'the-page-best'])
def test_a_lone_row_an_engine_gave_reaches_the_agent(web, query, row):
    """The rows the commit before handed on: whether one answers the search
    is the agent's to judge, from its title, text and link."""
    web.ddgs = {'wikipedia': [row]}
    out = helper.top5_results(query)
    assert isinstance(out, list), out
    assert out[0]['source'] == [row['href']], out
    assert row['title'] in out[0]['text'] or row['href'] in out[0]['text'], out
    assert web.fetched == [], 'the search was answered: no later tier is asked'


# -- one engine refusing is not a refused search (MEDIUM 1) ------------------

def test_mojeeks_captcha_does_not_make_an_empty_search_refused(web):
    """Mojeek serves its captcha on every search; ddgs's engines and
    DuckDuckGo searched and found nothing, so the search found nothing."""
    web.pages['duckduckgo'] = (200, NOTHING)
    out = helper.top5_results(QUESTION)
    head, said = _said(out)
    assert head == EMPTY_SEARCH_RESULT and 'Search again with different words' in head
    assert said == ('ddgs: no results from any engine it asked (brave, wikipedia, yahoo); '
                    "mojeek: a bot-check page instead of results (it says 'captcha', HTTP 200); "
                    'duckduckgo: no results.')


def test_the_walks_search_is_said_as_each_engine_answered(web):
    """ddgs's engines searched and found nothing (ddgs says no more), mojeek
    and DuckDuckGo served bot-check pages: nothing was found, and the agent
    reads which engine refused and why."""
    head, said = _said(helper.top5_results(QUESTION))
    assert head == EMPTY_SEARCH_RESULT
    assert 'ddgs: no results from any engine it asked (brave, wikipedia, yahoo)' in said
    assert "mojeek: a bot-check page instead of results (it says 'captcha', HTTP 200)" in said
    assert ("duckduckgo: a bot-check page instead of results (it says 'anomaly', HTTP 202)"
            in said)


def test_engines_that_found_nothing_say_so_and_the_agent_may_search_again(web):
    web.pages = {'mojeek': (200, NOTHING), 'duckduckgo': (200, NOTHING)}
    head, said = _said(helper.top5_results(QUESTION))
    assert head == EMPTY_SEARCH_RESULT and 'Search again with different words' in head
    assert said == ('ddgs: no results from any engine it asked (brave, wikipedia, yahoo); '
                    'mojeek: no results; duckduckgo: no results.')


def test_only_the_engines_asked_within_the_budget_are_named(web, monkeypatch):
    """The tier's clock reads 0 s when it starts and when it asks brave, and
    100 s after: its 15 s are spent before wikipedia and yahoo."""
    real_time = helper.time

    class Clock:
        reads = 0

        def __getattr__(self, name):
            return getattr(real_time, name)

        def monotonic(self):
            Clock.reads += 1
            return 0.0 if Clock.reads <= 2 else 100.0
    monkeypatch.setattr(helper, 'time', Clock())
    web.pages['duckduckgo'] = (200, NOTHING)
    head, said = _said(helper.top5_results(QUESTION))
    assert web.asked == ['brave']
    assert said.startswith('ddgs: no results from any engine it asked (brave); '), said


# -- no engine gave a usable answer: one refusal (MEDIUM 2) ------------------

def test_with_no_engine_answering_the_search_is_one_google_search_refusal(web):
    web.no_ddgs()
    out = helper.top5_results(QUESTION)
    said = _refused(out)
    assert said.startswith(
        "mojeek: a bot-check page instead of results (it says 'captcha', HTTP 200); "
        "duckduckgo: a bot-check page instead of results (it says 'anomaly', HTTP 202). "), out
    assert refusal_has_hint(out)
    assert refusal_lets_step_finish(out, 'google_search'), 'the step may finish by telling'
    assert constants.tool_result_is_empty(out) and constants.is_empty_search_result(out), \
        'still an empty lookup to the gates that read one'


def test_the_refusal_says_what_to_do_and_not_to_search_again(web):
    web.no_ddgs()
    out = helper.top5_results(QUESTION)
    _refused(out)
    assert 'Tell the person plainly that the web search is not available right now and why' in out
    assert 'do not make up a link or cite a source' in out
    assert 'do not answer from memory as if it had been searched' in out
    assert 'It may work again later' in out
    assert 'set GOOGLE_API_KEY and GOOGLE_CSE_ID, or set SEARXNG_URL to a SearXNG server' in out
    assert 'Search again with different words' not in out


def test_a_status_is_said_as_it_was_never_as_a_bot_check(web):
    web.no_ddgs()
    web.pages = {'mojeek': (429, '<html>Too Many Requests</html>'),
                 'duckduckgo': (503, '<html>Service Unavailable</html>')}
    out = helper.top5_results(QUESTION)
    said = _refused(out)
    assert said.startswith('mojeek: HTTP 429 and no results; duckduckgo: HTTP 503 and no '
                           'results. '), out
    assert 'bot check' not in out and 'bot-check' not in out and 'rate limit' not in out, out


def test_a_tier_whose_search_failed_says_so(web):
    web.no_ddgs()
    web.pages['mojeek'] = ConnectionError('connection refused by www.mojeek.com')
    out = helper.top5_results(QUESTION)
    said = _refused(out)
    assert said.startswith('mojeek: the search failed (ConnectionError); duckduckgo: '), out
    assert 'www.mojeek.com' not in out


def test_a_set_searxng_is_named_never_by_its_address_and_not_offered_again(web, monkeypatch):
    web.no_ddgs()
    monkeypatch.setenv('SEARXNG_URL', 'https://owner:s3cret-pass@searx.home.example')
    web.pages['searx'] = (503, '<html>Service Unavailable</html>')
    out = helper.top5_results(QUESTION)
    said = _refused(out)
    assert 'searxng: the search failed (JSONDecodeError, HTTP 503)' in said, out
    assert 's3cret' not in out and 'searx.home.example' not in out and 'owner:' not in out
    assert 'SEARXNG_URL' not in out, 'it is set already'
    assert 'set GOOGLE_API_KEY and GOOGLE_CSE_ID.' in out


def test_a_searxng_that_searched_and_found_nothing_is_a_search_that_found_nothing(
        web, monkeypatch):
    web.no_ddgs()
    monkeypatch.setenv('SEARXNG_URL', 'https://searx.home.example')
    web.pages['searx'] = (200, json.dumps({'results': []}))
    head, said = _said(helper.top5_results(QUESTION))
    assert head == EMPTY_SEARCH_RESULT
    assert said.endswith('; searxng: no results.'), said


def test_the_refusal_carries_no_key(web, monkeypatch):
    """Carried over: no secret of this machine is said."""
    web.no_ddgs()
    monkeypatch.setenv('GOOGLE_API_KEY', 'AIzaSyD-not-a-real-key-0123456789abcdef')
    out = helper.top5_results(QUESTION)
    _refused(out)
    assert 'AIzaSy' not in out


# -- carried over -------------------------------------------------------------

def test_a_lone_page_that_matches_the_search_is_kept(web):
    web.ddgs = {'wikipedia': [{
        'title': 'Basmati - Wikipedia', 'href': 'https://en.wikipedia.org/wiki/Basmati',
        'body': 'Basmati is a variety of long, slender-grained aromatic rice which is '
                'traditionally grown in the Indian subcontinent, mainly in India, Pakistan '
                'and Nepal.'}]}
    out = helper.top5_results('basmati rice')
    assert isinstance(out, list) and out[0]['source'] == ['https://en.wikipedia.org/wiki/Basmati']
    assert web.fetched == [], 'the search was answered: no later tier is asked'


def test_two_rows_reach_the_agent(web):
    web.ddgs = {'yahoo': [
        {'title': 'Alpha', 'href': 'https://a.example/1', 'body': 'x' * 100},
        {'title': 'Beta', 'href': 'https://b.example/2', 'body': 'y' * 100}]}
    out = helper.top5_results(BEST)
    assert isinstance(out, list)
    assert out[0]['source'] == ['https://a.example/1', 'https://b.example/2']


def test_a_search_that_found_results_returns_them_and_reports_nothing(web):
    web.ddgs = {'yahoo': [{'title': f'Speech therapy apps {i}', 'href': f'https://e.example/{i}',
                           'body': 'How apps help children with a speech delay practise ' * 2}
                          for i in range(5)]}
    out = helper.top5_results(QUESTION)
    assert isinstance(out, list) and len(out[0]['source']) == 5
    assert helper.keyless_search_report() == ([], False)


def test_an_earlier_searchs_report_is_not_said_for_a_later_search(web, monkeypatch):
    first = helper.top5_results(QUESTION)
    assert isinstance(first, str) and 'mojeek' in first
    monkeypatch.setattr(helper, '_keyless_top5', lambda q: [])
    assert helper.top5_results('another question') is EMPTY_SEARCH_RESULT


def test_each_thread_reads_its_own_searchs_report(web):
    web.no_ddgs()
    helper._SERP_SAID.notes = []
    seen = {}

    def other():
        seen['out'] = helper.top5_results(QUESTION)
        seen['report'] = helper.keyless_search_report()
    t = threading.Thread(target=other)
    t.start()
    t.join(30)
    assert seen['report'][1] is True and 'mojeek' in seen['out']
    assert helper.keyless_search_report() == ([], False)


# -- both answers are an empty lookup to the gate that reads one -------------

def test_both_answers_with_their_line_are_an_empty_result(monkeypatch):
    monkeypatch.delenv('SEARXNG_URL', raising=False)
    refused = constants.empty_search_result(['mojeek: a bot-check page'], True)
    nothing = constants.empty_search_result(['mojeek: no results'], False)
    assert refused.startswith(REFUSED_HEAD + 'mojeek: a bot-check page. Tell the person')
    assert nothing == (EMPTY_SEARCH_RESULT + '\n' + constants.SEARCH_ENGINES_SAID
                       + 'mojeek: no results.')
    for body in (refused, nothing, '  ' + refused + '\n', EMPTY_SEARCH_RESULT):
        assert constants.is_empty_search_result(body) and constants.tool_result_is_empty(body)


def test_a_result_with_anything_more_is_not_empty(monkeypatch):
    from core.tool_refusal import RETRY_FINISH_WITHOUT, refuse
    monkeypatch.delenv('SEARXNG_URL', raising=False)
    refused = constants.empty_search_result(['mojeek: a bot-check page'], True)
    quoted = refuse('google_search', 'the page read said "no search engine gave a usable '
                    'answer, and no Google search key is set on this machine"',
                    'Tell the person what the page said', retry=RETRY_FINISH_WITHOUT)
    for body in (refused + '\nThen: rice exporters, India.',
                 refused.replace('. Tell the person', '.\nThen: rice exporters, India. Tell '
                                 'the person'),
                 'Then: ' + refused,
                 quoted,
                 refused.replace(FINISH, 'Call google_search again.'),
                 EMPTY_SEARCH_RESULT + ' Then: rice exporters, India.',
                 'The web search could not be done.', '[{"text": "x"}]'):
        assert not constants.is_empty_search_result(body), body
        assert not constants.tool_result_is_empty(body), body


def test_with_nothing_to_name_the_answer_is_the_one_sentence_itself():
    """Carried over: callers that compare by identity keep working."""
    assert constants.empty_search_result() is EMPTY_SEARCH_RESULT
    assert constants.empty_search_result([], True) is EMPTY_SEARCH_RESULT
    assert constants.empty_search_result(['', '  ']) is EMPTY_SEARCH_RESULT


def test_the_line_of_what_the_engines_said_is_one_bounded_line():
    out = constants.empty_search_result([f'engine{i}: no results\nat all' for i in range(80)])
    head, said = _said(out)
    assert '\n' not in said and len(said) <= 601 and constants.is_empty_search_result(out)
    refused = constants.empty_search_result([f'engine{i}: refused\nat all' for i in range(80)],
                                            True)
    assert '\n' not in refused and len(refused) < len(REFUSED_HEAD) + 601 + 500


# -- ddgs's own errors are each engine's reason (review of dab24d38c) --------
# _ddgs_rows turned every exception into an empty list, so on a desktop
# where ddgs loads, a rate-limited or timed-out engine read as "no results",
# the search could never be refused, and the agent was told to search again
# with other words: the live walk's five-search loop.  The classes below
# carry the names ddgs's own exceptions have (ddgs.exceptions, 9.16); the
# older duckduckgo_search raises a RatelimitException too.

class DDGSException(Exception):
    pass


class RatelimitException(DDGSException):
    pass


class TimeoutException(DDGSException):
    pass


ENGINES3 = ('brave', 'wikipedia', 'yahoo')


def test_every_ddgs_engine_rate_limited_makes_the_search_refused(web):
    web.ddgs = {e: RatelimitException('202 Ratelimit') for e in ENGINES3}
    out = helper.top5_results(QUESTION)
    said = _refused(out)
    assert said.startswith(
        'ddgs brave: rate limited (RatelimitException: 202 Ratelimit); '
        'ddgs wikipedia: rate limited (RatelimitException: 202 Ratelimit); '
        'ddgs yahoo: rate limited (RatelimitException: 202 Ratelimit); '
        "mojeek: a bot-check page instead of results (it says 'captcha', HTTP 200); "), out
    assert 'no results from' not in out


def test_every_ddgs_engine_timed_out_makes_the_search_refused(web):
    web.ddgs = {e: TimeoutException('Request timed out') for e in ENGINES3}
    said = _refused(helper.top5_results(QUESTION))
    for engine in ENGINES3:
        assert f'ddgs {engine}: timed out (TimeoutException: Request timed out)' in said, said


def test_an_engine_that_gave_no_answer_in_its_time_is_said_so(web, monkeypatch):
    """ddgs 9.16 stops waiting at its timeout, keeps no error and says "No
    results found.": an engine that took its whole time gave no answer."""
    real_time = helper.time

    class Clock:
        now = 0.0

        def __getattr__(self, name):
            return getattr(real_time, name)

        def monotonic(self):
            return Clock.now

    def slow():
        Clock.now += 4
        raise DDGSException('No results found.')
    monkeypatch.setattr(helper, 'time', Clock())
    web.ddgs = {e: slow for e in ENGINES3}
    said = _refused(helper.top5_results(QUESTION))
    assert said.startswith('ddgs brave: no answer within 4 s; ddgs wikipedia: no answer '
                           'within 4 s; ddgs yahoo: no answer within 4 s; '), said


def test_engines_that_failed_and_one_that_found_nothing_are_each_said(web):
    """One engine searched and found nothing, so the search found nothing,
    and the agent reads what each of the others answered."""
    web.ddgs = {'wikipedia': TimeoutException('Request timed out'),
                'yahoo': RatelimitException('202 Ratelimit')}
    head, said = _said(helper.top5_results(QUESTION))
    assert head == EMPTY_SEARCH_RESULT
    assert said.startswith('ddgs: no results from brave; '
                           'ddgs wikipedia: timed out (TimeoutException: Request timed out); '
                           'ddgs yahoo: rate limited (RatelimitException: 202 Ratelimit); '
                           'mojeek: '), said


def test_an_engine_asked_again_is_said_by_its_latest_answer(web, monkeypatch):
    """The remembered engine that failed is asked once more; its second,
    honest empty answer is what is said."""
    monkeypatch.setattr(helper, '_DDGS_ANSWERED_LAST', {'engine': 'yahoo'})
    answers = [TimeoutException('Request timed out'), DDGSException('No results found.')]

    def yahoo():
        raise answers.pop(0)
    web.ddgs = {'yahoo': yahoo}
    head, said = _said(helper.top5_results(QUESTION))
    assert web.asked == ['yahoo', 'brave', 'wikipedia', 'yahoo']
    assert head == EMPTY_SEARCH_RESULT
    assert said.startswith('ddgs: no results from any engine it asked (yahoo, brave, '
                           'wikipedia); '), said


def test_a_ddgs_installed_without_its_parts_is_said(web, monkeypatch):
    """ddgs imports without primp or lxml and fails when DDGS() loads them
    (the keyless-search memory: central's image shipped it so)."""
    package = types.ModuleType('ddgs')

    def DDGS(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'lxml'")
    package.DDGS = DDGS
    monkeypatch.setitem(sys.modules, 'ddgs', package)
    monkeypatch.setitem(sys.modules, 'ddgs.engines', types.ModuleType('ddgs.engines'))
    said = _refused(helper.top5_results(QUESTION))
    assert said.startswith("ddgs: the search failed (ModuleNotFoundError: No module named "
                           "'lxml'); mojeek: "), said


def test_an_honest_empty_ddgs_answer_stays_no_results(web):
    """Carried over: ddgs's own "No results found." is an engine that searched."""
    web.ddgs = {e: DDGSException('No results found.') for e in ENGINES3}
    web.pages['duckduckgo'] = (200, NOTHING)
    head, said = _said(helper.top5_results(QUESTION))
    assert head == EMPTY_SEARCH_RESULT and 'Search again with different words' in head
    assert said.startswith('ddgs: no results from any engine it asked (brave, wikipedia, '
                           'yahoo); '), said
