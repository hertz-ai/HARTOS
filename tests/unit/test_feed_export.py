"""
test_feed_export.py - Tests for integrations/social/feed_export.py

feed_export.py turns social Posts into anonymous public RSS / Atom / JSON
feeds (the /users/<id>/feed.rss and /communities/<id>/feed.rss endpoints,
plus the global/trending/agent feeds).  Because those endpoints are
UNAUTHENTICATED, the module is a security boundary: a post that is
soft-deleted, moderator-hidden, or set to a non-public privacy level must
never appear in the emitted feed.

FT  (formatting): RSS/Atom/JSON structure, title truncation, author
    fallback, media/tag rendering, custom base URL.
NFT (robustness/security):
    - visibility leak: hidden / private / friends / deleted posts must be
      excluded from the anonymous user & community feeds (regression: the
      queries filtered on a phantom `deleted_at` column only).
    - dead-import regression: the main feeds must not be silently emptied
      by a broken `FeedEngine` import.
    - degrade: DB errors return an empty feed, never raise.
    - injection: user content / media URLs must be escaped, not emitted raw.
"""
import os
import sys
import json
from xml.etree import ElementTree as ET

import pytest

# Must be set BEFORE integrations.social.models is imported (it resolves the
# engine at import time). Keeps every DB test on a private in-memory SQLite.
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# feed_export itself is import-light (stdlib only). Import it unconditionally.
from integrations.social import feed_export as FE  # noqa: E402

DC_NS = '{http://purl.org/dc/elements/1.1/}'
ATOM_NS = '{http://www.w3.org/2005/Atom}'


def _parse(xml_str):
    """RSS/Atom serialize with an encoding declaration; ElementTree refuses
    to parse those as `str`, so hand it bytes."""
    return ET.fromstring(xml_str.encode('utf-8'))


def _gen(posts=None, base_url='https://test.example'):
    """A FeedGenerator whose _get_posts is stubbed to return `posts`.

    _get_posts is the single seam between the DB layer and the formatting
    layer; stubbing it lets the formatting tests run without a database
    while still exercising the REAL generate_rss/atom/json code.
    """
    g = FE.FeedGenerator(None, base_url=base_url)
    g._get_posts = lambda *a, **k: list(posts or [])
    return g


# ============================================================
# FT — RSS 2.0 formatting
# ============================================================

class TestGenerateRSS:
    def test_empty_feed_is_valid_xml_with_no_items(self):
        g = _gen([])
        root = _parse(g.generate_rss())
        assert root.tag == 'rss'
        channel = root.find('channel')
        assert channel is not None
        assert channel.find('title').text  # channel metadata present
        assert channel.findall('item') == []

    def test_single_post_core_fields(self):
        post = {
            'id': 42,
            'content': 'Hello world',
            'author': {'display_name': 'Bob', 'id': 7},
            'created_at': '2026-01-01T12:00:00Z',
            'tags': ['alpha', 'beta'],
            'community': {'name': 'science'},
        }
        root = _parse(_gen([post]).generate_rss())
        item = root.find('channel').find('item')
        assert item is not None
        assert item.find('title').text == 'Hello world'
        assert item.find('link').text == 'https://test.example/social/post/42'
        assert item.find('guid').text == 'https://test.example/social/post/42'
        assert item.find(DC_NS + 'creator').text == 'Bob'
        assert item.find('pubDate') is not None
        cats = [c.text for c in item.findall('category')]
        assert 'alpha' in cats and 'beta' in cats
        assert 's/science' in cats

    def test_title_truncated_to_100_chars(self):
        long = 'x' * 150
        root = _parse(_gen([{'id': 1, 'content': long}]).generate_rss())
        title = root.find('channel').find('item').find('title').text
        assert title.endswith('...')
        assert len(title) == 103  # 100 chars + '...'

    def test_title_uses_first_line_only(self):
        root = _parse(_gen([{'id': 1, 'content': 'headline\nbody line'}]).generate_rss())
        title = root.find('channel').find('item').find('title').text
        assert title == 'headline'

    def test_author_falls_back_to_username_then_anonymous(self):
        only_username = {'id': 1, 'content': 'a', 'author': {'username': 'neo'}}
        no_author = {'id': 2, 'content': 'b', 'author': {}}
        root = _parse(_gen([only_username, no_author]).generate_rss())
        items = root.find('channel').findall('item')
        assert items[0].find(DC_NS + 'creator').text == 'neo'
        assert items[1].find(DC_NS + 'creator').text == 'Anonymous'

    def test_community_as_plain_string(self):
        root = _parse(_gen([{'id': 1, 'content': 'a', 'community': 'plainname'}]).generate_rss())
        cats = [c.text for c in root.find('channel').find('item').findall('category')]
        assert 's/plainname' in cats

    def test_custom_title_overrides_default(self):
        root = _parse(_gen([]).generate_rss(title='My Custom Feed'))
        assert root.find('channel').find('title').text == 'My Custom Feed'

    def test_malformed_created_at_does_not_crash(self):
        # Bad ISO string -> falls back to now(); still produces a pubDate.
        root = _parse(_gen([{'id': 1, 'content': 'a', 'created_at': 'not-a-date'}]).generate_rss())
        assert root.find('channel').find('item').find('pubDate') is not None

    def test_missing_content_defaults_to_untitled(self):
        root = _parse(_gen([{'id': 1}]).generate_rss())
        assert root.find('channel').find('item').find('title').text == 'Untitled'

    def test_user_content_is_escaped_not_injected(self):
        # A script tag in post content must be serialized as escaped text,
        # never as a live child element in the feed body.
        raw = _gen([{'id': 1, 'content': '<script>alert(1)</script>'}]).generate_rss()
        assert '<script>' not in raw
        assert '&lt;script&gt;' in raw


