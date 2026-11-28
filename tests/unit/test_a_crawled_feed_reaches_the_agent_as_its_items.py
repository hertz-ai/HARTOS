"""A crawled RSS, Atom or JSON feed reaches the agent as its items.

WHAT WENT WRONG (owner's desktop, 28 Nov 2026, 22:07 IST, News Channel demo
agent, prompt 1795883185646)
  Action 2 crawled https://www.theverge.com/rss/index.xml with crawl4ai_crawl.
  crawl4ai is not installed, so integrations/web_crawler.py's read without a
  browser turned the feed into page text: 412,067 characters, mostly the
  items' escaped HTML bodies.  The agent's view of a tool result is short
  (the model received 3,027 characters of it, measured in llm_outbound.jsonl),
  which held the feed's own description and ONE story.  Asked for three
  technology stories, the model sent the person one real one and two it made
  up, with links that do not exist ("Tesla's FSD v12", "Apple Vision Pro 2").

THE RULE
  A body that is a feed (integrations.social.feed_import, the one feed reader
  this code base has: feedparser for RSS/Atom, JSON Feed by hand) is read as
  its items: title, date and link on one line each, then a short plain
  summary, newest as the feed orders them.  So the first few thousand
  characters the agent sees hold many real stories with their real links.
  Anything that is not a feed is read as before (a JSON API reply is a
  page, only a jsonfeed.org document is a feed).  A node without
  feedparser reads RSS 2.0, RSS 1.0 and Atom with the standard library; a
  feed cut at the read limit keeps the items finished before the cut.

Nothing here reaches the network: requests.get is replaced by a fake page.

    python -m pytest tests/unit/test_a_crawled_feed_reaches_the_agent_as_its_items.py -q --noconftest
"""
import json

import pytest
import requests

from integrations import web_crawler as wc
from integrations.social import feed_import

FEED_URL = 'https://news.example.org/rss/index.xml'


class _FakePage:
    """What requests.get returns, for one fixed body."""

    encoding = 'utf-8'

    def __init__(self, body, url):
        self.text = body
        self.content = body.encode('utf-8')
        self.status_code = 200
        self.url = url

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        self.closed = True


@pytest.fixture
def served(monkeypatch):
    """Serve one body for every requests.get; crawl4ai absent, no setup."""
    monkeypatch.setattr(wc, '_check_available', lambda: False)
    monkeypatch.setattr(wc, '_offer_setup', lambda log: None)
    bodies = {}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        return _FakePage(bodies['body'], url)
    monkeypatch.setattr(requests, 'get', fake_get)
    return bodies


def _long_html_body(n):
    return ('&lt;p&gt;' + ('Long story body with a lot of markup and words. ' * 40)
            + f'&lt;div class="product-block"&gt;&lt;h3&gt;Item {n}&lt;/h3&gt;&lt;/div&gt;&lt;/p&gt;')


def _rss(count=30):
    items = ''.join(
        f'<item><title>Story number {n} headline</title>'
        f'<link>https://news.example.org/story-{n}</link>'
        f'<pubDate>Sat, 28 Nov 2026 1{n % 10}:00:00 +0000</pubDate>'
        f'<description>{_long_html_body(n)}</description></item>'
        for n in range(1, count + 1))
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            '<title>Example News</title><link>https://news.example.org</link>'
            '<description>Example News is about everything.</description>'
            f'{items}</channel></rss>')


def _content_block(text):
    return text.split('Content:\n', 1)[1]


def test_a_feed_reaches_the_agent_as_many_real_stories_with_their_links(served):
    served['body'] = _rss(30)
    out = wc.crawl_url_for_agent(FEED_URL)
    seen = _content_block(out)[:3000]   # what the model kept of it (measured)
    for n in range(1, 11):
        assert f'Story number {n} headline' in seen, n
        assert f'https://news.example.org/story-{n}' in seen, n


def test_a_feed_item_reads_as_plain_words_not_its_html(served):
    served['body'] = _rss(3)
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    assert '<div' not in content and 'product-block' not in content
    assert 'Long story body' in content


