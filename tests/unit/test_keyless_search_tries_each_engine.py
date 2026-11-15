"""Keyless search returns the rows of an engine that answers, even when the
ddgs 'auto' mode finds nothing.

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

THE RULE
  When auto gives no rows, tier A asks ddgs's own text engines one at a time,
  names read from ddgs (no list of our own, so no engine is contacted that auto
  would not contact), and returns the first engine's rows.  When auto answers,
  nothing else is asked.

    python -m pytest tests/unit/test_keyless_search_tries_each_engine.py -q --noconftest
"""
import sys
import types

import pytest

import hartos.helper as helper

_ROWS = [{'title': 'Speech apps for toddlers', 'href': 'https://example.org/a',
          'body': 'A list of speech therapy apps.'}]


def _fake_ddgs(monkeypatch, answering, engines=('wikipedia', 'duckduckgo', 'yandex', 'brave')):
    """A stand-in for the ddgs package: 'auto' finds nothing unless 'auto' is
    in ``answering``; each named engine answers only when it is in
    ``answering``.  Records every backend asked."""
    asked = []

    class DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results=5, backend='auto'):
            asked.append(backend)
            if backend in answering:
                return list(_ROWS)
            raise RuntimeError('No results found.')

    mod = types.ModuleType('ddgs')
    mod.DDGS = DDGS
    eng = types.ModuleType('ddgs.engines')
    eng.ENGINES = {'text': {name: object for name in engines}}
    mod.engines = eng
    monkeypatch.setitem(sys.modules, 'ddgs', mod)
    monkeypatch.setitem(sys.modules, 'ddgs.engines', eng)
    return asked


@pytest.fixture
def no_other_tiers(monkeypatch):
    """Tiers B and C must not be needed when an engine answers."""
    called = []
    monkeypatch.setattr(helper, '_mojeek_serp', lambda q, n=5: called.append('B') or [])
    monkeypatch.setattr(helper, '_ddg_html_serp', lambda q, n=5: called.append('C') or [])
    monkeypatch.delenv('SEARXNG_URL', raising=False)
    return called


def test_an_engine_that_answers_is_used_when_auto_finds_nothing(monkeypatch, no_other_tiers):
    asked = _fake_ddgs(monkeypatch, answering={'yandex'})
    rows = helper._keyless_serp('speech therapy apps for children', max_results=5)
    assert [r['url'] for r in rows] == ['https://example.org/a'], (rows, asked)
    assert rows[0]['title'] == 'Speech apps for toddlers'
    assert rows[0]['snippet'] == 'A list of speech therapy apps.'
    assert asked[0] == 'auto' and 'yandex' in asked, asked
    assert no_other_tiers == [], 'tiers B/C ran although an engine answered'


def test_when_auto_answers_nothing_else_is_asked(monkeypatch, no_other_tiers):
    asked = _fake_ddgs(monkeypatch, answering={'auto'})
    rows = helper._keyless_serp('speech therapy apps for children', max_results=5)
    assert rows and asked == ['auto'], asked
    assert no_other_tiers == []


def test_only_ddgs_own_engines_are_asked(monkeypatch, no_other_tiers):
    asked = _fake_ddgs(monkeypatch, answering=set(), engines=('mojeek', 'yandex'))
    helper._keyless_serp('q', max_results=5)
    assert set(asked) <= {'auto', 'mojeek', 'yandex'}, asked
    assert sorted(set(asked) - {'auto'}) == ['mojeek', 'yandex'], asked


def test_the_engines_are_asked_until_one_answers_then_stop(monkeypatch, no_other_tiers):
    asked = _fake_ddgs(monkeypatch, answering={'duckduckgo'},
                       engines=('duckduckgo', 'yandex', 'brave'))
    rows = helper._keyless_serp('q', max_results=5)
    assert rows
    assert asked == ['auto', 'duckduckgo'], asked


def test_when_no_engine_answers_the_later_tiers_still_run(monkeypatch, no_other_tiers):
    _fake_ddgs(monkeypatch, answering=set())
    assert helper._keyless_serp('q', max_results=5) == []
    assert no_other_tiers == ['B', 'C']
