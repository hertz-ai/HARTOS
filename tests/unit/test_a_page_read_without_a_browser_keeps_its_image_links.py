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
import pytest
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

    # The reader streams the body and stops at its size limit.
    encoding = 'utf-8'
    chunks_read = 0

    def iter_content(self, chunk_size=65536):
        body = self.content
        for i in range(0, len(body), chunk_size):
            self.chunks_read += 1
            yield body[i:i + chunk_size]

    def close(self):
        self.closed = True


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


# -- words in separate boxes stay separate words (review of 8c595f0db) ------
# Measured on this fallback, a desktop without crawl4ai: a page sent without
# line breaks came back as "ItemPriceRice42Dal95" and "Phone:+91 98400
# 00000Emailsales@ex.org".  get_text() joins the text of neighbouring
# elements when no whitespace stands between them in the HTML.

_FILL = '<p>' + 'Prices are checked every morning at the market. ' * 3 + '</p>'


def _words(monkeypatch, body):
    _serve(monkeypatch, '<html><body>' + _FILL + body + '</body></html>')
    result = _read()
    assert result['success'] is True, result
    return result['markdown']


def test_a_table_sent_without_line_breaks_keeps_its_cells_apart(monkeypatch):
    md = _words(monkeypatch,
                '<table><tr><th>Item</th><th>Price</th></tr><tr><td>Rice</td>'
                '<td>42</td></tr><tr><td>Dal</td><td>95</td></tr></table>')
    assert md.endswith('Item Price Rice 42 Dal 95'), md


def test_two_lines_of_labels_stay_apart_and_each_reads_as_it_is_written(monkeypatch):
    """The two lines are two boxes and stay apart.  Inside a line the label
    and its value are neighbouring inline elements with nothing between
    them, which a browser shows joined; they read joined (review of
    5fcacc57d: a space added there split prices, code and words)."""
    md = _words(monkeypatch,
                '<div><span>Phone:</span><span>+91 98400 00000</span></div>'
                '<div><b>Email</b><i>sales@ex.org</i></div>')
    assert md.endswith('Phone:+91 98400 00000 Emailsales@ex.org'), md


def test_a_label_and_value_written_with_a_space_between_keep_it(monkeypatch):
    """The page's own whitespace decides, never an added space."""
    md = _words(monkeypatch,
                '<div><span>Phone:</span> <span>+91 98400 00000</span></div>'
                '<div><b>Email</b>\n<i>sales@ex.org</i></div>')
    assert md.endswith('Phone: +91 98400 00000 Email sales@ex.org'), md


# Review of 5fcacc57d, measured: a space before every element that follows
# another split each of these.
@pytest.mark.parametrize('body, read', [
    # a price built from spans (the shape shop pages use)
    ('<p>Now <span class="a-price"><span class="sym">\u20b9</span><span class="whole">1,299'
     '<span class="dec">.</span></span><span class="frac">00</span></span> only</p>',
     'Now \u20b91,299.00 only'),
    # highlighted code, one span per token
    ('<pre><code><span class="n">os</span><span class="o">.</span><span class="n">path</span>'
     '<span class="o">.</span><span class="n">join</span><span class="p">(</span>'
     '<span class="n">a</span><span class="p">)</span></code></pre>',
     'os.path.join(a)'),
    # a heading revealed letter by letter
    ('<h1><span>W</span><span>e</span><span>l</span><span>c</span><span>o</span>'
     '<span>m</span><span>e</span></h1>', 'Welcome'),
    # an email address in pieces
    ('<p>Write to <span>sales</span><span>@</span><span>ex.org</span> today</p>',
     'Write to sales@ex.org today'),
    # one word, half bold and half italic
    ('<p><b>Hel</b><i>lo</i> world</p>', 'Hello world'),
    # two links making one word
    ('<p><a href="/x">inter</a><a href="/y">national</a> calls</p>', 'international calls'),
    # a footnote mark after its word, a comma after a link
    ('<p>Speech delay<sup><a href="#n1">[1]</a></sup> is common.</p>',
     'Speech delay[1] is common.'),
    ('<p>See <a href="/a">this</a><span>,</span> then go.</p>', 'See this, then go.'),
], ids=['price', 'code', 'letter-heading', 'email', 'bold-italic-halves', 'two-links',
        'footnote', 'comma-after-link'])
def test_neighbouring_inline_elements_are_not_split(monkeypatch, body, read):
    md = _words(monkeypatch, body)
    assert md.endswith('market. ' + read), md


def test_list_items_headings_and_line_breaks_stay_apart(monkeypatch):
    md = _words(monkeypatch,
                '<h2>Hours</h2><ul><li>Mon</li><li>Tue</li></ul>open<br>daily')
    assert md.endswith('Hours Mon Tue open daily'), md


def test_an_inline_element_inside_a_word_does_not_split_it(monkeypatch):
    md = _words(monkeypatch,
                '<p><b>H</b>ello, H<sub>2</sub>O and <em>very</em>! good.</p>')
    assert md.endswith('Hello, H2O and very! good.'), md


