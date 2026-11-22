"""A web search the engines refused says which engine refused and why.

WHAT WENT WRONG (owner's desktop, installed build, 22 Nov 2026, 14:29 IST)
  The research agent was asked how speech therapy apps help children with
  speech delay.  It took 187.9 s and 29 model calls and answered from its own
  guess, with no sources, naming apps that do not exist.  The log:
    * keyless search: mojeek answered HTTP 200 with a captcha page, DuckDuckGo
      HTTP 202 with an "anomaly" page, and none of ddgs's engines gave a row.
      The tool told the agent only "The web search found no results ...
      Search again with different words", so it searched five times with
      other words and crawled three links it made up.
    * the fifth search was 'best speech therapy apps for children with
      speech delay'.  Its one row was Wikipedia's page "Best".  The search
      had been sent whole (every engine's request line carries all its
      words): nothing was wrong with the query or its encoding.  One engine
      answered a page about its first word (the link is Wikipedia's mobile
      address under a " - Wikipedia" title: a web engine's row, most likely
      from Yahoo's cut-down page, asked first and again last), every other
      engine had refused or found nothing, and the code handed that lone
      row on as the search's result.  The agent crawled it.

THE RULE
  * What each engine answered is kept for the search's caller
    (helper.keyless_search_report): a bot-check page or a status that is not
    200 is a REFUSED search, not an empty one.
  * A search's ONE row that does not hold half of the search's words is not
    its result (helper._row_answers); two rows or more are never judged.
  * With no result and no Google key, the agent is told which it was:
    nothing found (core.constants.EMPTY_SEARCH_RESULT, "search again with
    different words"), or refused (REFUSED_SEARCH_RESULT: other words get the
    same refusal, do not search again, do not answer from memory, tell the
    person), each with one line naming what every engine answered.  Both are
    still an empty lookup to the gate that reads one (tool_result_is_empty).

Nothing here reaches the network: ddgs and the two page fetches are fakes.

    python -m pytest tests/unit/test_a_refused_search_says_which_engine_refused.py -q --noconftest
"""
import logging
import sys
import types

import pytest

import hartos.helper as helper
from core import constants
from core.constants import EMPTY_SEARCH_RESULT

QUESTION = 'How do speech therapy apps help children with speech delay?'
BEST = 'best speech therapy apps for children with speech delay'
CAPTCHA = '<html><body><h1>Verify you are human</h1><div class="captcha"></div></body></html>'
ANOMALY = '<html><body><div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div></body></html>'
NOTHING = '<html><body><p>No pages were found for these words.</p></body></html>'
BEST_ROW = {'title': 'Best - Wikipedia', 'href': 'https://en.wikipedia.org/wiki/Best',
            'body': 'Best & Co., an 1879-1971 clothing chain Best Lock Corporation, a lock '
                    'manufacturer Best Manufacturing Company, a farm machinery company'}


@pytest.fixture
def web(monkeypatch):
    """The keyless tiers with nothing real behind them.  ``web.ddgs`` is what
    each ddgs engine answers ({engine: rows}); ``web.pages`` what mojeek and
    DuckDuckGo's plain pages answer ({host word: (status, html)})."""
    state = types.SimpleNamespace(
        ddgs={}, asked=[], fetched=[],
        pages={'mojeek': (200, CAPTCHA), 'duckduckgo': (202, ANOMALY)})

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
        status, html = next(page for word, page in state.pages.items() if word in url)
        return types.SimpleNamespace(text=html, status_code=status)
    monkeypatch.setattr(helper, 'pooled_get', pooled_get)
    monkeypatch.setattr(helper, 'current_app',
                        types.SimpleNamespace(logger=logging.getLogger('t')))
    monkeypatch.setattr(helper, 'search', None)
    monkeypatch.setattr(helper, '_DDGS_ANSWERED_LAST', {}, raising=False)
    monkeypatch.setattr(helper, 'crawl4ai_batch_fetch', lambda urls, max_concurrent=2: [
        '' for _ in urls])
    monkeypatch.setattr(helper, '_remember_links', lambda query, links: None)
    monkeypatch.delenv('SEARXNG_URL', raising=False)
    return state