def test_the_feed_is_named_with_its_item_count(served):
    served['body'] = _rss(5)
    out = wc.crawl_url_for_agent(FEED_URL)
    assert 'Example News' in out and '5 items' in out


def test_a_json_feed_reaches_the_agent_as_its_items(served):
    served['body'] = json.dumps({
        'version': 'https://jsonfeed.org/version/1.1', 'title': 'JSON News',
        'items': [{'id': str(n), 'title': f'Json story {n}',
                   'url': f'https://json.example.org/{n}',
                   'content_text': 'A short summary.'} for n in range(1, 6)]})
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    for n in range(1, 6):
        assert f'Json story {n}' in content and f'https://json.example.org/{n}' in content


def test_an_ordinary_page_is_read_as_before(served):
    served['body'] = ('<html><body><h1>Plain article</h1><p>'
                      + 'A sentence of the article. ' * 10 + '</p></body></html>')
    result = wc._fallback_fetch('https://www.example.org/a', 30, wc._ProgressLog())
    assert result['success'] and 'Plain article' in result['markdown']
    assert 'items' not in result['markdown'].split('Plain article')[0]


def test_without_feedparser_a_feed_is_still_read_as_its_items(served, monkeypatch):
    """The installed desktop and central carry no feedparser (measured
    28 Nov 2026); feed_import's own standard-library reader reads it."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    served['body'] = _rss(12)
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    assert 'Example News (12 items)' in content
    for n in range(1, 13):
        assert f'https://news.example.org/story-{n}' in content


def test_an_atom_feed_reaches_the_agent_as_its_items(served, monkeypatch):
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    entries = ''.join(
        f'<entry><title>Atom story {n}</title>'
        f'<link rel="alternate" href="https://atom.example.org/{n}"/>'
        f'<id>tag:atom.example.org,{n}</id><updated>2026-11-28T1{n}:00:00Z</updated>'
        f'<summary type="html">&lt;p&gt;Atom summary {n}.&lt;/p&gt;</summary></entry>'
        for n in range(1, 4))
    served['body'] = ('<?xml version="1.0" encoding="utf-8"?>'
                      '<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom News</title>'
                      '<link rel="self" href="https://atom.example.org/feed"/>'
                      f'<link rel="alternate" href="https://atom.example.org/"/>{entries}</feed>')
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    assert 'Atom News (3 items)' in content
    for n in range(1, 4):
        assert f'Atom story {n}' in content and f'https://atom.example.org/{n}' in content
        assert f'Atom summary {n}.' in content


def test_a_feed_that_declares_entities_is_not_read_as_a_feed(served, monkeypatch):
    """A feed never needs its own entities; they are how XML bombs are built."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    served['body'] = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">]>'
                      '<rss version="2.0"><channel><title>Bomb</title>'
                      '<item><title>&a;&a;&a;</title><link>https://x.example/1</link></item>'
                      '</channel></rss>')
    assert feed_import.read_feed(served['body'], FEED_URL) is None
    result = wc._fallback_fetch(FEED_URL, 30, wc._ProgressLog())
    assert 'items)' not in (result.get('markdown') or '')


def test_a_json_api_reply_is_read_as_a_page_not_a_feed(served):
    """rv_feed1 on 9b8939bc6: any JSON with an "items" list was taken for a
    JSON Feed, and a search API's links and snippets were dropped.  Only a
    jsonfeed.org document is a feed."""
    served['body'] = json.dumps({'kind': 'customsearch#search', 'items': [
        {'title': 'Result one title', 'link': 'https://one.example.org/a',
         'snippet': 'The first snippet says something useful.'},
        {'title': 'Result two title', 'link': 'https://two.example.org/b',
         'snippet': 'The second snippet says more of it.'}]})
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    assert 'https://one.example.org/a' in content and 'first snippet' in content
    assert 'items)' not in content