def test_a_page_written_with_line_breaks_reads_as_before(monkeypatch):
    _serve(monkeypatch, ARTICLE_WITH_A_CHART)
    assert _read()['markdown'] == (
        'Speech therapy outcomes Children who practised every day improved '
        'more than those who practised weekly. ' + CHART_LINK
        + ' Gains held at the six month check.')


# -- the bytes read are bounded before anything is parsed -------------------
# Review of 8c595f0db: a 20 MB page took 2.1 s and peaked at 255 MB.

def test_a_page_past_the_size_limit_is_read_up_to_it(monkeypatch):
    import core.constants as constants
    monkeypatch.setattr(constants, 'MAX_PAYLOAD_BYTES', 200_000)
    pages = []
    html = ('<html><body><p>The first paragraph is the one that matters.</p>'
            + '<p>' + 'filler words go on and on. ' * 40_000 + '</p>'
            + '<p>THE-VERY-END</p></body></html>')
    assert len(html) > 1_000_000

    def fake_get(url, headers=None, timeout=None, **kwargs):
        pages.append((_FakePage(html, url), kwargs))
        return pages[-1][0]
    monkeypatch.setattr(requests, 'get', fake_get)
    log = wc._ProgressLog()
    result = wc._fallback_fetch(PAGE, 30, log)
    page, kwargs = pages[0]
    assert kwargs.get('stream') is True, 'the body is not read before the limit is known'
    assert page.chunks_read <= 200_000 // 65536 + 2, page.chunks_read
    assert page.closed is True
    assert result['success'] is True
    assert result['markdown'].startswith('The first paragraph is the one that matters.')
    assert 'THE-VERY-END' not in result['markdown']
    assert len(result['markdown']) <= 200_000
    assert 'read up to the size limit' in log.text()


def test_a_page_within_the_limit_is_read_whole(monkeypatch):
    _serve(monkeypatch, ARTICLE_WITH_A_CHART)
    log = wc._ProgressLog()
    result = wc._fallback_fetch(PAGE, 30, log)
    assert result['markdown'].endswith('Gains held at the six month check.')
    assert f"HTTP 200, {len(ARTICLE_WITH_A_CHART.encode())} bytes received" in log.text()
    assert 'size limit' not in log.text()


def test_the_page_is_decoded_as_it_declares(monkeypatch):
    html = ARTICLE_WITH_A_CHART.replace('Gains held', 'Caf\u00e9 gains held')

    def fake_get(url, headers=None, timeout=None, **kwargs):
        page = _FakePage(html, url)
        page.content = html.encode('latin-1')
        page.encoding = 'ISO-8859-1'
        return page
    monkeypatch.setattr(requests, 'get', fake_get)
    assert 'Caf\u00e9 gains held' in _read()['markdown']


# -- the read with a cookie jar is the same plain read (audit row H19) ------
# web_crawler._fallback_fetch_with_cookies (browser_research's platform
# scripts, when crawl4ai is not installed) carried a copy of the plain read:
# the whole body read whatever its size, and the words of neighbouring boxes
# run together.  It reads through _bounded_text and _page_text now; its
# answer keeps its own shape.

class _FakeJar:
    def __init__(self):
        self.kept = []

    def set(self, name, value, domain=None, path=None):
        self.kept.append((name, value, domain, path))


def _serve_session(monkeypatch, html, status=200, raises=None):
    """requests.Session() for the cookie read: one fixed page."""
    made = []

    class _FakeSession:
        def __init__(self):
            self.cookies = _FakeJar()
            self.asked = []
            self.pages = []
            made.append(self)

        def get(self, url, timeout=None, headers=None, **kwargs):
            self.asked.append({'url': url, 'timeout': timeout, 'headers': headers,
                               'kwargs': kwargs})
            if raises is not None:
                raise raises
            self.pages.append(_FakePage(html, url, status))
            return self.pages[-1]
    monkeypatch.setattr(requests, 'Session', _FakeSession)
    monkeypatch.setattr(wc, '_check_available', lambda: False)
    return made


_COOKIE_KEYS = {'success', 'url', 'markdown', 'word_count', 'status_code',
                'connection_mechanism', 'progress'}


def test_the_cookie_read_keeps_the_words_of_separate_boxes_apart(monkeypatch):
    _serve_session(monkeypatch,
                   '<html><body><table><tr><th>Item</th><th>Price</th></tr><tr><td>Rice</td>'
                   '<td>42</td></tr></table><ul><li>Mon</li><li>Tue</li></ul></body></html>')
    result = wc.crawl_url_with_cookies(PAGE, cookies=[])
    assert result['markdown'] == 'Item Price Rice 42 Mon Tue', result
    assert result['word_count'] == 6


def test_the_cookie_read_does_not_split_a_word_either(monkeypatch):
    _serve_session(monkeypatch,
                   '<html><body><p>Now <span>\u20b9</span><span>1,299</span><span>.</span>'
                   '<span>00</span> only, <b>Hel</b><i>lo</i>.</p></body></html>')
    result = wc.crawl_url_with_cookies(PAGE, cookies=[])
    assert result['markdown'] == 'Now \u20b91,299.00 only, Hello.', result