def _said(out):
    """The one line naming what each engine answered."""
    head, sep, said = out.partition('\n' + constants.SEARCH_ENGINES_SAID)
    assert sep, out
    return head, said


# -- the walk's search: every engine refused ---------------------------------

def test_a_captcha_and_a_bot_check_page_are_a_refused_search_and_named(web):
    out = helper.top5_results(QUESTION)
    assert isinstance(out, str), out
    head, said = _said(out)
    assert head == constants.REFUSED_SEARCH_RESULT
    assert "mojeek: a bot-check page instead of results (it says 'captcha', HTTP 200)" in said
    assert ("duckduckgo: a bot-check page instead of results (it says 'anomaly', HTTP 202)"
            in said)
    assert 'ddgs: no results from any of its engines (brave, wikipedia, yahoo)' in said


def test_a_refused_search_tells_the_agent_not_to_search_again_or_guess(web):
    out = helper.top5_results(QUESTION)
    assert 'could not be done' in out and 'refused' in out
    assert 'Do not make up a link or cite a source' in out
    assert 'do not answer from memory' in out
    assert 'do not search again in this turn' in out
    assert 'Tell the person plainly' in out
    assert 'Search again with different words' not in out, \
        'other words get the same refusal: the agent searched five times'


def test_a_status_that_is_not_200_is_a_refusal_too(web):
    web.pages = {'mojeek': (429, '<html>Too Many Requests</html>'),
                 'duckduckgo': (503, '<html>Service Unavailable</html>')}
    head, said = _said(helper.top5_results(QUESTION))
    assert head == constants.REFUSED_SEARCH_RESULT
    assert 'mojeek: HTTP 429 and no results' in said
    assert 'duckduckgo: HTTP 503 and no results' in said


# -- an engine that searched and found nothing is not one that refused -------

def test_engines_that_found_nothing_say_so_and_the_agent_may_search_again(web):
    web.pages = {'mojeek': (200, NOTHING), 'duckduckgo': (200, NOTHING)}
    head, said = _said(helper.top5_results(QUESTION))
    assert head == EMPTY_SEARCH_RESULT and 'Search again with different words' in head
    assert said == ('ddgs: no results from any of its engines (brave, wikipedia, yahoo); '
                    'mojeek: no results; duckduckgo: no results.')


def test_one_engine_refusing_makes_the_search_refused(web):
    web.pages = {'mojeek': (200, NOTHING), 'duckduckgo': (202, ANOMALY)}
    head, said = _said(helper.top5_results(QUESTION))
    assert head == constants.REFUSED_SEARCH_RESULT
    assert 'mojeek: no results' in said and "duckduckgo: a bot-check page" in said


# -- the page "Best": one row that does not match the search -----------------

def test_a_lone_page_that_does_not_match_the_search_is_no_result(web):
    web.ddgs = {'wikipedia': [BEST_ROW]}
    out = helper.top5_results(BEST)
    assert isinstance(out, str), 'the page "Best" was handed to the agent as the result'
    head, said = _said(out)
    assert head == constants.REFUSED_SEARCH_RESULT
    assert ("ddgs: only one page, titled 'Best - Wikipedia', which does not match the "
            "search") in said
    assert 'wiki/Best' not in out, 'its link is not offered for a crawl'
    assert len(web.fetched) == 2, 'the later tiers were still asked'


def test_a_lone_page_that_matches_the_search_is_kept(web):
    """Carried over: one row that answers the search is a result."""
    web.ddgs = {'wikipedia': [{
        'title': 'Basmati - Wikipedia', 'href': 'https://en.wikipedia.org/wiki/Basmati',
        'body': 'Basmati is a variety of long, slender-grained aromatic rice which is '
                'traditionally grown in the Indian subcontinent, mainly in India, Pakistan '
                'and Nepal.'}]}
    out = helper.top5_results('basmati rice')
    assert isinstance(out, list) and out[0]['source'] == ['https://en.wikipedia.org/wiki/Basmati']
    assert web.fetched == [], 'the search was answered: no later tier is asked'