# ============================================================
# FT — Atom 1.0 formatting
# ============================================================

class TestGenerateAtom:
    def test_empty_feed_valid(self):
        root = _parse(_gen([]).generate_atom())
        assert root.tag == ATOM_NS + 'feed'
        assert root.findall(ATOM_NS + 'entry') == []

    def test_entry_fields(self):
        post = {
            'id': 9,
            'content': 'Atomic content',
            'author': {'display_name': 'Ada', 'id': 3},
            'created_at': '2026-02-02T00:00:00+00:00',
            'updated_at': '2026-02-03T00:00:00+00:00',
            'tags': ['t1'],
        }
        root = _parse(_gen([post]).generate_atom())
        entry = root.find(ATOM_NS + 'entry')
        assert entry.find(ATOM_NS + 'title').text == 'Atomic content'
        assert entry.find(ATOM_NS + 'id').text == 'https://test.example/social/post/9'
        assert entry.find(ATOM_NS + 'content').text == 'Atomic content'
        author = entry.find(ATOM_NS + 'author')
        assert author.find(ATOM_NS + 'name').text == 'Ada'
        assert entry.find(ATOM_NS + 'published').text == '2026-02-02T00:00:00+00:00'
        assert entry.find(ATOM_NS + 'updated').text == '2026-02-03T00:00:00+00:00'

    def test_created_at_only_does_not_crash(self):
        # Exercises the created_at-without-updated_at branch; must produce a
        # parseable feed with a published date and not raise.
        root = _parse(_gen([{'id': 1, 'content': 'a', 'created_at': '2026-01-01T00:00:00+00:00'}]).generate_atom())
        entry = root.find(ATOM_NS + 'entry')
        assert entry.find(ATOM_NS + 'published') is not None

    def test_summary_truncated_at_300(self):
        long = 'y' * 400
        root = _parse(_gen([{'id': 1, 'content': long}]).generate_atom())
        summary = root.find(ATOM_NS + 'entry').find(ATOM_NS + 'summary').text
        assert summary.endswith('...')
        assert len(summary) == 303


# ============================================================
# FT — JSON Feed 1.1 formatting
# ============================================================

