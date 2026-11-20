"""A page read without a browser keeps its pictures' links, where they stood.

WHAT WENT WRONG (owner's desktop, build 75, 17 Nov 2026, 14:11-14:22 IST)
  The Autonomous Research Assistant's create step 5 was "get_text_from_image
  for the primary research topic": pick a chart from the pages crawled in
  steps 3 and 4 and read it.  crawl4ai is not installed on that desktop, so
  every crawl ran integrations/web_crawler.py's requests+BeautifulSoup
  fallback, which turned each page into its words only and dropped every
  <img>.  The model said so itself ("The content I received was text-based,
  and I don't have direct image URLs from the crawled pages"), called
  get_text_from_image with links it made up (HTTP 400 twice), failed the
  step three times and asked the person for an image link.

THE RULE
  The fallback keeps each picture as ``![alt](link)`` at the place it stood
  in the page, the shape crawl4ai's markdown gives, so every caller of
  crawl_url / crawl_urls / crawl4ai_crawl reads the same kind of page from
  either path:
    * a relative link is made whole against the page actually reached
      (after a redirect);
    * a lazy picture's real link (data-src) is used, not its data: stand-in;
    * a picture with no web link, or inside the nav/header/footer that are
      already dropped, is left out;
    * "too little content" is still judged on the words alone, so a page
      whose HTML is only a logo still fails as before;
    * every distinct link is kept once, as crawl4ai keeps them.

  What the agent's text budget leaves out is decided where the budget is
  (crawl_url_for_agent 8000, crawl_urls_for_agent 4000 per page): a page
  that fits reaches the agent whole; one that does not keeps the first 3
  pictures of each row of pictures with no words between (a gallery, a row
  of share icons) that reaches past the budget or takes more than a quarter
  of it, and is told how many more stood there, so a picture standing among
  the words stays; a smaller row wholly inside the budget is kept whole, as
  a plain cut keeps it (rv_d0ce); the text is cut at a sentence end, never
  inside a picture link.  The agent gets that only when it has at least the
  pictures and the sentences of the live plain cut (its first ``budget``
  characters); otherwise it gets the plain cut, a half link at its end
  trimmed (rv_fbc2, owner: never worse than before on anything measured).

Nothing here reaches the network: requests.get is replaced by a fake page.

    python -m pytest tests/unit/test_a_page_read_without_a_browser_keeps_its_image_links.py -q --noconftest
"""
import requests

from integrations import web_crawler as wc

PAGE = 'https://www.example.org/post/outcomes'


class _FakePage:
    """What requests.get returns, for one fixed HTML page."""

    def __init__(self, html, url, status=200):
        self.text = html
        self.content = html.encode('utf-8')
        self.status_code = status
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: Forbidden for url: {self.url}")


def _serve(monkeypatch, html, final_url=None, status=200):
    asked = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        asked.append({'url': url, 'headers': headers, 'timeout': timeout})
        return _FakePage(html, final_url or url, status)
    monkeypatch.setattr(requests, 'get', fake_get)
    return asked


def _read(url=PAGE):
    return wc._fallback_fetch(url, 30, wc._ProgressLog())


ARTICLE_WITH_A_CHART = """<html><body>
<h1>Speech therapy outcomes</h1>
<p>Children who practised every day improved more than those who practised weekly.</p>
<img src="/media/outcomes-by-age.png" alt="Outcomes by age">
<p>Gains held at the six month check.</p>
</body></html>"""

CHART_LINK = '![Outcomes by age](https://www.example.org/media/outcomes-by-age.png)'


# -- the defect: the picture's link reaches whoever reads the page ----------

def test_a_chart_on_the_page_reaches_the_reader_as_an_image_link(monkeypatch):
    _serve(monkeypatch, ARTICLE_WITH_A_CHART)
    result = _read()
    assert result['success'] is True, result
    assert CHART_LINK in result['markdown'], result['markdown']


def test_the_image_link_stands_where_the_picture_stood(monkeypatch):
    _serve(monkeypatch, ARTICLE_WITH_A_CHART)
    md = _read()['markdown']
    assert CHART_LINK in md, md
    assert md.index('weekly.') < md.index(CHART_LINK) < md.index('Gains held'), md


def test_a_relative_link_is_made_whole_against_the_page_reached_after_a_redirect(monkeypatch):
    html = ARTICLE_WITH_A_CHART.replace('/media/outcomes-by-age.png', 'chart.png')
    _serve(monkeypatch, html, final_url='https://www.example.org/post/new/')
    md = _read('http://example.org/old-post')['markdown']
    assert '![Outcomes by age](https://www.example.org/post/new/chart.png)' in md, md


