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
  Anything that is not a feed is read as before.  A node without feedparser
  reads an RSS/Atom feed as page text, as before.

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