def test_two_rows_are_never_judged(web):
    """Carried over: the rule is for a search's one row only."""
    web.ddgs = {'yahoo': [
        {'title': 'Alpha', 'href': 'https://a.example/1', 'body': 'x' * 100},
        {'title': 'Beta', 'href': 'https://b.example/2', 'body': 'y' * 100}]}
    out = helper.top5_results(BEST)
    assert isinstance(out, list)
    assert out[0]['source'] == ['https://a.example/1', 'https://b.example/2']


@pytest.mark.parametrize('query, row, answers', [
    (BEST, {'title': 'Best - Wikipedia', 'snippet': BEST_ROW['body'],
            'url': BEST_ROW['href']}, False),
    ('basmati rice', {'title': 'Basmati', 'snippet': 'a long-grained aromatic rice',
                      'url': 'https://en.wikipedia.org/wiki/Basmati'}, True),
    ('Narendra Modi', {'title': 'Narendra Modi', 'snippet': '', 'url': ''}, True),
    ('what is the capital of France',
     {'title': 'France', 'snippet': 'France is a country whose capital is Paris; the '
                                    'history of what became France',
      'url': 'https://en.wikipedia.org/wiki/France'}, True),
    ('speech therapy apps for toddlers', {'title': 'Speech therapy apps', 'snippet': '',
                                          'url': 'https://example.org/a'}, True),
    ('speech therapy apps for toddlers at home', {'title': 'Speech therapy', 'snippet': '',
                                                  'url': 'https://example.org/b'}, False),
    ('speech therapy apps', {'title': 'Top speech therapy apps of 2026', 'snippet': '',
                             'url': 'https://example.org/apps'}, True),
    ('பேச்சு பயிற்சி',
     {'title': 'பேச்சு பயிற்சி',
      'snippet': '', 'url': ''}, True),
    ('', {'title': 'Anything', 'snippet': '', 'url': ''}, True),
    ('??', {'title': 'Anything', 'snippet': '', 'url': ''}, True),
], ids=['best', 'basmati', 'a-name', 'a-question', 'three-of-five', 'two-of-seven', 'a-phrase',
        'tamil', 'no-words', 'marks'])
def test_a_row_answers_a_search_when_it_holds_half_of_its_words(query, row, answers):
    assert helper._row_answers(query, row) is answers


# -- each search reports itself ----------------------------------------------

def test_a_search_that_found_results_returns_them_and_reports_nothing(web):
    web.ddgs = {'yahoo': [{'title': f'Speech therapy apps {i}', 'href': f'https://e.example/{i}',
                           'body': 'How apps help children with a speech delay practise ' * 2}
                          for i in range(5)]}
    out = helper.top5_results(QUESTION)
    assert isinstance(out, list) and len(out[0]['source']) == 5
    assert helper.keyless_search_report() == ([], False)


def test_an_earlier_searchs_refusal_is_not_said_for_a_later_search(web, monkeypatch):
    assert helper.top5_results(QUESTION).startswith(constants.REFUSED_SEARCH_RESULT)
    monkeypatch.setattr(helper, '_keyless_top5', lambda q: [])
    assert helper.top5_results('another question') is EMPTY_SEARCH_RESULT


# -- both answers are an empty lookup to the gate that reads one -------------

def test_both_sentences_with_their_line_are_an_empty_result():
    refused = constants.empty_search_result(['mojeek: a bot-check page'], True)
    nothing = constants.empty_search_result(['mojeek: no results'], False)
    assert refused == (constants.REFUSED_SEARCH_RESULT + '\n' + constants.SEARCH_ENGINES_SAID
                       + 'mojeek: a bot-check page.')
    assert nothing.startswith(EMPTY_SEARCH_RESULT + '\n')
    for body in (refused, nothing, '  ' + refused + '\n', EMPTY_SEARCH_RESULT,
                 constants.REFUSED_SEARCH_RESULT):
        assert constants.is_empty_search_result(body) and constants.tool_result_is_empty(body)


def test_a_result_with_anything_more_is_not_empty():
    refused = constants.empty_search_result(['mojeek: a bot-check page'], True)
    for body in (refused + '\nThen: rice exporters, India.',
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
