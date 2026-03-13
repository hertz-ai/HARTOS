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
            except:
                pass

        if '<feed' in content_lower and 'xmlns' in content_lower:
            return FeedFormat.ATOM

        if '<rss' in content_lower or '<channel>' in content_lower:
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
            except:
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
            response = requests.get(url, headers=headers, timeout=self.timeout)

            # Handle 304 Not Modified
            if response.status_code == 304:
                return FeedMetadata(url=url), [], False

            response.raise_for_status()
            content = response.text

            # Detect format
            feed_format = self._detect_format(content)

            if feed_format == FeedFormat.JSON:
                metadata, items = self._parse_json_feed(content, url)
            elif FEEDPARSER_AVAILABLE:
                metadata, items = self._parse_with_feedparser(content, url)
            else:
                raise ValueError("feedparser not available and non-JSON feed detected")

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

    def import_items(self, items: List[FeedItem], user_id: int,
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
            from .models import Post, get_db
            from .services import PostService

            created_ids = []

            for item in items:
                # Check for duplicates by content hash
                existing = self.db.query(Post).filter(
                    Post.content.contains(item.content_hash)
                ).first()

                if existing:
                    logger.debug(f"Skipping duplicate item: {item.title}")
                    continue

                # Build post content
                content = f"**{item.title}**\n\n{item.content}"
                if item.link:
                    content += f"\n\n[Source]({item.link})"

                # Add content hash for future dedup
                content += f"\n\n<!-- feed_hash:{item.content_hash} -->"

                # Prepare tags
                tags = list(item.categories)
                if auto_tag and item.source_feed:
                    # Add source domain as tag
                    from urllib.parse import urlparse
                    domain = urlparse(item.source_feed).netloc
                    if domain:
                        tags.append(f"via:{domain.replace('www.', '')}")

                # Create post
                try:
                    post = PostService.create_post(
                        self.db,
                        author_id=user_id,
                        content=content,
                        tags=tags[:10],  # Limit tags
                        media_urls=item.media_urls[:5],  # Limit media
                        community_id=community_id,
                        post_type='link' if item.link else 'text'
                    )
                    self.db.commit()
                    created_ids.append(post.id)
                    logger.info(f"Imported feed item as post {post.id}: {item.title[:50]}")
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

    def subscribe(self, user_id: int, feed_url: str,
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