class TestGenerateJSONFeed:
    def test_empty_feed_shape(self):
        feed = json.loads(_gen([]).generate_json_feed())
        assert feed['version'] == 'https://jsonfeed.org/version/1.1'
        assert feed['items'] == []
        assert feed['home_page_url'] == 'https://test.example'

    def test_item_fields(self):
        post = {
            'id': 55,
            'content': 'json body',
            'author': {'display_name': 'Grace', 'id': 8},
            'created_at': '2026-03-03T00:00:00+00:00',
            'tags': ['j'],
        }
        feed = json.loads(_gen([post]).generate_json_feed())
        item = feed['items'][0]
        assert item['id'] == '55'
        assert item['url'] == 'https://test.example/social/post/55'
        assert item['content_text'] == 'json body'
        assert item['title'] == 'json body'
        assert item['authors'][0]['name'] == 'Grace'
        assert item['authors'][0]['url'] == 'https://test.example/social/u/8'
        assert item['tags'] == ['j']
        assert item['date_published'] == '2026-03-03T00:00:00+00:00'

    def test_author_without_id_has_null_url(self):
        feed = json.loads(_gen([{'id': 1, 'content': 'a', 'author': {'username': 'x'}}]).generate_json_feed())
        assert feed['items'][0]['authors'][0]['url'] is None

    def test_attachment_mime_detection(self):
        post = {
            'id': 1, 'content': 'a',
            'media_urls': [
                'http://c/img.png', 'http://c/anim.gif',
                'http://c/pic.webp', 'http://c/clip.mp4', 'http://c/photo.jpg',
            ],
        }
        feed = json.loads(_gen([post]).generate_json_feed())
        mimes = [a['mime_type'] for a in feed['items'][0]['attachments']]
        assert mimes == ['image/png', 'image/gif', 'image/webp', 'video/mp4', 'image/jpeg']


# ============================================================
# FT — helper units
# ============================================================

class TestHelpers:
    def test_post_and_author_urls(self):
        g = FE.FeedGenerator(None, base_url='https://h.test')
        assert g._get_post_url({'id': 12}) == 'https://h.test/social/post/12'
        assert g._get_author_url({'id': 3}) == 'https://h.test/social/u/3'

    def test_format_content_renders_image_vs_link(self):
        g = FE.FeedGenerator(None)
        out = g._format_post_content({'content': 'x', 'media_urls': ['http://c/a.png', 'http://c/doc.pdf']})
        assert '<img src="http://c/a.png" />' in out
        assert '<a href="http://c/doc.pdf">' in out

    def test_format_content_escapes_media_url(self):
        # A media URL crafted to break out of the src attribute must be
        # HTML-escaped (the quote becomes &quot;), not emitted verbatim.
        g = FE.FeedGenerator(None)
        evil = 'http://c/x.png" onerror="alert(1)'
        out = g._format_post_content({'content': '', 'media_urls': [evil]})
        assert '&quot;' in out
        assert '" onerror="alert(1)' not in out

    def test_format_content_no_media(self):
        g = FE.FeedGenerator(None)
        assert g._format_post_content({'content': 'plain'}) == 'plain'


# ============================================================
# NFT — _get_posts degrade path (no DB needed: mock the boundary)
# ============================================================

class TestGetPostsDegrade:
    def test_db_error_returns_empty_list_not_raise(self):
        from unittest.mock import MagicMock
        db = MagicMock()
        db.query.side_effect = RuntimeError('db down')
        g = FE.FeedGenerator(db)
        # Community branch touches db.query directly; error must be swallowed.
        assert g._get_posts(feed_type='global', community_id='c1') == []


# ============================================================
# NFT — visibility + dead-import regressions (real in-memory DB)
# ============================================================

# Import the ORM lazily and skip cleanly if the (heavy) social stack can't
# load in this environment, rather than leaving the file red.
try:
    from integrations.social.models import (
        Base, get_engine, get_db, User, Post, Community, _uuid,
    )
    _DB_OK = True
    _DB_SKIP = ''
except Exception as e:  # pragma: no cover - environment guard
    _DB_OK = False
    _DB_SKIP = f'social ORM unavailable: {e}'

pytestmark_db = pytest.mark.skipif(not _DB_OK, reason=_DB_SKIP)


@pytest.fixture()
def db():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = get_db()
    yield session
    session.rollback()
    session.close()
    Base.metadata.drop_all(engine)


def _mk_user(db, username, user_type='human'):
    u = User(username=username, display_name=username.title(), user_type=user_type)
    db.add(u)
    db.commit()
    return u


def _mk_post(db, user, content, community_id=None, **flags):
    p = Post(id=_uuid(), author_id=user.id, title=content[:20],
             content=content, community_id=community_id, **flags)
    db.add(p)
    db.commit()
    return p


