"""A keyless search answers with results, not the engine's ad clicks.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 17:36 IST)
  The Research Assistant searched "how speech therapy apps help children with
  speech delay".  Of the five rows ddgs gave back, the first two were Bing ad
  clicks -- https://www.bing.com/aclick?ld=...&u=... , about 600 characters
  each -- ahead of the three real results (speechlp.com, speechblubs.com,
  forbrain.com).  Its fetch step then asked for a page that is in no result,
  https://speechtherapyapps.org/top-10-best-apps, and got a 500.

THE RULE
  A row whose link is a Bing ad click is not a result: tier A drops it.  An
  engine whose rows are all ad clicks has answered nothing, so the next engine
  is asked, exactly as for an engine that found nothing.  Only the Bing form was
  measured; no other engine's ad form is guessed here.

    python -m pytest tests/unit/test_a_search_drops_ad_clicks.py -q --noconftest
"""
import sys
import types

import hartos.helper as helper

_AD = {'title': 'Apps For Speech Therapy For Toddlers',
       'href': 'https://www.bing.com/aclick?ld=e8_H10KiIK9kMKwPscW6T1yDVUCUy&u=aHR0cHM',
       'body': '5M+ moms love this learning to talk app. Get 70% off all plans.'}
_REAL = [
    {'title': 'Speech Therapy App for Kids', 'href': 'https://speechlp.com/',
     'body': 'AI speech games for children.'},
    {'title': 'The Best Apps for Kids with Speech Delay',
     'href': 'https://speechblubs.com/blog/empowering-voices-the-best-apps-for-kids-with-speech-delay',
     'body': 'How apps help children practise speech at home.'},
]


def _fake_ddgs(monkeypatch, auto_rows, engine_rows=None, engines=('bing', 'yandex')):
    """A stand-in ddgs: 'auto' returns ``auto_rows``; each named engine
    returns ``engine_rows[name]``.  Records every backend asked."""
    asked = []
    engine_rows = engine_rows or {}

    class DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, max_results=5, backend=None):
            asked.append(backend or 'auto')
            return list(auto_rows if backend is None else engine_rows.get(backend, []))

    pkg = types.ModuleType('ddgs')
    pkg.DDGS = DDGS
    eng = types.ModuleType('ddgs.engines')
    eng.ENGINES = {'text': {name: object() for name in engines}}
    monkeypatch.setitem(sys.modules, 'ddgs', pkg)
    monkeypatch.setitem(sys.modules, 'ddgs.engines', eng)
    return asked


def test_the_rows_hold_no_ad_click(monkeypatch):
    _fake_ddgs(monkeypatch, [_AD, dict(_AD, href=_AD['href'] + '2')] + _REAL)
    urls = [r['url'] for r in helper._keyless_serp('speech apps', max_results=5)]
    assert urls == [r['href'] for r in _REAL]


def test_the_answer_the_model_reads_cites_only_results(monkeypatch):
    _fake_ddgs(monkeypatch, [_AD] + _REAL)
    got = helper._keyless_top5('how speech therapy apps help children with speech delay')
    assert got and 'aclick' not in got[0]['text']
    assert got[0]['source'] == [r['href'] for r in _REAL]


def test_an_engine_that_answers_only_with_ads_is_passed_over(monkeypatch):
    asked = _fake_ddgs(monkeypatch, [_AD],
                       engine_rows={'bing': [_AD], 'yandex': _REAL})
    urls = [r['url'] for r in helper._keyless_serp('speech apps', max_results=5)]
    assert urls == [r['href'] for r in _REAL]
    assert asked == ['auto', 'bing', 'yandex']


def test_a_result_that_merely_mentions_bing_is_kept(monkeypatch):
    kept = {'title': 'Bing tips', 'href': 'https://www.bing.com/search?q=aclick+meaning',
            'body': 'What an aclick link is.'}
    _fake_ddgs(monkeypatch, [kept])
    assert [r['url'] for r in helper._keyless_serp('aclick', max_results=5)] == [kept['href']]
