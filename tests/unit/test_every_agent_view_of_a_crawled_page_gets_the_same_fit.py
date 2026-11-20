"""Every agent view of a crawled page fits it to the agent's budget the same way.

WHAT WENT WRONG (rv_66web's review of 993f1671a, 18 Nov 2026)
  993f1671a took the 12-picture cap out of the read without a browser and
  moved the cost to the agent's text budget, in web_crawler._fit_for_agent:
  over the budget a row of pictures keeps its first 3 and a note, and the
  cut is at a sentence end, never inside a picture link.  Only the crawl
  tool used it (crawl_url_for_agent, crawl_urls_for_agent).  Three other
  views of the same page kept their own copies of the old cut, and with the
  cap gone they got worse than before:
    * the LangChain tool Data_Extraction_From_URL
      (hart_intelligence_entry.parse_link_for_crwalab, 8000): a gallery page
      gave 54 of 139 sentences and 40 links, against 89 from the crawl tool,
      and a cut still ended inside a link ('![p4](http://news.example.');
    * google_search's crawl after a search (helper._keyless_top5,
      body[:4000]): 8 sentences, against 35 before;
    * its read of a link the model gave (text[:4000]) and the Google-key
      crawl in top5_results (4000 with rfind('.')).

THE RULE
  Each of those views fits the page with _fit_for_agent, the crawl tool's
  own function, at its own budget:
    * the LangChain tool gives exactly what the crawl tool gives;
    * a gallery before an article leaves the article its budget, less the
      room of the 3 pictures shown and the note;
    * the text never ends inside a picture link;
    * a page that fits reaches the agent whole;
    * a row of a few pictures wholly inside the budget is kept whole, as a
      plain cut keeps it (rv_d0ce, 20 Nov 2026: five charts at characters
      2918-3122 of a 4562-character page lost two to the thinning in the
      direct read, where the parent's cut kept all five);
    * the crawl after a search joins its two pages, then fits them once;
    * a gallery whose pictures each carry a short caption is a row like any
      other (rv_66web E2), and the pictures shown keep their captions.

Nothing here reaches the network: requests.get serves a fixed page, the
search engines and the WAMP/crossbar publishers are replaced.

    python -m pytest tests/unit/test_every_agent_view_of_a_crawled_page_gets_the_same_fit.py -q --noconftest
"""
import ast
import logging
import os
import re
import sys
import types

import pytest
import requests

import hartos.helper as helper
from integrations import web_crawler as wc

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_ENTRY = os.path.join(_ROOT, 'hart_intelligence_entry.py')
URL = 'https://news.example.org/page'
LINK_RE = re.compile(r'!\[[^\]]*\]\([^)\s]*\)')

ARTICLE = ' '.join(f'Sentence number {i} says that demand for lash extensions grew '
                   f'in Chennai this quarter.' for i in range(1, 140))
GALLERY = ''.join(f'<img src="/media/gallery/2026/11/photo-{i}.jpg?w=300&h=200" alt="photo {i}">'
                  for i in range(40))
#: The same gallery with each picture in a figure with a short caption.
CAPTIONED = ''.join(f'<figure><img src="/media/gallery/2026/11/photo-{i}.jpg?w=300&h=200" '
                    f'alt="photo {i}"><figcaption>Photo {i}</figcaption></figure>'
                    for i in range(40))


def _page(gallery, captions=False):
    pictures = (CAPTIONED if captions else GALLERY) if gallery else ''
    return ('<html><body><nav>menu</nav><div>' + pictures
            + '</div><article><p>' + ARTICLE + '</p></article></body></html>')


