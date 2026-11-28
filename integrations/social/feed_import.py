"""
feed_import.py - RSS and Atom feed consumption for HevolveSocial

Imports external feeds and creates social posts from them.
Supports RSS 2.0, Atom 1.0, and JSON Feed formats.

Usage:
    from integrations.social.feed_import import FeedImporter, FeedSubscriptionService

    importer = FeedImporter(db)
    items = importer.fetch_feed('https://example.com/feed.xml')
    importer.import_items(items, user_id=123)
"""

import logging
import hashlib
import requests
from core.http_pool import pooled_get
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json
import re

logger = logging.getLogger('hevolve_social')

# Try to import feedparser (optional dependency)
try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False
    logger.warning("feedparser not available - using basic XML parsing")


class FeedFormat(Enum):
    """Supported feed formats."""
    RSS = 'rss'
    ATOM = 'atom'
    JSON = 'json'
    UNKNOWN = 'unknown'


@dataclass
class FeedItem:
    """Represents a single feed item."""
    id: str
    title: str
    content: str
    link: str
    author: str = ''
    published: Optional[datetime] = None
    updated: Optional[datetime] = None
    categories: List[str] = field(default_factory=list)
    media_urls: List[str] = field(default_factory=list)
    source_feed: str = ''
    content_hash: str = ''

    def __post_init__(self):
        """Calculate content hash for deduplication."""
        if not self.content_hash:
            hash_content = f"{self.link}:{self.title}:{self.content[:500]}"
            self.content_hash = hashlib.sha256(hash_content.encode()).hexdigest()[:32]


@dataclass
class FeedMetadata:
    """Metadata about a feed."""
    url: str
    title: str = ''
    description: str = ''
    link: str = ''
    format: FeedFormat = FeedFormat.UNKNOWN
    last_updated: Optional[datetime] = None
    etag: str = ''
    last_modified: str = ''
    # Why the read stopped before the feed's end, one plain sentence for
    # whoever is shown the items (a break in its XML); empty when the whole
    # body was read.  Only the standard library's reader (_parse_xml_feed)
    # stops early: feedparser reads past such a break.
    stopped_early: str = ''