@pytestmark_db
class TestMainFeedsPopulate:
    """Regression: a dead `from .feed_engine import FeedEngine` import made
    _get_posts raise ImportError and silently return [] for every non-
    community feed, so the global/trending/agent RSS feeds were always empty."""

    def test_global_feed_is_not_silently_empty(self, db):
        u = _mk_user(db, 'globaluser')
        _mk_post(db, u, 'VISIBLE GLOBAL POST')
        posts = FE.FeedGenerator(db)._get_posts(feed_type='global')
        assert len(posts) == 1
        assert posts[0]['content'] == 'VISIBLE GLOBAL POST'

    def test_generate_rss_global_contains_post(self, db):
        u = _mk_user(db, 'rssuser')
        _mk_post(db, u, 'RSS BODY TEXT')
        root = _parse(FE.FeedGenerator(db).generate_rss(feed_type='global'))
        items = root.find('channel').findall('item')
        assert len(items) == 1

    def test_agent_feed_returns_agent_posts(self, db):
        agent = _mk_user(db, 'agent007', user_type='agent')
        _mk_post(db, agent, 'AGENT AUTHORED')
        posts = FE.FeedGenerator(db)._get_posts(feed_type='agents')
        assert any(p['content'] == 'AGENT AUTHORED' for p in posts)


@pytestmark_db
class TestUserFeedVisibility:
    """Security: /users/<id>/feed.rss is anonymous. It must expose the
    user's PUBLIC posts and nothing else."""

    def test_public_post_is_exposed(self, db):
        u = _mk_user(db, 'alice')
        _mk_post(db, u, 'MY PUBLIC POST')
        rss = FE.get_user_feed_rss(db, u.id)
        assert 'MY PUBLIC POST' in rss

    def test_hidden_post_is_not_leaked(self, db):
        u = _mk_user(db, 'alice')
        _mk_post(db, u, 'PUBLIC ONE')
        _mk_post(db, u, 'MODERATOR HIDDEN', is_hidden=True)
        rss = FE.get_user_feed_rss(db, u.id)
        assert 'PUBLIC ONE' in rss
        assert 'MODERATOR HIDDEN' not in rss

    def test_private_and_friends_posts_are_not_leaked(self, db):
        u = _mk_user(db, 'alice')
        _mk_post(db, u, 'PUBLIC ONE')
        _mk_post(db, u, 'SECRET PRIVATE', privacy='private')
        _mk_post(db, u, 'FRIENDS ONLY', privacy='friends')
        rss = FE.get_user_feed_rss(db, u.id)
        assert 'PUBLIC ONE' in rss
        assert 'SECRET PRIVATE' not in rss
        assert 'FRIENDS ONLY' not in rss

    def test_deleted_post_is_not_leaked(self, db):
        u = _mk_user(db, 'alice')
        _mk_post(db, u, 'PUBLIC ONE')
        _mk_post(db, u, 'SOFT DELETED', is_deleted=True)
        rss = FE.get_user_feed_rss(db, u.id)
        assert 'PUBLIC ONE' in rss
        assert 'SOFT DELETED' not in rss

    def test_unknown_user_returns_empty_feed(self, db):
        rss = FE.get_user_feed_rss(db, 'no-such-user')
        root = _parse(rss)
        assert root.find('channel').findall('item') == []


@pytestmark_db
class TestCommunityFeedVisibility:
    """Security: /communities/<id>/feed.rss is anonymous. Hidden/private
    posts must not appear; a private community exposes nothing anonymously."""

    def _mk_community(self, db, user, name, is_private=False):
        c = Community(id=_uuid(), name=name, creator_id=user.id, is_private=is_private)
        db.add(c)
        db.commit()
        return c

    def test_public_community_post_exposed_hidden_excluded(self, db):
        u = _mk_user(db, 'mod')
        c = self._mk_community(db, u, 'science')
        _mk_post(db, u, 'COMMUNITY PUBLIC', community_id=c.id)
        _mk_post(db, u, 'COMMUNITY HIDDEN', community_id=c.id, is_hidden=True)
        _mk_post(db, u, 'COMMUNITY PRIVATE', community_id=c.id, privacy='private')
        rss = FE.get_community_feed_rss(db, c.id)
        assert 'COMMUNITY PUBLIC' in rss
        assert 'COMMUNITY HIDDEN' not in rss
        assert 'COMMUNITY PRIVATE' not in rss

    def test_private_community_is_empty_anonymously(self, db):
        u = _mk_user(db, 'mod')
        c = self._mk_community(db, u, 'secretclub', is_private=True)
        _mk_post(db, u, 'INSIDE PRIVATE COMMUNITY', community_id=c.id)
        rss = FE.get_community_feed_rss(db, c.id)
        assert 'INSIDE PRIVATE COMMUNITY' not in rss

    def test_unknown_community_returns_empty_feed(self, db):
        root = _parse(FE.get_community_feed_rss(db, 'no-such-community'))
        assert root.find('channel').findall('item') == []