def test_a_lazy_picture_gives_its_real_link_not_its_stand_in(monkeypatch):
    html = ARTICLE_WITH_A_CHART.replace(
        '<img src="/media/outcomes-by-age.png" alt="Outcomes by age">',
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" '
        'data-src="https://cdn.example.org/figures/fig1.jpg" alt="Figure 1">')
    _serve(monkeypatch, html)
    md = _read()['markdown']
    assert '![Figure 1](https://cdn.example.org/figures/fig1.jpg)' in md, md
    assert 'data:image' not in md, md


def test_the_crawl_tool_shows_the_model_the_image_link(monkeypatch):
    """The whole tool path the research agent called (crawl4ai_crawl ->
    crawl_url_for_agent -> crawl_url -> the fallback) with crawl4ai absent.
    The setup offer is faked: no consent row, no install."""
    _serve(monkeypatch, ARTICLE_WITH_A_CHART)
    monkeypatch.setattr(wc, '_crawler_available', False)
    monkeypatch.setattr(wc, '_offer_setup', lambda log: None)
    out = wc.crawl_url_for_agent(PAGE)
    assert f"URL: {PAGE}" in out, out
    assert CHART_LINK in out, out


# -- guards: what the fallback already did, it still does -------------------

def test_a_page_with_no_pictures_reads_exactly_as_before(monkeypatch):
    _serve(monkeypatch, """<html><body>
<nav>Home About Blog</nav>
<h1>Speech therapy outcomes</h1>
<p>Children who practised every day improved more than those who practised weekly.</p>
<footer>Copyright footer text</footer>
<script>var tracking = 1;</script>
</body></html>""")
    result = _read()
    assert result == {
        'success': True, 'url': PAGE,
        'markdown': ('Speech therapy outcomes Children who practised every day '
                     'improved more than those who practised weekly.'),
        'word_count': 15,
    }, result


def test_pictures_in_the_dropped_navigation_and_footer_stay_out(monkeypatch):
    html = ARTICLE_WITH_A_CHART.replace(
        '<h1>', '<nav><img src="/nav-logo.png" alt="Nav logo"></nav><h1>').replace(
        '</body>', '<footer><img src="/footer-badge.png" alt="Badge"></footer></body>')
    _serve(monkeypatch, html)
    md = _read()['markdown']
    assert 'nav-logo' not in md and 'footer-badge' not in md, md


