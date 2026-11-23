"""Keyless search asks ddgs's engines one at a time, each with a short wait,
adds their rows until the request is filled, and asks first the engine that
last filled a request on its own on this node.

WHAT WENT WRONG (desktop build 75, 15 Nov 2026, the build's own ddgs 9.16)
  The Research Assistant's search step got nothing back on every call; its
  verifier then held the step for the person.  Measured from the desktop:
  DDGS().text(q, max_results=5) raised 'No results found', while asking each
  engine alone gave: duckduckgo, google, brave, bing, yahoo, mojeek and
  wikipedia nothing, yandex 3 real rows.  ddgs's auto mode runs two engines at
  a time, stops waiting at the first exception, and never collects the futures
  still running in its last batch, so the one engine that answers is lost.
  Tiers B and C (plain-request Mojeek and DuckDuckGo pages) were blocked too
  ('captcha', 'anomaly').

AND ON CENTRAL (container langchain, build a59caf223, ddgs 9.16, 19 Nov 2026)
  Asked alone, with ddgs's 5 s timeout, for 'rice exporters India',
  'textile manufacturers Tirupur' and 'speech therapy clinics Chennai': brave,
  google, grokipedia, mojeek, startpage and wikipedia found nothing in 0.5 to
  1.2 s; duckduckgo hung for the whole 5.0 s; yahoo answered 5 rows in 1.5 to
  1.8 s, 9 times in 12.  Auto took 9.5 to 11.3 s and timed out on one of the
  three.  So the daemon's sourcing searches paid auto's 10 s, then every
  engine in ddgs's order, before reaching the one that answers.

AND THE FIRST FIX'S OWN DEFECT (bfe435df5, review rv_bfe4, live)
  The first engine with any rows won and was remembered.  grokipedia and
  wikipedia give at most one row, and grokipedia comes before yahoo in ddgs's
  order: 'Python programming language' got 1 grokipedia row in 3 of 3 runs
  where auto got 5, 5 and 2, and grokipedia was then asked first on every
  later search.  Tier A could also run to 19 s on a 15 s budget, and the
  older duckduckgo_search lost its own 10 s timeout.

AND ON CENTRAL AGAIN (container langchain, ddgs 9.16, primp 2.0.0, 22 Nov 2026)
  Yahoo was still the only engine with web rows there, but for 'basmati
  rice' it served the container a page with no web results (a recipe module
  and a knowledge panel, no result list) on 9 of 16 asks and its full page on
  the other 7, whatever the edge (Ireland or Singapore), the browser OS
  claimed, kept cookies or the path; the next ask about 6 s after an empty one
  had the web rows 3 times in 7.  The desktop on the same public IP got the
  full page 6 times in 6.  The daemon's burst of five searches on 21 Nov
  (00:56:41-00:57:06) got rows for the first two and nothing for the rest.
  Both pages are recorded in tests/fixtures/yahoo_serp_*.html.

THE RULE
  Tier A asks ddgs's own text engines one at a time (names read from ddgs, no
  list of our own) and adds their rows, one copy per link, until the request
  is filled or the tier's time budget is spent; no engine waits past what is
  left of that budget.  Only an engine that filled the request on its own is
  remembered (the one with the most rows), and it is asked first next time
  (process memory, nothing hard-coded: a desktop where duckduckgo answers keeps
  duckduckgo).  When that remembered engine comes back empty and the others
  have not filled the request, it is asked once more after them, inside the
  same budget.  The later tiers still run when tier A finds nothing.  Only a
  ddgs that names no engines (the older duckduckgo_search package) is asked
  with its own default, timeout included.

    python -m pytest tests/unit/test_keyless_search_tries_each_engine.py -q --noconftest
"""
import os
import sys
import types

import pytest

import hartos.helper as helper

# What one engine costs, from the central measurement above.
_ANSWER_S = 1.6     # yahoo answering
_EMPTY_S = 1.0      # an engine that finds nothing


