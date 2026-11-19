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
      whose HTML is only a logo still fails as before.

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