def test_a_picture_with_no_web_link_is_left_out(monkeypatch):
    html = ARTICLE_WITH_A_CHART.replace(
        '<img src="/media/outcomes-by-age.png" alt="Outcomes by age">',
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="Inline">')
    _serve(monkeypatch, html)
    md = _read()['markdown']
    assert 'data:image' not in md and 'Inline' not in md, md


def test_a_page_that_is_only_a_logo_is_still_too_little_content(monkeypatch):
    _serve(monkeypatch, '<html><body>'
           '<img src="/static/media/brand/company-logo-large-retina.png" alt="Company logo">'
           '<p>Loading</p></body></html>')
    result = _read()
    assert result == {'success': False, 'url': PAGE,
                      'error': 'Too little content extracted'}, result


def test_a_refused_page_still_fails_with_its_status(monkeypatch):
    _serve(monkeypatch, '<html><body>Forbidden</body></html>', status=403)
    result = _read('https://www.sciencedirect.com/science/article/pii/S0165027019300437')
    assert result['success'] is False
    assert '403' in result['error'], result


def test_the_request_is_made_as_before(monkeypatch):
    asked = _serve(monkeypatch, ARTICLE_WITH_A_CHART)
    wc._fallback_fetch(PAGE, 17, wc._ProgressLog())
    assert len(asked) == 1
    assert asked[0]['url'] == PAGE and asked[0]['timeout'] == 17
    assert asked[0]['headers']['User-Agent'].startswith('Mozilla/5.0')


def test_the_word_count_counts_what_the_reader_gets(monkeypatch):
    _serve(monkeypatch, ARTICLE_WITH_A_CHART)
    result = _read()
    assert result['word_count'] == len(result['markdown'].split()), result


# The links share the agent's text budget with the page's words (rv_b9,
# 2026-11-18: a 40-picture gallery before an article cut the article from 93
# sentences to 54 under the 8000-character budget; rv_66web, same day: a cap
# on the page's first 12 pictures won the words back and lost the article's
# chart behind a header's logo, share icons and tracking pixel).

def _gallery(n, repeat_logo=False, sentences=30):
    pics = ''.join(f'<img src="/media/photo-{i}.jpg" alt="photo {i}">' for i in range(n))
    logo = '<img src="/media/logo.png" alt="logo">' * (3 if repeat_logo else 0)
    words = ' '.join(f'Sentence {i} of the article.' for i in range(sentences))
    return f'<html><body>{logo}{pics}<p>{words}</p></body></html>'


def _agent_view(monkeypatch, html):
    _serve(monkeypatch, html)
    monkeypatch.setattr(wc, '_crawler_available', False)
    monkeypatch.setattr(wc, '_offer_setup', lambda log: None)
    return wc.crawl_url_for_agent(PAGE).split('Content:\n', 1)[1]


CHART = '<img src="/media/charts/demand-2026.png" alt="Demand chart 2026">'
LONG_WORDS = ' '.join(f'Paragraph sentence {i} about lash extension demand.' for i in range(120))
DECORATED_ARTICLE = (
    '<html><body><div class="site-header"><img src="/static/logo.svg" alt="News">'
    + ''.join(f'<a href="#"><img src="/static/icons/{n}.svg" alt="{n}"></a>'
              for n in ('facebook', 'x', 'linkedin', 'whatsapp', 'email', 'print'))
    + '<img src="https://px.tracker.example/t.gif?id=42" width="1" height="1" alt="">'
    + '<img src="/media/authors/meena.jpg" alt="Meena, reporter"></div>'
    + '<div class="related">'
    + ''.join(f'<img src="/media/related/r{i}.jpg" alt="related {i}">' for i in range(4))
    + '</div><article><p>{words}</p>' + CHART + '<p>{words}</p></article></body></html>')


def test_the_read_keeps_every_distinct_picture_as_crawl4ai_does(monkeypatch):
    _serve(monkeypatch, _gallery(40))
    md = _read()['markdown']
    assert md.count('![') == 40, md.count('![')
    assert 'photo-39.jpg' in md and 'Sentence 29 of the article.' in md


def test_a_chart_behind_more_than_twelve_decorative_pictures_reaches_the_agent(monkeypatch):
    """rv_66web's page: 13 pictures in the header and related strip come
    before the article's chart; the page fits the budget."""
    words = ' '.join(f'Demand grew in month {i} of the year.' for i in range(6))
    view = _agent_view(monkeypatch, DECORATED_ARTICLE.replace('{words}', words))
    assert '![Demand chart 2026](https://www.example.org/media/charts/demand-2026.png)' in view, view
    assert view.count('![') == 14 and 'not shown' not in view, view


def test_over_the_budget_a_chart_stays_and_a_small_row_inside_the_budget_is_kept_whole(monkeypatch):
    """The 13 pictures of the header and related strip stand in one row of
    about 750 characters, wholly inside the budget: a plain cut keeps them
    all, and so does the fit (rv_d0ce)."""
    view = _agent_view(monkeypatch, DECORATED_ARTICLE.replace('{words}', LONG_WORDS))
    assert 'demand-2026.png' in view, view
    assert view.count('![') == 14, view
    assert 'not shown' not in view, view


def _never_worse(view, md, budget=8000):
    """rv_fbc2 on fbc29ad4d: the agent's text has at least the pictures and
    the sentences of the live plain cut, its first ``budget`` characters."""
    got, plain = wc._measure(view), wc._measure(md[:budget])
    assert got[0] >= plain[0] and got[1] >= plain[1], (got, plain)


def test_a_header_strip_over_a_quarter_of_the_budget_is_thinned_and_the_chart_stays(monkeypatch):
    """rv_fbc2 F2: the 13-picture header has 40 related thumbnails more, a
    row of about 2800 characters, over a quarter of the budget, so the
    thinning cuts it to 3 and the note, and the article's chart after it
    stays.  The agent gets that or the plain cut, whichever is not worse."""
    html = DECORATED_ARTICLE.replace('{words}', LONG_WORDS).replace(
        '</div><article>', ''.join(f'<img src="/media/related/m{i}.jpg" alt="more {i}">'
                                   for i in range(40)) + '</div><article>')
    _serve(monkeypatch, html)
    md = _read()['markdown']
    row = wc._PICTURE_RUN_RE.search(md)
    assert row.end() - row.start() > 8000 // wc._ROW_KEPT_WHOLE_PART, row.span()
    thinned = wc._thin_and_cut(md, 8000, '\n[Content truncated]')
    assert '[50 more pictures here not shown]' in thinned, thinned[:400]
    assert 'demand-2026.png' in thinned and thinned.count('![') == 4, thinned
    _never_worse(_agent_view(monkeypatch, html), md)


def test_a_gallery_before_a_long_article_is_never_worse_than_the_plain_cut(monkeypatch):
    """rv_b9's gallery page.  The thinning keeps the first 3 pictures and
    the note; the agent gets that only when it loses no picture and no
    sentence against the live plain cut (rv_fbc2: the live cut shows all 40
    pictures there, so the agent gets the plain cut)."""
    _serve(monkeypatch, _gallery(40, sentences=400))
    md = _read()['markdown']
    thinned = wc._thin_and_cut(md, 8000, '\n[Content truncated]')
    assert thinned.count('![') == 3 and '[37 more pictures here not shown]' in thinned
    assert 'photo-0.jpg' in thinned and 'photo-3.jpg' not in thinned, thinned[:600]
    _never_worse(_agent_view(monkeypatch, _gallery(40, sentences=400)), md)


def test_a_picture_leading_a_gallery_stays_over_the_budget(monkeypatch):
    """The chart and an 80-picture gallery make one row that reaches past
    the budget, so the thinning cuts it, and the chart that leads it stays;
    the agent's text keeps the chart either way and is never worse."""
    html = ('<html><body><article><p>' + LONG_WORDS + '</p>' + CHART + '</article><div>'
            + ''.join(f'<img src="/g/{i}.jpg" alt="g{i}">' for i in range(80))
            + '</div><p>' + LONG_WORDS + '</p></body></html>')
    _serve(monkeypatch, html)
    md = _read()['markdown']
    thinned = wc._thin_and_cut(md, 8000, '\n[Content truncated]')
    assert 'demand-2026.png' in thinned and '[78 more pictures here not shown]' in thinned
    view = _agent_view(monkeypatch, html)
    assert 'demand-2026.png' in view, view
    _never_worse(view, md)


def test_a_page_inside_the_budget_reaches_the_agent_whole(monkeypatch):
    html = ('<html><body><p>' + ' '.join(f'Item note {i}.' for i in range(40)) + '</p>'
            + ''.join(f'<img src="/p/item-{i:02d}.jpg" alt="item {i}">' for i in range(1, 21))
            + '</body></html>')
    _serve(monkeypatch, html)
    md = _read()['markdown']
    view = _agent_view(monkeypatch, html)
    assert view == md, view
    assert view.count('![') == 20 and 'item-15.jpg' in view


def test_the_cut_never_ends_inside_a_picture_link(monkeypatch):
    """rv_b9 F2: words with no full stop, pictures, more words: the last '.'
    before the budget is inside a link."""
    words = ' '.join(['word'] * 1550)
    pics = ''.join(f'<img src="/media/photo-{i}.jpg" alt="p{i}">' for i in range(2))
    view = _agent_view(monkeypatch, '<html><body><p>Intro sentence. ' + words + '</p>'
                       + pics + '<p>' + words + '</p></body></html>')
    body = view.rsplit('\n[Content truncated]', 1)[0]
    assert view.endswith('\n[Content truncated]'), view[-120:]
    assert body.count('](') == body.count(')') and not body.endswith('.'), body[-120:]
    assert len(body) <= 8000


def test_a_hard_cut_never_lands_inside_a_picture_link():
    """No sentence end late enough, and the budget falls inside a link: the
    cut is made before the link, not through it."""
    head = 'Intro sentence. ' + 'word ' * 1594
    link = '![p](https://x.example/a.jpg)'
    content = head + link + ' ' + 'word ' * 2000
    assert content.index(link) < 8000 < content.index(link) + len(link)
    out = wc._fit_for_agent(content, 8000, '\n[Content truncated]')
    assert out == head + '\n[Content truncated]', out[-80:]


def test_several_pages_get_the_same_fit_per_page(monkeypatch):
    md = ''.join(f'![g{i}](https://x.example/g{i}.jpg) ' for i in range(40)) + ' '.join(
        f'Line {i} of the page.' for i in range(400))
    monkeypatch.setattr(wc, 'crawl_urls', lambda urls, timeout=30: [
        {'success': True, 'url': u, 'markdown': md, 'word_count': len(md.split())} for u in urls])
    out = wc.crawl_urls_for_agent(['https://x.example/1', 'https://x.example/2'])
    assert out.count(' [truncated]') == 2, out[-200:]
    plain = wc._measure(md[:4000])
    assert all(g >= 2 * p for g, p in zip(wc._measure(out), plain)), (wc._measure(out), plain)


def test_a_picture_repeated_on_the_page_is_linked_once(monkeypatch):
    _serve(monkeypatch, _gallery(2, repeat_logo=True))
    md = _read()['markdown']
    assert md.count('logo.png') == 1
    assert md.count('![') == 3