class _Clock:
    """The time the fake engines spend; the helper reads it as time.monotonic."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def _rows_of(backend, how):
    """The rows a fake engine gives: ``how`` rows of its own links, or the
    links in a list ``how``."""
    links = how if isinstance(how, list) else [
        f'https://{backend}.example/{i}' for i in range(how)]
    return [{'title': f'{backend} {i}', 'href': link, 'body': f'about {link}'}
            for i, link in enumerate(links)]


def _fake_ddgs(monkeypatch, clock, behaviour,
               engines=('wikipedia', 'duckduckgo', 'yandex', 'brave')):
    """A stand-in for the ddgs package.  ``behaviour`` maps a backend name to
    'rows' (a full answer, 5 rows), a number of rows, a list of links, 'hang'
    (spends the whole timeout the DDGS was built with, then gives up, as
    duckduckgo did on central) or 'empty' (finds nothing); a backend not named
    is 'empty'.  No backend argument is the package default, recorded as
    'auto'.  ``engines=None`` = a package that names no engines.  Returns
    (backends asked, timeouts each DDGS was built with; 'own' = none given)."""
    asked, built = [], []

    class DDGS:
        def __init__(self, *args, **kwargs):
            built.append(kwargs.get('timeout', 'own'))
            self.timeout = kwargs.get('timeout', 5)           # ddgs 9.16 default

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results=5, backend='auto'):
            asked.append(backend)
            how = behaviour.get(backend, 'empty')
            if callable(how):
                return how(self.timeout, max_results)
            if how == 'hang':
                clock.now += self.timeout
                raise RuntimeError('timed out')
            if how == 'empty':
                clock.now += _EMPTY_S
                raise RuntimeError('No results found.')
            clock.now += _ANSWER_S
            return _rows_of(backend, 5 if how == 'rows' else how)

    mod = types.ModuleType('ddgs')
    mod.DDGS = DDGS
    monkeypatch.setitem(sys.modules, 'ddgs', mod)
    if engines is None:
        monkeypatch.setitem(sys.modules, 'ddgs.engines', None)
    else:
        eng = types.ModuleType('ddgs.engines')
        eng.ENGINES = {'text': {name: object for name in engines}}
        mod.engines = eng
        monkeypatch.setitem(sys.modules, 'ddgs.engines', eng)
    return asked, built


_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'fixtures')
_WEB = 'yahoo_serp_web_results.html'          # central, 22 Nov 2026, 10 web rows
_NO_WEB = 'yahoo_serp_no_web_results.html'    # central, same day, no result list
_CENTRAL_ENGINES = ('brave', 'duckduckgo', 'google', 'grokipedia', 'mojeek',
                    'startpage', 'wikipedia', 'yahoo')     # ddgs 9.16 there


def _yahoo_rows(page):
    """The rows ddgs 9.16's Yahoo engine reads from a recorded page, with its
    own xpaths, ad-click skip and /RU= unwrap (ddgs/engines/yahoo.py); ddgs is
    not installed in the test venv.  ddgs 9.16 itself reads 10 and 0 rows from
    the two fixtures (checked on the desktop's python-embed, 22 Nov 2026)."""
    from urllib.parse import unquote_plus
    from lxml import html
    with open(os.path.join(_FIXTURES, page), encoding='utf-8') as f:
        tree = html.fromstring(f.read())
    rows = []
    for item in tree.xpath("//div[contains(@class, 'relsrch')]"):
        def text(xpath):
            return ' '.join(''.join(item.xpath(xpath)).split())
        href = text(".//div[contains(@class, 'Title')]//a/@href")
        if href.startswith('https://www.bing.com/aclick?'):
            continue
        if '/RU=' in href:
            href = unquote_plus(href.split('/RU=', 1)[1].split('/RK=', 1)[0]
                                .split('/RS=', 1)[0])
        rows.append({'title': text(".//div[contains(@class, 'Title')]//h3//text()"),
                     'href': href,
                     'body': text(".//div[contains(@class, 'Text')]//text()")})
    return rows


def _yahoo_serves(clock, *pages):
    """A Yahoo that serves the recorded pages in turn, one per ask, as
    central's did.  ddgs raises 'No results found.' for a page it reads no
    rows from, and returns at most max_results rows."""
    queue = list(pages)

    def ask(timeout, max_results):
        rows = _yahoo_rows(queue.pop(0))
        clock.now += _ANSWER_S if rows else _EMPTY_S
        if not rows:
            raise RuntimeError('No results found.')
        return rows[:max_results]
    return ask


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(helper, 'time', types.SimpleNamespace(monotonic=c.monotonic))
    return c


@pytest.fixture
def no_other_tiers(monkeypatch):
    """A fresh node (no engine remembered), and tiers B and C recorded."""
    monkeypatch.setattr(helper, '_DDGS_ANSWERED_LAST', {}, raising=False)
    called = []
    monkeypatch.setattr(helper, '_mojeek_serp', lambda q, n=5: called.append('B') or [])
    monkeypatch.setattr(helper, '_ddg_html_serp', lambda q, n=5: called.append('C') or [])
    monkeypatch.delenv('SEARXNG_URL', raising=False)
    return called


def test_two_hanging_engines_then_the_answer_comes_within_budget_and_is_asked_first_next(
        monkeypatch, clock, no_other_tiers):
    asked, _ = _fake_ddgs(monkeypatch, clock, {'A': 'hang', 'B': 'hang', 'C': 'rows'},
                          engines=('A', 'B', 'C'))
    rows = helper._keyless_serp('rice exporters India', max_results=5)
    assert [r['url'] for r in rows] == [f'https://C.example/{i}' for i in range(5)], (rows, asked)
    assert rows[0]['title'] == 'C 0'
    assert rows[0]['snippet'] == 'about https://C.example/0'
    assert asked == ['A', 'B', 'C'], asked
    # Two hanging engines at ddgs's own 5 s would spend the 10 s alone.
    assert clock.now <= 10.0, f'first search took {clock.now} s'
    assert no_other_tiers == [], 'tiers B/C ran although an engine answered'

    asked.clear()
    started = clock.now
    rows = helper._keyless_serp('textile manufacturers Tirupur', max_results=5)
    assert len(rows) == 5, asked
    assert asked == ['C'], asked
    assert clock.now - started == pytest.approx(_ANSWER_S)


def test_a_one_row_engine_before_a_full_one_still_gives_five_rows_and_the_full_one_is_kept(
        monkeypatch, clock, no_other_tiers):
    asked, _ = _fake_ddgs(monkeypatch, clock, {'grokipedia': 1, 'yahoo': 'rows'},
                          engines=('brave', 'grokipedia', 'yahoo', 'wikipedia'))
    rows = helper._keyless_serp('Python programming language', max_results=5)
    urls = [r['url'] for r in rows]
    assert len(urls) == 5 and len(set(urls)) == 5, urls
    assert urls[0] == 'https://grokipedia.example/0', urls
    assert asked == ['brave', 'grokipedia', 'yahoo'], asked

    asked.clear()
    assert len(helper._keyless_serp('speech therapy clinics Chennai', max_results=5)) == 5
    assert asked == ['yahoo'], asked


def test_rows_of_two_partial_engines_add_up_one_per_link_and_neither_is_kept(
        monkeypatch, clock, no_other_tiers):
    a_links = [f'https://a.example/{i}' for i in range(3)]
    asked, _ = _fake_ddgs(
        monkeypatch, clock,
        {'A': a_links, 'B': [a_links[0], 'https://b.example/1', 'https://b.example/2']},
        engines=('A', 'B', 'C'))
    rows = helper._keyless_serp('q', max_results=5)
    assert [r['url'] for r in rows] == a_links + ['https://b.example/1',
                                                   'https://b.example/2'], rows
    assert asked == ['A', 'B'], asked
    asked.clear()
    helper._keyless_serp('q2', max_results=5)
    assert asked[0] == 'A', asked


def test_when_the_remembered_engine_finds_nothing_the_others_are_asked_and_the_new_one_kept(
        monkeypatch, clock, no_other_tiers):
    behaviour = {'C': 'rows'}
    asked, _ = _fake_ddgs(monkeypatch, clock, behaviour, engines=('A', 'B', 'C'))
    assert helper._keyless_serp('q1', max_results=5)
    behaviour.clear()
    behaviour['B'] = 'rows'
    asked.clear()
    assert helper._keyless_serp('q2', max_results=5)
    assert asked == ['C', 'A', 'B'], asked
    asked.clear()
    assert helper._keyless_serp('q3', max_results=5)
    assert asked == ['B'], asked


def test_tier_a_never_runs_past_its_budget(monkeypatch, clock, no_other_tiers):
    names = ('brave', 'duckduckgo', 'google', 'grokipedia', 'mojeek',
             'startpage', 'wikipedia', 'yahoo')
    asked, _ = _fake_ddgs(monkeypatch, clock, {n: 'hang' for n in names + ('auto',)},
                          engines=names)
    assert helper._keyless_serp('q', max_results=5) == []
    assert len(asked) < len(names), asked
    assert clock.now <= 15.0, f'tier A took {clock.now} s on a 15 s budget'
    assert no_other_tiers == ['B', 'C']


def test_only_ddgs_own_engines_are_asked(monkeypatch, clock, no_other_tiers):
    asked, _ = _fake_ddgs(monkeypatch, clock, {}, engines=('mojeek', 'yandex'))
    helper._keyless_serp('q', max_results=5)
    assert asked == ['mojeek', 'yandex'], asked


def test_a_desktop_where_duckduckgo_answers_asks_nothing_else(monkeypatch, clock, no_other_tiers):
    asked, _ = _fake_ddgs(monkeypatch, clock, {'duckduckgo': 'rows'},
                          engines=('duckduckgo', 'yandex', 'brave'))
    assert len(helper._keyless_serp('q', max_results=5)) == 5
    assert asked == ['duckduckgo'], asked


def test_a_package_that_names_no_engines_is_asked_with_its_own_default_and_timeout(
        monkeypatch, clock, no_other_tiers):
    asked, built = _fake_ddgs(monkeypatch, clock, {'auto': 'rows'}, engines=None)
    assert helper._keyless_serp('q', max_results=5)
    assert asked == ['auto'], asked
    assert built == ['own'], built
    assert no_other_tiers == []


def test_when_no_engine_answers_the_later_tiers_still_run(monkeypatch, clock, no_other_tiers):
    _fake_ddgs(monkeypatch, clock, {})
    assert helper._keyless_serp('q', max_results=5) == []
    assert no_other_tiers == ['B', 'C']


def test_the_recorded_yahoo_pages_read_as_central_got_them():
    rows = _yahoo_rows(_WEB)
    assert len(rows) == 10, rows
    assert rows[0]['href'] == 'https://en.wikipedia.org/wiki/Basmati', rows[0]
    assert _yahoo_rows(_NO_WEB) == []


def test_the_remembered_engine_that_comes_back_empty_is_asked_once_more_after_the_others(
        monkeypatch, clock, no_other_tiers):
    monkeypatch.setitem(helper._DDGS_ANSWERED_LAST, 'engine', 'yahoo')
    asked, _ = _fake_ddgs(monkeypatch, clock,
                          {'yahoo': _yahoo_serves(clock, _NO_WEB, _WEB)},
                          engines=_CENTRAL_ENGINES)
    rows = helper._keyless_serp('basmati rice', max_results=5)
    assert [r['url'] for r in rows] == [r['href'] for r in _yahoo_rows(_WEB)[:5]], (rows, asked)
    assert asked == ['yahoo', 'brave', 'duckduckgo', 'google', 'grokipedia',
                     'mojeek', 'startpage', 'wikipedia', 'yahoo'], asked
    assert no_other_tiers == [], 'tiers B/C ran although the second ask answered'
    assert helper._DDGS_ANSWERED_LAST['engine'] == 'yahoo'
    assert clock.now <= 15.0, f'tier A took {clock.now} s on a 15 s budget'


def test_the_remembered_engine_is_asked_again_only_once(monkeypatch, clock, no_other_tiers):
    monkeypatch.setitem(helper._DDGS_ANSWERED_LAST, 'engine', 'yahoo')
    asked, _ = _fake_ddgs(monkeypatch, clock,
                          {'yahoo': _yahoo_serves(clock, _NO_WEB, _NO_WEB, _WEB)},
                          engines=_CENTRAL_ENGINES)
    assert helper._keyless_serp('basmati rice', max_results=5) == []
    assert asked.count('yahoo') == 2 and asked[-1] == 'yahoo', asked
    assert no_other_tiers == ['B', 'C']


def test_rows_found_before_the_second_ask_are_kept_and_topped_up_by_it(
        monkeypatch, clock, no_other_tiers):
    monkeypatch.setitem(helper._DDGS_ANSWERED_LAST, 'engine', 'yahoo')
    asked, _ = _fake_ddgs(monkeypatch, clock,
                          {'wikipedia': 1, 'yahoo': _yahoo_serves(clock, _NO_WEB, _WEB)},
                          engines=_CENTRAL_ENGINES)
    rows = helper._keyless_serp('basmati rice', max_results=5)
    assert [r['url'] for r in rows] == (['https://wikipedia.example/0']
                                        + [r['href'] for r in _yahoo_rows(_WEB)[:4]]), rows
    assert asked[-1] == 'yahoo', asked


def test_the_second_ask_waits_only_for_what_is_left_of_the_budget(
        monkeypatch, clock, no_other_tiers):
    monkeypatch.setitem(helper._DDGS_ANSWERED_LAST, 'engine', 'yahoo')
    asked, built = _fake_ddgs(
        monkeypatch, clock,
        {'yahoo': _yahoo_serves(clock, _NO_WEB, _NO_WEB), 'A': 'hang', 'B': 'hang', 'C': 'hang'},
        engines=('yahoo', 'A', 'B', 'C'))
    assert helper._keyless_serp('q', max_results=5) == []
    # yahoo 1 s, then three engines hang 4 s each: 13 s spent, 2 s left.
    assert asked == ['yahoo', 'A', 'B', 'C', 'yahoo'], asked
    assert built[-1] == 2, built
    assert clock.now <= 15.0, f'tier A took {clock.now} s on a 15 s budget'


def test_no_second_ask_once_the_budget_is_spent(monkeypatch, clock, no_other_tiers):
    monkeypatch.setitem(helper._DDGS_ANSWERED_LAST, 'engine', 'yahoo')
    asked, _ = _fake_ddgs(
        monkeypatch, clock,
        {'yahoo': _yahoo_serves(clock, _NO_WEB, _WEB),
         'A': 'hang', 'B': 'hang', 'C': 'hang', 'D': 'hang'},
        engines=('yahoo', 'A', 'B', 'C', 'D'))
    assert helper._keyless_serp('q', max_results=5) == []
    assert asked == ['yahoo', 'A', 'B', 'C', 'D'], asked
    assert clock.now <= 15.0, f'tier A took {clock.now} s on a 15 s budget'
    assert no_other_tiers == ['B', 'C']