class FeedImporter:
    """Fetches and parses external RSS/Atom/JSON feeds."""

    def __init__(self, db_session=None, timeout: int = 30):
        """
        Initialize feed importer.

        Args:
            db_session: SQLAlchemy database session (optional)
            timeout: Request timeout in seconds
        """
        self.db = db_session
        self.timeout = timeout
        self.user_agent = 'HARTSocial/1.0 (+https://hevolve.ai)'

    def _detect_format(self, content: str) -> FeedFormat:
        """Detect feed format from content."""
        content_lower = content.strip().lower()

        if content_lower.startswith('{'):
            try:
                data = json.loads(content)
                if 'version' in data and 'jsonfeed' in str(data.get('version', '')).lower():
                    return FeedFormat.JSON
                if 'items' in data or 'entries' in data:
                    return FeedFormat.JSON
            except Exception:
                pass

        if '<feed' in content_lower and 'xmlns' in content_lower:
            return FeedFormat.ATOM

        if '<rss' in content_lower or '<channel>' in content_lower:
            return FeedFormat.RSS

        # RSS 1.0 (RDF): <rdf:RDF xmlns="http://purl.org/rss/1.0/">, its
        # channel written <channel rdf:about="...">.  rv_feed1 on 9b8939bc6:
        # nature.com's feed read as UNKNOWN, so the crawler never read it as
        # a feed.
        if '<rdf:rdf' in content_lower and 'purl.org/rss/1.0' in content_lower:
            return FeedFormat.RSS

        return FeedFormat.UNKNOWN

    def _extract_images_from_content(self, content: str) -> List[str]:
        """Extract image URLs from HTML content."""
        images = []
        # Find img tags
        img_pattern = r'<img[^>]+src=["\']([^"\']+)["\']'
        images.extend(re.findall(img_pattern, content, re.IGNORECASE))

        # Find media:content or enclosure
        media_pattern = r'<media:content[^>]+url=["\']([^"\']+)["\']'
        images.extend(re.findall(media_pattern, content, re.IGNORECASE))

        enclosure_pattern = r'<enclosure[^>]+url=["\']([^"\']+)["\']'
        images.extend(re.findall(enclosure_pattern, content, re.IGNORECASE))

        return list(set(images))

    def _parse_datetime(self, date_str: str) -> Optional[datetime]:
        """Parse various datetime formats."""
        if not date_str:
            return None

        formats = [
            '%Y-%m-%dT%H:%M:%S%z',
            '%Y-%m-%dT%H:%M:%SZ',
            '%Y-%m-%dT%H:%M:%S.%f%z',
            '%Y-%m-%dT%H:%M:%S.%fZ',
            '%a, %d %b %Y %H:%M:%S %z',
            '%a, %d %b %Y %H:%M:%S %Z',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d',
        ]

        date_str = date_str.strip()

        # Handle common timezone abbreviations
        date_str = date_str.replace('GMT', '+0000').replace('UTC', '+0000')

        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

        # Try feedparser's date parsing if available
        if FEEDPARSER_AVAILABLE:
            try:
                parsed = feedparser._parse_date(date_str)
                if parsed:
                    return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass

        logger.warning(f"Could not parse date: {date_str}")
        return None

    def _parse_with_feedparser(self, content: str, url: str) -> Tuple[FeedMetadata, List[FeedItem]]:
        """Parse feed using feedparser library."""
        feed = feedparser.parse(content)

        # Extract metadata
        metadata = FeedMetadata(
            url=url,
            title=feed.feed.get('title', ''),
            description=feed.feed.get('description', feed.feed.get('subtitle', '')),
            link=feed.feed.get('link', ''),
            format=FeedFormat.RSS if feed.version.startswith('rss') else FeedFormat.ATOM,
            last_updated=self._parse_datetime(feed.feed.get('updated', feed.feed.get('published', '')))
        )

        # Extract items
        items = []
        for entry in feed.entries:
            # Get content
            content = ''
            if entry.get('content'):
                content = entry.content[0].get('value', '')
            elif entry.get('summary'):
                content = entry.summary
            elif entry.get('description'):
                content = entry.description

            # Extract images
            media_urls = self._extract_images_from_content(content)
            if entry.get('media_content'):
                for media in entry.media_content:
                    if media.get('url'):
                        media_urls.append(media['url'])
            if entry.get('enclosures'):
                for enc in entry.enclosures:
                    if enc.get('href'):
                        media_urls.append(enc['href'])

            # Get author
            author = ''
            if entry.get('author'):
                author = entry.author
            elif entry.get('authors'):
                author = entry.authors[0].get('name', '') if entry.authors else ''

            # Categories
            categories = [tag.get('term', tag) if isinstance(tag, dict) else str(tag)
                         for tag in entry.get('tags', [])]

            item = FeedItem(
                id=entry.get('id', entry.get('link', '')),
                title=entry.get('title', ''),
                content=content,
                link=entry.get('link', ''),
                author=author,
                published=self._parse_datetime(entry.get('published', '')),
                updated=self._parse_datetime(entry.get('updated', '')),
                categories=categories,
                media_urls=list(set(media_urls)),
                source_feed=url
            )
            items.append(item)

        return metadata, items

    def _parse_json_feed(self, content: str, url: str) -> Tuple[FeedMetadata, List[FeedItem]]:
        """Parse JSON Feed format."""
        data = json.loads(content)

        metadata = FeedMetadata(
            url=url,
            title=data.get('title', ''),
            description=data.get('description', ''),
            link=data.get('home_page_url', ''),
            format=FeedFormat.JSON
        )

        items = []
        for entry in data.get('items', []):
            # Get content
            content = entry.get('content_html', entry.get('content_text', ''))

            # Media
            media_urls = []
            for attachment in entry.get('attachments', []):
                if attachment.get('url'):
                    media_urls.append(attachment['url'])
            if entry.get('image'):
                media_urls.append(entry['image'])

            # Author
            author = ''
            authors = entry.get('authors', [entry.get('author')] if entry.get('author') else [])
            if authors and authors[0]:
                author = authors[0].get('name', '') if isinstance(authors[0], dict) else str(authors[0])

            item = FeedItem(
                id=entry.get('id', entry.get('url', '')),
                title=entry.get('title', ''),
                content=content,
                link=entry.get('url', ''),
                author=author,
                published=self._parse_datetime(entry.get('date_published', '')),
                updated=self._parse_datetime(entry.get('date_modified', '')),
                categories=entry.get('tags', []),
                media_urls=list(set(media_urls)),
                source_feed=url
            )
            items.append(item)

        return metadata, items

    def parse(self, content: str, url: str) -> Tuple[FeedMetadata, List[FeedItem]]:
        """Read a feed body already fetched from ``url``: JSON Feed by hand,
        RSS/Atom with feedparser, or with the standard library's XML reader
        on a node without feedparser (_parse_xml_feed).  The one reading of
        a feed body, for fetch_feed and for the web crawler (read_feed)."""
        if self._detect_format(content) == FeedFormat.JSON:
            return self._parse_json_feed(content, url)
        if not FEEDPARSER_AVAILABLE:
            return self._parse_xml_feed(content, url)
        return self._parse_with_feedparser(content, url)

    def _parse_xml_feed(self, content: str, url: str) -> Tuple[FeedMetadata, List[FeedItem]]:
        """RSS 2.0, RSS 1.0 (RDF) and Atom with xml.etree, for a node without
        feedparser: the "basic XML parsing" this module's import warning
        promised and nothing did.  The installed desktop and central carry
        no feedparser (measured 28 Nov 2026: the desktop's python-embed has
        none; central's image drops it, task #307), so every RSS read raised
        "feedparser not available" there.

        A document that declares its own entities is refused (ValueError):
        a feed never needs them, and they are how entity-expansion bombs are
        built.  xml.etree does not fetch external entities or DTDs.

        A body cut short (a reader's size limit, rv_feed1 on 9b8939bc6: a
        2 MiB feed went back to page text) keeps every item that ended
        before the cut; the one cut in half is left out.  A break in the
        XML (a bare '&', an undefined entity) ends the read the same way,
        and metadata.stopped_early says where, so a reader can tell the
        person the feed was read in part (rv_feed1 on 06d464676: 9 of 30
        items, said nowhere)."""
        import xml.etree.ElementTree as ET
        if '<!ENTITY' in content.upper():
            raise ValueError('the feed declares XML entities')

        def local(el):
            return el.tag.rsplit('}', 1)[-1] if isinstance(el.tag, str) else ''

        parser = ET.XMLPullParser(events=('start', 'end'))
        state = {'root': None, 'ended': set(), 'stopped': ''}

        def stopped(sentence, e):
            # The first stop is the one that matters: after a break the
            # close below fails too.
            if not state['stopped']:
                state['stopped'] = sentence
            logger.info(f"Feed {url}: {sentence} ({e})")

        def drain():
            try:
                for event, el in parser.read_events():
                    if event == 'start' and state['root'] is None:
                        state['root'] = el
                    elif event == 'end' and local(el) in ('item', 'entry'):
                        state['ended'].add(id(el))
            except ET.ParseError as e:
                line, column = getattr(e, 'position', None) or ('?', '?')
                stopped(f'its XML breaks at line {line}, column {column}, '
                        'so the items after that point were not read', e)

        parser.feed(content)
        drain()
        try:
            parser.close()
        except ET.ParseError as e:
            stopped('it ends before its XML does, so an item cut short there '
                    'was not read', e)
        drain()
        root = state['root']
        if root is None:
            raise ValueError('not an XML document')

        def child(el, *names):
            """The first child named ``names[0]``, else ``names[1]``, ...:
            the names in order of preference, not the document's order
            (rv_feed1: a short <description> won over content:encoded)."""
            for name in names:
                found = next((c for c in el if local(c) == name), None)
                if found is not None:
                    return found
            return None

        def text(el, *names):
            found = child(el, *names)
            return ''.join(found.itertext()).strip() if found is not None else ''

        def ended(el):
            return id(el) in state['ended']

        top = local(root)
        if top == 'feed':                      # Atom
            channel, fmt = root, FeedFormat.ATOM
            entries = [c for c in root if local(c) == 'entry' and ended(c)]
        else:                                  # RSS 2.0 (<rss><channel>) or RSS 1.0 (<rdf:RDF>)
            channel = child(root, 'channel') if top in ('rss', 'RDF') else None
            if channel is None:
                raise ValueError(f'not an RSS or Atom document (root <{top}>)')
            holder = channel if top == 'rss' else root
            entries = [c for c in holder if local(c) == 'item' and ended(c)]
            fmt = FeedFormat.RSS

        def link_of(el):
            if fmt == FeedFormat.RSS:
                link = text(el, 'link')
                guid = child(el, 'guid')
                # An item whose only link is its permalink guid (rv_feed1).
                if not link and guid is not None and guid.get('isPermaLink', 'true') != 'false':
                    candidate = ''.join(guid.itertext()).strip()
                    link = candidate if candidate.startswith(('http://', 'https://')) else ''
                return link
            links = [c for c in el if local(c) == 'link']
            best = next((c for c in links if c.get('rel', 'alternate') == 'alternate'),
                        links[0] if links else None)
            return (best.get('href') or '').strip() if best is not None else ''

        metadata = FeedMetadata(
            url=url, title=text(channel, 'title'),
            description=text(channel, 'description', 'subtitle'),
            link=link_of(channel), format=fmt,
            last_updated=self._parse_datetime(text(channel, 'lastBuildDate', 'updated', 'date')),
            stopped_early=state['stopped'])

        items = []
        for entry in entries:
            body = text(entry, 'encoded', 'content', 'description', 'summary')
            author_el = child(entry, 'author', 'creator')
            author = ''
            if author_el is not None:
                author = (text(author_el, 'name') or ''.join(author_el.itertext())).strip()
            categories = [(c.get('term') or ''.join(c.itertext())).strip()
                          for c in entry if local(c) == 'category']
            link = link_of(entry)
            items.append(FeedItem(
                id=text(entry, 'guid', 'id') or link,
                title=text(entry, 'title'),
                content=body,
                link=link,
                author=author,
                published=self._parse_datetime(text(entry, 'pubDate', 'published', 'date')),
                updated=self._parse_datetime(text(entry, 'updated')),
                categories=[c for c in categories if c],
                media_urls=self._extract_images_from_content(body),
                source_feed=url))
        return metadata, items

    def fetch_feed(self, url: str, etag: str = None,
                   last_modified: str = None) -> Tuple[FeedMetadata, List[FeedItem], bool]:
        """
        Fetch and parse a feed from URL.

        Args:
            url: Feed URL
            etag: Previous ETag for conditional requests
            last_modified: Previous Last-Modified for conditional requests

        Returns:
            Tuple of (metadata, items, was_modified)
        """
        headers = {'User-Agent': self.user_agent}
        if etag:
            headers['If-None-Match'] = etag
        if last_modified:
            headers['If-Modified-Since'] = last_modified

        try:
            response = pooled_get(url, headers=headers, timeout=self.timeout)

            # Handle 304 Not Modified
            if response.status_code == 304:
                return FeedMetadata(url=url), [], False

            response.raise_for_status()
            metadata, items = self.parse(response.text, url)
            if metadata.stopped_early:
                logger.warning(f"Feed {url} read in part ({len(items)} items): "
                               f"{metadata.stopped_early}")

            # Store conditional request headers
            metadata.etag = response.headers.get('ETag', '')
            metadata.last_modified = response.headers.get('Last-Modified', '')

            return metadata, items, True

        except requests.RequestException as e:
            logger.error(f"Error fetching feed {url}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error parsing feed {url}: {e}")
            raise

    def import_items(self, items: List[FeedItem], user_id: str,
                     community_id: int = None, auto_tag: bool = True) -> List[int]:
        """
        Import feed items as social posts.

        Args:
            items: List of FeedItem objects
            user_id: User ID to attribute posts to
            community_id: Optional community to post to
            auto_tag: Whether to auto-generate tags

        Returns:
            List of created post IDs
        """
        if not self.db:
            raise ValueError("Database session required for importing")

        try:
            from .models import Post, get_db, User, Community
            from .services import PostService
            from urllib.parse import urlparse

            created_ids = []

            for item in items:
                # Check for duplicates by content hash
                existing = self.db.query(Post).filter(
                    Post.content.contains(item.content_hash)
                ).first()

                if existing:
                    logger.debug(f"Skipping duplicate item: {item.title}")
                    continue

                # Build post BODY.  title is a first-class column on the
                # post, so don't duplicate it inside the body.  Keep the
                # feed-hash marker in the body so the dedup query above
                # (Post.content.contains(content_hash)) stays valid; we
                # also stamp source_channel/source_message_id below for the
                # canonical dedup columns.
                body = item.content or ''
                if item.link:
                    body += f"\n\n[Source]({item.link})"
                body += f"\n\n<!-- feed_hash:{item.content_hash} -->"

                # PostService.create takes the author User object (not an
                # id) — the old create_post(author_id=..., post_type=...,
                # tags=..., community_id=...) signature never existed and
                # raised AttributeError on every feed item.  Map onto the
                # real API (services.py:412).  Tags are dropped: create()
                # has no tags param and the post model is tagged via a
                # separate path — adding tag support here would be a new
                # concern, tracked separately.
                author = self.db.query(User).filter(User.id == user_id).first()
                if author is None:
                    logger.error(
                        f"feed_import: author user {user_id} not found; skipping item")
                    continue

                community_name = None
                if community_id:
                    _c = self.db.query(Community).filter(
                        Community.id == community_id).first()
                    community_name = _c.name if _c else None

                _domain = urlparse(item.source_feed).netloc if item.source_feed else ''

                try:
                    post = PostService.create(
                        self.db,
                        author,
                        (item.title or '(untitled)')[:300],
                        body,
                        content_type='link' if item.link else 'text',
                        community_name=community_name,
                        media_urls=(item.media_urls or [])[:5],
                        link_url=item.link or None,
                        source_channel=f"feed:{_domain}" if _domain else 'feed',
                        source_message_id=item.content_hash,
                    )
                    self.db.commit()
                    created_ids.append(post.id)
                    logger.info(f"Imported feed item as post {post.id}: {(item.title or '')[:50]}")
                except Exception as e:
                    self.db.rollback()
                    logger.error(f"Error creating post from feed item: {e}")

            return created_ids

        except ImportError as e:
            logger.error(f"Cannot import - missing models: {e}")
            raise


