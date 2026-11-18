"""An empty web search says so, in words, instead of handing back [].

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 20:56 IST,
a test account): the research agent was asked "How do speech therapy apps
help children with speech delay?".  Its first google_search came back as the
two characters "[]": the keyless engines answered nothing that moment, and a
desktop has no Google key, so helper.top5_results fell to its
`if search is None: return []` exit.  With no links in its window, the
model's next step crawled two healthline.com links that no search had given
(both 404) and then asked the person what they wanted.  A minute later the
same search returned five real links.

The google_search tool agents call is core/agent_tools.google_search ->
helper.top5_results.  hart_intelligence_entry has its own top5_results whose
empty exits already answer in words (test_google_search_empty_is_diagnosable);
the helper's copy, the one that runs, did not.

THE RULE
  When the keyless search is empty and no Google key is set, top5_results
  returns a sentence that says nothing was found and what to do: not make up
  a link, search again with other words, or tell the person.  A keyless
  search that found results returns them unchanged.

    python -m pytest tests/unit/test_an_empty_keyless_search_says_so.py -q --noconftest
"""
import logging
import types

import hartos.helper as helper

ROWS = [{'text': 'Speech Therapy Apps — help children practise (https://example.org/a)',
         'source': ['https://example.org/a'], 'method': 'serp_snippets'}]


def _rig(monkeypatch, keyless):
    monkeypatch.setattr(helper, 'current_app',
                        types.SimpleNamespace(logger=logging.getLogger('t')))
    monkeypatch.setattr(helper, '_keyless_top5', lambda q: keyless)
    monkeypatch.setattr(helper, 'search', None)


def test_an_empty_search_on_a_machine_with_no_key_says_nothing_was_found(monkeypatch):
    _rig(monkeypatch, [])
    out = helper.top5_results('speech therapy apps for children')
    assert isinstance(out, str) and out, out
    assert 'no results' in out
    assert 'Do not make up a link' in out
    assert 'Search again' in out


def test_the_answer_is_not_the_empty_list_the_model_cannot_read(monkeypatch):
    _rig(monkeypatch, [])
    out = helper.top5_results('speech therapy apps for children')
    assert out != [] and str(out) != '[]'


def test_a_search_that_found_results_returns_them_unchanged(monkeypatch):
    _rig(monkeypatch, ROWS)
    assert helper.top5_results('speech therapy apps for children') is ROWS