def _cut_page(budget):
    """Words with no full stop until just before the budget, then pictures
    with a word between each, then words: the last '.' before the budget is
    inside a link."""
    words = ' '.join(['word'] * ((budget - 160) // 5))
    pics = ' and '.join(
        f'<img src="/media/2026/11/feature-story-photo-number-{i}-full-size.jpg'
        f'?width=1200&amp;height=800&amp;crop=smart" alt="p{i}">' for i in range(5))
    return f'<html><body><p>Intro sentence. {words}</p>{pics}<p>{words}</p></body></html>'


def _charts_page(budget):
    """rv_d0ce's page, scaled to the budget: a row of five charts at about
    three quarters of it, and the page about 14% over it."""
    pre = ' '.join(f'Opening sentence {i} explains the quarterly lash demand data.'
                   for i in range(12 * budget // 1000))
    post = ' '.join(f'Closing sentence {i} adds more detail to the story here.'
                    for i in range(25 * budget // 4000))
    charts = ''.join(f'<img src="/charts/q{i}.png" alt="chart {i}">' for i in range(5))
    return f'<html><body><p>{pre}</p>{charts}<p>{post}</p></body></html>'


class _FakePage:
    def __init__(self, html):
        self.text, self.content, self.status_code, self.url = html, html.encode(), 200, URL

    def raise_for_status(self):
        pass


class _Search:
    """The Google key's search wrapper: one result, the page."""

    def results(self, query, n):
        return [{'title': 'T', 'link': URL, 'snippet': 's'}]


@pytest.fixture
def serve(monkeypatch):
    """Serve ``html`` as the page; no browser, no setup offer, no publish."""
    monkeypatch.setattr(wc, '_crawler_available', False)
    monkeypatch.setattr(wc, '_offer_setup', lambda log: None)
    monkeypatch.setattr(helper, 'current_app',
                        types.SimpleNamespace(logger=logging.getLogger('fit_views')))
    monkeypatch.setattr(helper, '_remember_links', lambda q, l: None, raising=False)
    trace = types.ModuleType('core.peer_link.crossbar_publish')
    trace.publish_thinking_trace = lambda **k: None
    monkeypatch.setitem(sys.modules, 'core.peer_link.crossbar_publish', trace)

    def _serve(html):
        monkeypatch.setattr(requests, 'get', lambda *a, **k: _FakePage(html))
    _serve.monkeypatch = monkeypatch
    return _serve


def _md():
    """The page as every view reads it (the read without a browser)."""
    return wc._fallback_fetch(URL, 30, wc._ProgressLog())['markdown']


def _extraction_tool():
    """hart_intelligence_entry.parse_link_for_crwalab, the LangChain tool
    Data_Extraction_From_URL, lifted by name with ast (the entry module
    cannot be imported in a bare env); its WAMP publish, logger and
    thread-local ids are stand-ins, the crawl underneath is real."""
    src = open(_ENTRY, encoding='utf-8').read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == 'parse_link_for_crwalab')
    value = types.SimpleNamespace
    ns = {
        'thread_local_data': value(get_user_id=lambda: 1, get_request_id=lambda: 'r1'),
        'app': value(logger=logging.getLogger('fit_views')),
        'TaskStatus': value(EXECUTING=value(value='executing'), ERROR=value(value='error')),
        'TaskNames': value(CRAWLAB=value(value='crawlab')),
        'publish_async': lambda *a, **k: None,
        '_parse_pdf_in_process': lambda *a: 'pdf',
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _ENTRY, 'exec'), ns)
    return ns['parse_link_for_crwalab']


def _crawl_tool(serve):
    return wc.crawl_url_for_agent(URL).split('Content:\n', 1)[1]


def _data_extraction(serve):
    return _extraction_tool()(URL + ', website').split('Content:\n', 1)[1]


def _search_with_a_link(serve):
    rows = helper._keyless_top5(URL)
    assert rows[0]['method'] == 'direct_url', rows
    return rows[0]['text']


def _search_then_crawl(serve):
    serve.monkeypatch.setattr(helper, '_keyless_serp', lambda q, max_results=5: [
        {'title': 'T', 'url': URL, 'snippet': 'short'}])
    rows = helper._keyless_top5('lash extension demand chennai')
    assert rows[0]['method'] == 'serp_crawl', rows
    return rows[0]['text']


def _google_key_search(serve):
    serve.monkeypatch.setattr(helper, '_keyless_top5', lambda q: [])
    serve.monkeypatch.setattr(helper, 'search', _Search())
    rows = helper.top5_results('lash extension demand chennai')
    assert rows[0]['method'] == 'crawl4ai_api', rows
    return rows[0]['text']


#: (view, its budget, its note)
VIEWS = {
    'Data_Extraction_From_URL': (_data_extraction, 8000, '\n[Content truncated]'),
    'google_search, a link': (_search_with_a_link, 4000, ' [Content truncated]'),
    'google_search, crawl after search': (_search_then_crawl, 4000, ' [Content truncated]'),
    'google_search, Google key': (_google_key_search, 4000, ' [Content truncated]'),
}


def _sentences(text):
    return len(re.findall(r'Sentence number \d+ says', text))


@pytest.mark.parametrize('page', ['gallery', 'cut'])
def test_the_extraction_tool_gives_what_the_crawl_tool_gives(serve, page):
    serve(_page(True) if page == 'gallery' else _cut_page(8000))
    assert _data_extraction(serve) == _crawl_tool(serve)


@pytest.mark.parametrize('name', list(VIEWS))
def test_a_gallery_before_an_article_leaves_the_article_its_budget(serve, name):
    """The same page with no gallery, less the room of the 3 pictures shown
    and the note (about 290 characters, under 5 of these sentences)."""
    view, budget, _ = VIEWS[name]
    serve(_page(False))
    plain = _sentences(view(serve))
    serve(_page(True))
    got = view(serve)
    assert plain >= budget // 100, plain     # the article fills the budget
    assert len(LINK_RE.findall(got)) == 3, got[:600]
    assert '[37 more pictures here not shown]' in got, got[:600]
    assert _sentences(got) >= plain - 5, (_sentences(got), plain)


@pytest.mark.parametrize('name', list(VIEWS))
def test_a_captioned_gallery_before_an_article_leaves_the_article_its_budget(serve, name):
    """rv_66web E2 on 993f1671a: each figure's caption stood between the
    pictures, so the gallery was never a row and never thinned (137 of 139
    sentences before that commit, 101 after).  The pictures shown keep
    their captions."""
    view, budget, _ = VIEWS[name]
    serve(_page(False))
    plain = _sentences(view(serve))
    serve(_page(True, captions=True))
    got = view(serve)
    assert len(LINK_RE.findall(got)) == 3, got[:700]
    assert re.search(r'\bPhoto 2\b', got) and not re.search(r'\bPhoto 3\b', got), got[:700]
    assert '[37 more pictures here not shown]' in got, got[:700]
    assert _sentences(got) >= plain - 5, (_sentences(got), plain)


@pytest.mark.parametrize('name', list(VIEWS))
def test_the_text_never_ends_inside_a_picture_link(serve, name):
    view, budget, note = VIEWS[name]
    serve(_cut_page(budget))
    md = _md()
    assert any(m.start() < budget < m.end() for m in LINK_RE.finditer(md)), \
        'the budget must fall inside a link on this page'
    got = view(serve)
    assert got.endswith(note), got[-120:]
    body = got[:-len(note)]
    assert len(body) <= budget, len(body)
    rest = LINK_RE.sub('', body)
    assert '![' not in rest and '](' not in rest, body[-160:]


@pytest.mark.parametrize('name', list(VIEWS))
def test_a_page_that_fits_reaches_every_view_whole(serve, name):
    view, budget, note = VIEWS[name]
    html = ('<html><body><p>' + ' '.join(f'Item note {i}.' for i in range(40)) + '</p>'
            + ''.join(f'<img src="/p/item-{i:02d}.jpg" alt="item {i}">' for i in range(1, 21))
            + '</body></html>')
    serve(html)
    md = _md()
    assert len(md) < 4000
    got = view(serve)
    assert got == md, got
    assert len(LINK_RE.findall(got)) == 20 and note.strip() not in got


@pytest.mark.parametrize('name', list(VIEWS))
def test_a_row_of_charts_inside_the_budget_is_kept_whole(serve, name):
    view, budget, note = VIEWS[name]
    serve(_charts_page(budget))
    md = _md()
    rows = [m.span() for m in wc._PICTURE_RUN_RE.finditer(md)]
    assert len(md) > budget and len(rows) == 1, (len(md), rows)
    assert budget // 2 < rows[0][0] and rows[0][1] <= budget, rows
    got = view(serve)
    assert len(LINK_RE.findall(got)) == 5, got[-400:]
    assert 'not shown' not in got and got.endswith(note), got[-400:]


def test_pictures_between_sentences_are_not_a_row():
    """An illustrated article (a picture, a sentence, a picture, ...) is not
    a gallery: only a short caption with no sentence end joins two pictures
    into a row, so these pictures stay wherever the cut keeps them."""
    body = ' '.join(f'![figure {i}](https://x.example/fig-{i}.png) Figure {i} shows '
                    f'the demand in month {i} of the year.' for i in range(60))
    assert len(body) > 4000
    out = wc._fit_for_agent(body, 4000, ' [Content truncated]')
    assert 'not shown' not in out, out[:400]
    assert len(LINK_RE.findall(out)) > 30, out[-300:]


def test_a_small_row_reaching_past_the_budget_is_thinned():
    """Twelve small pictures that start inside the budget and end past it:
    kept whole, the cut would fall before the row and lose it and every
    word after it; thinned, 3 pictures, the note and the next words fit."""
    budget, pre, i = 4000, '', 0
    while len(pre) < budget - 300:
        pre += f'Line {i} of the page. '
        i += 1
    pics = ' '.join(f'![p{i}](https://x.example/p{i}.jpg)' for i in range(12))
    content = pre + pics + ' ' + ' '.join(f'After line {i}.' for i in range(100))
    row = wc._PICTURE_RUN_RE.search(content)
    assert row.start() < budget < row.end() <= row.start() + budget // 4, row.span()
    out = wc._fit_for_agent(content, budget, ' [Content truncated]')
    assert len(LINK_RE.findall(out)) == 3, out[-500:]
    assert '[9 more pictures here not shown]' in out and 'After line 0.' in out, out[-500:]


def test_the_crawl_after_a_search_joins_its_two_pages_then_fits_them(serve):
    second = ('<html><body><p>' + ' '.join(
        f'Second page line {i} gives lash prices.' for i in range(100)) + '</p></body></html>')
    pages = {URL: _page(True), URL + '/2': second}
    serve.monkeypatch.setattr(requests, 'get', lambda url, *a, **k: _FakePage(pages[url]))
    serve.monkeypatch.setattr(helper, '_keyless_serp', lambda q, max_results=5: [
        {'title': 'A', 'url': URL, 'snippet': 'short'},
        {'title': 'B', 'url': URL + '/2', 'snippet': 'short'}])
    joined = ' '.join(wc._fallback_fetch(u, 30, wc._ProgressLog())['markdown'] for u in pages)
    rows = helper._keyless_top5('lash extension demand chennai')
    assert rows[0]['method'] == 'serp_crawl' and rows[0]['source'] == [URL, URL + '/2'], rows
    assert rows[0]['text'] == wc._fit_for_agent(joined, 4000, ' [Content truncated]')
    assert rows[0]['word_count'] == len(joined.split())