class FeedSubscriptionService:
    """Manages feed subscriptions for users."""

    def __init__(self, db_session):
        self.db = db_session
        self.importer = FeedImporter(db_session)

    def subscribe(self, user_id: str, feed_url: str,
                  community_id: int = None, auto_import: bool = True) -> Dict[str, Any]:
        """
        Subscribe a user to a feed.

        Args:
            user_id: User ID
            feed_url: Feed URL to subscribe to
            community_id: Optional community to post imported items to
            auto_import: Whether to automatically import new items

        Returns:
            Subscription details
        """
        try:
            # Validate feed
            metadata, items, _ = self.importer.fetch_feed(feed_url)

            # Store subscription (using a simple JSON approach for now)
            # In production, this would use a FeedSubscription model
            subscription = {
                'user_id': user_id,
                'feed_url': feed_url,
                'feed_title': metadata.title,
                'community_id': community_id,
                'auto_import': auto_import,
                'etag': metadata.etag,
                'last_modified': metadata.last_modified,
                'last_checked': datetime.now(timezone.utc).isoformat(),
                'item_count': len(items),
                'status': 'active'
            }
            if metadata.stopped_early:
                subscription['partial'] = metadata.stopped_early

            logger.info(f"User {user_id} subscribed to feed: {feed_url}")
            return subscription

        except Exception as e:
            logger.error(f"Error subscribing to feed: {e}")
            return {'error': str(e), 'status': 'failed'}

    def check_feed(self, subscription: Dict[str, Any]) -> List[FeedItem]:
        """
        Check a subscription for new items.

        Args:
            subscription: Subscription details dict

        Returns:
            List of new FeedItem objects
        """
        try:
            metadata, items, was_modified = self.importer.fetch_feed(
                subscription['feed_url'],
                etag=subscription.get('etag'),
                last_modified=subscription.get('last_modified')
            )

            if not was_modified:
                return []

            # Update subscription
            subscription['etag'] = metadata.etag
            subscription['last_modified'] = metadata.last_modified
            subscription['last_checked'] = datetime.now(timezone.utc).isoformat()
            if metadata.stopped_early:
                subscription['partial'] = metadata.stopped_early
            else:
                subscription.pop('partial', None)

            return items

        except Exception as e:
            logger.error(f"Error checking feed: {e}")
            subscription['status'] = 'error'
            subscription['last_error'] = str(e)
            return []

    def import_new_items(self, subscription: Dict[str, Any]) -> int:
        """
        Import new items from a subscription.

        Args:
            subscription: Subscription details

        Returns:
            Number of items imported
        """
        items = self.check_feed(subscription)
        if not items:
            return 0

        created_ids = self.importer.import_items(
            items,
            user_id=subscription['user_id'],
            community_id=subscription.get('community_id')
        )

        return len(created_ids)