def test_an_rss_1_0_rdf_feed_reaches_the_agent_as_its_items(served, monkeypatch):
    """rv_feed1 on 9b8939bc6: an <rdf:RDF> feed (nature.com) was read as page
    text because the feed check did not know it."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    items = ''.join(
        f'<item rdf:about="https://rdf.example.org/{n}"><title>RDF story {n}</title>'
        f'<link>https://rdf.example.org/{n}</link><description>Summary {n}.</description>'
        f'<dc:date>2026-11-28T10:00:00Z</dc:date></item>' for n in range(1, 4))
    served['body'] = ('<?xml version="1.0"?><rdf:RDF '
                      'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
                      'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                      '<channel rdf:about="https://rdf.example.org/"><title>RDF News</title>'
                      f'<link>https://rdf.example.org/</link></channel>{items}</rdf:RDF>')
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    assert 'RDF News (3 items)' in content
    for n in range(1, 4):
        assert f'RDF story {n}' in content and f'https://rdf.example.org/{n}' in content


def test_a_feed_cut_at_the_size_limit_keeps_the_items_read_before_the_cut(
        served, monkeypatch):
    """rv_feed1 on 9b8939bc6: a feed over the read limit was cut mid-item,
    failed to parse, and went back to page text (2 stories in 3,000
    characters).  The items finished before the cut are read, and the
    agent is told the feed was longer."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    import core.constants
    body = _rss(30)
    monkeypatch.setattr(core.constants, 'MAX_PAYLOAD_BYTES', len(body) // 2)
    served['body'] = body
    out = wc.crawl_url_for_agent(FEED_URL)
    content = _content_block(out)
    assert 'Story number 1 headline' in content and 'https://news.example.org/story-1' in content
    assert 'cut' in content.split('\n', 1)[0]
    assert '&lt;' not in content and '<p>' not in content


def test_titles_read_as_plain_words_and_the_permalink_guid_is_the_link(served, monkeypatch):
    """rv_feed1 nits: markup in a title reached the agent, an item whose only
    link is its permalink guid showed none, and the short description won
    over content:encoded when it came first."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    served['body'] = (
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>T</title>'
        '<item><title><![CDATA[Tom &amp; Jerry <b>bold</b>]]></title>'
        '<guid isPermaLink="true">https://guid.example.org/1</guid>'
        '<description>Short.</description>'
        '<content:encoded><![CDATA[<p>The full body with <img src="https://img.example.org/1.png"> a picture.</p>]]></content:encoded>'
        '</item></channel></rss>')
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    assert 'Tom & Jerry bold' in content and '<b>' not in content
    assert 'https://guid.example.org/1' in content
    assert 'The full body' in content
    metadata, items = feed_import.read_feed(served['body'], FEED_URL)
    assert items[0].media_urls == ['https://img.example.org/1.png']


def _rss_with_a_bare_ampersand(count, broken_at):
    """_rss with a bare '&' in item ``broken_at``'s title, the commonest
    fault in real feeds: feedparser reads past it, the standard library's
    XML reader stops there."""
    return _rss(count).replace(f'<title>Story number {broken_at} headline</title>',
                               f'<title>Story number {broken_at} Q&A</title>')


class _FeedResp:
    """What pooled_get returns to FeedImporter.fetch_feed, for one body."""
    status_code = 200
    headers = {}

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_a_feed_with_a_break_in_its_xml_says_it_was_read_only_in_part(served, monkeypatch):
    """rv_feed1 on 06d464676: the standard-library reader keeps the items
    before ANY break in the XML, not only before the size cut.  A 30-item
    feed with a bare '&' in item 10 reached the agent as "Feed: T (9 items)",
    with no hint that 21 more exist.  The agent is told the read stopped,
    and where."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    served['body'] = _rss_with_a_bare_ampersand(6, 3)
    first = _content_block(wc.crawl_url_for_agent(FEED_URL)).split('\n', 1)[0]
    assert first.startswith('Feed: Example News (2 items'), first
    assert 'read only in part' in first and 'line ' in first, first


def test_a_subscription_read_in_part_says_so_and_a_whole_read_clears_it(monkeypatch):
    """rv_feed1 on 06d464676: on a node without feedparser a subscription to
    such a feed imported 9 of 30 items and reported 'active' with nothing
    said.  The subscription carries why it was read in part; a later whole
    read drops that note."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    bodies = [_rss_with_a_bare_ampersand(6, 3)]
    monkeypatch.setattr(feed_import, 'pooled_get', lambda *a, **k: _FeedResp(bodies[0]))
    svc = feed_import.FeedSubscriptionService(None)
    sub = svc.subscribe('u1', FEED_URL)
    assert sub['status'] == 'active' and sub['item_count'] == 2, sub
    assert 'were not read' in sub['partial'], sub
    bodies[0] = _rss(6)
    assert len(svc.check_feed(sub)) == 6
    assert 'partial' not in sub, sub


def test_the_news_tool_tells_the_agent_a_feed_was_read_only_in_part(monkeypatch):
    """The news agents' own feed tool (fetch_news_feeds) says so too, beside
    the items it could read."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    monkeypatch.setattr(feed_import, 'pooled_get',
                        lambda *a, **k: _FeedResp(_rss_with_a_bare_ampersand(6, 3)))
    from integrations.agent_engine import news_tools
    captured = {}

    class _Agent:
        def register_for_llm(self, name=None, description=None):
            def deco(fn):
                captured[name] = fn
                return fn
            return deco

        def register_for_execution(self, name=None):
            return lambda fn: fn

    news_tools.register_news_tools(_Agent(), _Agent(), '0')
    out = json.loads(captured['fetch_news_feeds'](FEED_URL))
    assert out['total'] == 2 and not out['used_default_feeds'], out
    notes = [e for e in out['errors'] if e.get('url') == FEED_URL]
    assert notes and 'were not read' in notes[0].get('partial', ''), out