def test_the_cookie_read_keeps_a_picture_where_it_stood(monkeypatch):
    _serve_session(monkeypatch, ARTICLE_WITH_A_CHART)
    result = wc.crawl_url_with_cookies(PAGE, cookies=[])
    assert result['markdown'] == (
        'Speech therapy outcomes Children who practised every day improved '
        'more than those who practised weekly. ' + CHART_LINK
        + ' Gains held at the six month check.')


def test_the_cookie_read_stops_at_the_size_limit(monkeypatch):
    import core.constants as constants
    monkeypatch.setattr(constants, 'MAX_PAYLOAD_BYTES', 200_000)
    html = ('<html><body><p>The first paragraph is the one that matters.</p>'
            + '<p>' + 'filler words go on and on. ' * 40_000 + '</p>'
            + '<p>THE-VERY-END</p></body></html>')
    made = _serve_session(monkeypatch, html)
    result = wc.crawl_url_with_cookies(PAGE, cookies=[])
    session = made[0]
    page = session.pages[0]
    assert session.asked[0]['kwargs'].get('stream') is True, \
        'the body is not read before the limit is known'
    assert page.chunks_read <= 200_000 // 65536 + 2, page.chunks_read
    assert page.closed is True
    assert result['success'] is True
    assert result['markdown'].startswith('The first paragraph is the one that matters.')
    assert result['word_count'] < 60_000, 'the words of the part read, not of the whole page'


# -- carried over: the cookie read's own answer, before and after -----------

def test_the_cookie_read_sends_the_jar_and_answers_in_its_own_shape(monkeypatch):
    made = _serve_session(monkeypatch, '<html><body><p>Hi   there.</p>\n<p>Bye\tnow.</p></body></html>')
    result = wc.crawl_url_with_cookies(
        PAGE, cookies=[{'name': 'sid', 'value': 'abc', 'domain': '.example.org'},
                       {'name': 'theme', 'value': 'dark', 'path': '/post'},
                       {'name': 'empty', 'value': ''}, {'value': 'nameless'}],
        timeout=17)
    session = made[0]
    assert session.cookies.kept == [('sid', 'abc', '.example.org', '/'),
                                    ('theme', 'dark', None, '/post')]
    assert session.asked[0]['url'] == PAGE and session.asked[0]['timeout'] == 17
    assert session.asked[0]['headers'] == {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    assert set(result) == _COOKIE_KEYS
    # A page of a few words is a success here (no "too little content"
    # rule), and every run of whitespace is one space.
    assert result['success'] is True and result['status_code'] == 200
    assert result['markdown'] == 'Hi there. Bye now.' and result['word_count'] == 4
    assert result['url'] == PAGE and result['connection_mechanism'] == 'public_http'
    assert 'requests + cookies, no JS' in result['progress']


def test_the_cookie_read_of_an_error_page_fails_and_still_hands_its_text(monkeypatch):
    _serve_session(monkeypatch, '<html><body><h1>Sign in</h1>\n<p>to see this page.</p></body></html>',
                   status=403)
    result = wc.crawl_url_with_cookies(PAGE, cookies=[])
    assert set(result) == _COOKIE_KEYS
    assert result['success'] is False and result['status_code'] == 403
    assert result['markdown'] == 'Sign in to see this page.'


def test_the_cookie_read_cuts_its_text_at_50000_and_counts_all_of_it(monkeypatch):
    _serve_session(monkeypatch, '<html><body><p>' + 'word ' * 20_000 + '</p></body></html>')
    result = wc.crawl_url_with_cookies(PAGE, cookies=[])
    assert len(result['markdown']) == 50000 and result['word_count'] == 20_000


def test_a_cookie_read_that_cannot_connect_says_why(monkeypatch):
    _serve_session(monkeypatch, '', raises=requests.ConnectionError('no route to host'))
    result = wc.crawl_url_with_cookies(PAGE, cookies=[])
    assert set(result) == {'success', 'url', 'error', 'progress', 'connection_mechanism'}
    assert result['success'] is False and result['error'] == 'no route to host'
    assert result['connection_mechanism'] == 'public_http'


def test_the_two_plain_reads_give_the_same_text(monkeypatch):
    """One reader: the public read and the cookie read of one page."""
    html = ('<html><body>' + _FILL + '<table><tr><td>Rice</td><td>42</td></tr></table>'
            '<img src="/media/outcomes-by-age.png" alt="Outcomes by age">'
            '<p><b>Hel</b><i>lo</i></p></body></html>')
    _serve(monkeypatch, html)
    public = _read()['markdown']
    _serve_session(monkeypatch, html)
    assert wc.crawl_url_with_cookies(PAGE, cookies=[])['markdown'] == public
    assert public.endswith('Rice 42 ' + CHART_LINK + ' Hello')