# Convenience functions
def read_feed(content: str, url: str) -> Optional[Tuple[FeedMetadata, List[FeedItem]]]:
    """``(metadata, items)`` when a body already fetched is an RSS, Atom or
    JSON feed, else None (not a feed, or a body it cannot parse).  For
    readers that fetched a page and must tell a feed from a page: the web
    crawler reads a feed as its items (News Channel demo, 28 Nov 2026: read
    as page text, the model saw one story of The Verge's feed and made up
    two).

    JSON counts only as a jsonfeed.org document (its "version" names it).
    _detect_format also takes any JSON with an "items" list, which suits a
    feed subscription, but a search API's reply is not a feed: read as one,
    its links and snippets were dropped (rv_feed1 on 9b8939bc6)."""
    importer = FeedImporter()
    kind = importer._detect_format(content or '')
    if kind == FeedFormat.UNKNOWN:
        return None
    if kind == FeedFormat.JSON:
        try:
            version = str(json.loads(content).get('version', ''))
        except (ValueError, AttributeError):
            return None
        if 'jsonfeed' not in version.lower():
            return None
    try:
        metadata, items = importer.parse(content, url)
    except Exception as e:
        logger.info(f"Not read as a feed ({url}): {e}")
        return None
    return (metadata, items) if items else None


def fetch_and_parse_feed(url: str) -> Tuple[FeedMetadata, List[FeedItem]]:
    """Fetch and parse a feed without database access."""
    importer = FeedImporter()
    metadata, items, _ = importer.fetch_feed(url)
    return metadata, items


def preview_feed(url: str, limit: int = 5) -> Dict[str, Any]:
    """Preview a feed's contents."""
    try:
        metadata, items = fetch_and_parse_feed(url)
        return {
            'success': True,
            'metadata': {
                'title': metadata.title,
                'description': metadata.description,
                'link': metadata.link,
                'format': metadata.format.value
            },
            'item_count': len(items),
            **({'partial': metadata.stopped_early} if metadata.stopped_early else {}),
            'preview_items': [
                {
                    'title': item.title,
                    'link': item.link,
                    'author': item.author,
                    'published': item.published.isoformat() if item.published else None,
                    'categories': item.categories
                }
                for item in items[:limit]
            ]
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}