def test_titles_keep_words_that_only_look_like_tags(served, monkeypatch):
    """rv_feed1 on 06d464676: every title was read as markup, so a
    programming feed's "std::vector<int>" lost "<int>" and "Use the <div> tag"
    lost "<div>"; and inline tags split a word ("C 2 H 4", "Hel lo").  A title
    is read as markup only when it looks like HTML (a closing tag or an
    entity reference, feedparser's own test), and its words are kept
    together the way the crawler keeps a page's (_keep_words_apart)."""
    monkeypatch.setattr(feed_import, 'FEEDPARSER_AVAILABLE', False)
    titles = ['C++: std::vector&lt;int&gt; and friends',
              'Use the &lt;div&gt; tag in React',
              'Ethylene C&lt;sub&gt;2&lt;/sub&gt;H&lt;sub&gt;4&lt;/sub&gt; made cheaply',
              '&lt;b&gt;Hel&lt;/b&gt;&lt;i&gt;lo&lt;/i&gt; world']
    items = ''.join(f'<item><title>{t}</title><link>https://t.example.org/{n}</link></item>'
                    for n, t in enumerate(titles, 1))
    served['body'] = ('<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
                      f'{items}</channel></rss>')
    content = _content_block(wc.crawl_url_for_agent(FEED_URL))
    assert 'C++: std::vector<int> and friends' in content, content
    assert 'Use the <div> tag in React' in content, content
    assert 'Ethylene C2H4 made cheaply' in content, content
    assert 'Hello world' in content, content


def test_fetch_feed_parses_through_the_same_reader(monkeypatch):
    """feed_import.FeedImporter.fetch_feed and the crawler read a feed body
    with one method (FeedImporter.parse)."""
    class _Resp:
        status_code = 200
        text = _rss(4)
        headers = {}

        def raise_for_status(self):
            return None
    monkeypatch.setattr(feed_import, 'pooled_get', lambda *a, **k: _Resp())
    seen = []
    real = feed_import.FeedImporter.parse

    def spy(self, content, url):
        seen.append(url)
        return real(self, content, url)
    monkeypatch.setattr(feed_import.FeedImporter, 'parse', spy)
    metadata, items, modified = feed_import.FeedImporter().fetch_feed(FEED_URL)
    assert seen == [FEED_URL] and modified
    assert [i.title for i in items][:2] == ['Story number 1 headline',
                                           'Story number 2 headline']
