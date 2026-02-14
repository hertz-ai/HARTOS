"""
feed_export.py - RSS and Atom feed generation for HevolveSocial

Generates standardized feed formats (RSS 2.0, Atom 1.0, JSON Feed 1.1)
from social content including posts, comments, and user activity.

Usage:
    from integrations.social.feed_export import FeedGenerator

    generator = FeedGenerator(db)
    rss_xml = generator.generate_rss(feed_type='global', limit=50)
    atom_xml = generator.generate_atom(feed_type='trending')
"""

import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from xml.etree import ElementTree as ET
from html import escape
import json

logger = logging.getLogger('hevolve_social')

# Feed configuration
FEED_CONFIG = {
    'title': 'Hevolve Social',
    'description': 'AI-powered social network for humans and agents',
    'link': 'https://hevolve.ai',
    'language': 'en-us',
    'generator': 'HevolveSocial Feed Generator v1.0',
    'ttl': 60,  # minutes
}


class FeedGenerator:
    """Generates RSS, Atom, and JSON feeds from social content."""

    def __init__(self, db_session, base_url: str = None):
        """
        Initialize feed generator.

        Args:
            db_session: SQLAlchemy database session
            base_url: Base URL for feed links (default: from config)
        """
        self.db = db_session
        self.base_url = base_url or FEED_CONFIG['link']

    def _get_posts(self, feed_type: str = 'global', limit: int = 50,
                   user_id: int = None, community_id: int = None) -> List[Dict]:
        """
        Fetch posts for feed generation.

        Args:
            feed_type: 'global', 'trending', 'personalized', 'agents'
            limit: Maximum number of posts
            user_id: User ID for personalized/user feeds
            community_id: Community ID for community feeds
        """
        try:
            from .feed_engine import FeedEngine
            from .models import Post, User, Community

            engine = FeedEngine(self.db)

            if community_id:
                # Community-specific feed
                posts = self.db.query(Post).filter(
                    Post.community_id == community_id,
                    Post.deleted_at.is_(None)
                ).order_by(Post.created_at.desc()).limit(limit).all()
            elif user_id and feed_type == 'personalized':
                # Personalized feed for user
                posts = engine.get_personalized_feed(user_id, limit=limit)
            elif feed_type == 'trending':
                posts = engine.get_trending_feed(limit=limit)
            elif feed_type == 'agents':
                posts = engine.get_agent_feed(limit=limit)
            else:
                # Global feed
                posts = engine.get_global_feed(limit=limit)

            # Convert to dict format
            result = []
            for post in posts:
                post_dict = post.to_dict() if hasattr(post, 'to_dict') else post
                result.append(post_dict)

            return result

        except Exception as e:
            logger.error(f"Error fetching posts for feed: {e}")
            return []

    def _format_post_content(self, post: Dict) -> str:
        """Format post content for feed inclusion."""
        content = post.get('content', '')

        # Add media if present
        media = post.get('media_urls') or []
        if media:
            content += '\n\n'
            for url in media:
                if any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                    content += f'<img src="{escape(url)}" />\n'
                else:
                    content += f'<a href="{escape(url)}">{escape(url)}</a>\n'

        return content

    def _get_post_url(self, post: Dict) -> str:
        """Generate URL for a post."""
        post_id = post.get('id', '')
        return f"{self.base_url}/social/post/{post_id}"

    def _get_author_url(self, author: Dict) -> str:
        """Generate URL for an author."""
        author_id = author.get('id', '')
        return f"{self.base_url}/social/u/{author_id}"

    def generate_rss(self, feed_type: str = 'global', limit: int = 50,
                     user_id: int = None, community_id: int = None,
                     title: str = None) -> str:
        """
        Generate RSS 2.0 feed.

        Args:
            feed_type: Type of feed ('global', 'trending', 'personalized', 'agents')
            limit: Maximum number of items
            user_id: User ID for personalized feeds
            community_id: Community ID for community feeds
            title: Custom feed title

        Returns:
            RSS 2.0 XML string
        """
        posts = self._get_posts(feed_type, limit, user_id, community_id)

        # Build RSS structure
        rss = ET.Element('rss', version='2.0')
        rss.set('xmlns:atom', 'http://www.w3.org/2005/Atom')
        rss.set('xmlns:dc', 'http://purl.org/dc/elements/1.1/')

        channel = ET.SubElement(rss, 'channel')

        # Channel metadata
        feed_title = title or f"{FEED_CONFIG['title']} - {feed_type.title()} Feed"
        ET.SubElement(channel, 'title').text = feed_title
        ET.SubElement(channel, 'link').text = self.base_url
        ET.SubElement(channel, 'description').text = FEED_CONFIG['description']
        ET.SubElement(channel, 'language').text = FEED_CONFIG['language']
        ET.SubElement(channel, 'generator').text = FEED_CONFIG['generator']
        ET.SubElement(channel, 'ttl').text = str(FEED_CONFIG['ttl'])
        ET.SubElement(channel, 'lastBuildDate').text = datetime.now(timezone.utc).strftime(
            '%a, %d %b %Y %H:%M:%S +0000')

        # Self-referential atom:link
        atom_link = ET.SubElement(channel, '{http://www.w3.org/2005/Atom}link')
        atom_link.set('href', f"{self.base_url}/api/social/feeds/rss?type={feed_type}")
        atom_link.set('rel', 'self')
        atom_link.set('type', 'application/rss+xml')

        # Add items
        for post in posts:
            item = ET.SubElement(channel, 'item')

            # Title - use first line of content or truncate
            content = post.get('content', 'Untitled')
            title_text = content.split('\n')[0][:100]
            if len(content) > 100:
                title_text += '...'
            ET.SubElement(item, 'title').text = title_text

            # Link
            ET.SubElement(item, 'link').text = self._get_post_url(post)

            # GUID
            guid = ET.SubElement(item, 'guid')
            guid.text = self._get_post_url(post)
            guid.set('isPermaLink', 'true')

            # Description (full content)
            ET.SubElement(item, 'description').text = self._format_post_content(post)

            # Author
            author = post.get('author', {})
            author_name = author.get('display_name') or author.get('username', 'Anonymous')
            ET.SubElement(item, '{http://purl.org/dc/elements/1.1/}creator').text = author_name

            # Publication date
            created_at = post.get('created_at')
            if created_at:
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    except:
                        created_at = datetime.now(timezone.utc)
                ET.SubElement(item, 'pubDate').text = created_at.strftime(
                    '%a, %d %b %Y %H:%M:%S +0000')

            # Categories (tags)
            tags = post.get('tags', [])
            for tag in tags:
                ET.SubElement(item, 'category').text = tag

            # Community as category
            community = post.get('community')
            if community:
                community_name = community.get('name') if isinstance(community, dict) else str(community)
                ET.SubElement(item, 'category').text = f"s/{community_name}"

        # Generate XML string
        return ET.tostring(rss, encoding='unicode', xml_declaration=True)

    def generate_atom(self, feed_type: str = 'global', limit: int = 50,
                      user_id: int = None, community_id: int = None,
                      title: str = None) -> str:
        """
        Generate Atom 1.0 feed.

        Args:
            feed_type: Type of feed
            limit: Maximum number of entries
            user_id: User ID for personalized feeds
            community_id: Community ID for community feeds
            title: Custom feed title

        Returns:
            Atom 1.0 XML string
        """
        posts = self._get_posts(feed_type, limit, user_id, community_id)

        # Atom namespace
        ATOM_NS = 'http://www.w3.org/2005/Atom'

        feed = ET.Element('{%s}feed' % ATOM_NS)
        feed.set('xmlns', ATOM_NS)

        # Feed metadata
        feed_title = title or f"{FEED_CONFIG['title']} - {feed_type.title()} Feed"
        ET.SubElement(feed, 'title').text = feed_title
        ET.SubElement(feed, 'subtitle').text = FEED_CONFIG['description']

        # Links
        link_self = ET.SubElement(feed, 'link')
        link_self.set('href', f"{self.base_url}/api/social/feeds/atom?type={feed_type}")
        link_self.set('rel', 'self')
        link_self.set('type', 'application/atom+xml')

        link_alt = ET.SubElement(feed, 'link')
        link_alt.set('href', self.base_url)
        link_alt.set('rel', 'alternate')
        link_alt.set('type', 'text/html')

        # Feed ID
        ET.SubElement(feed, 'id').text = f"{self.base_url}/feeds/{feed_type}"

        # Updated
        ET.SubElement(feed, 'updated').text = datetime.now(timezone.utc).isoformat()

        # Generator
        generator = ET.SubElement(feed, 'generator')
        generator.text = 'HevolveSocial'
        generator.set('version', '1.0')
        generator.set('uri', self.base_url)

        # Add entries
        for post in posts:
            entry = ET.SubElement(feed, 'entry')

            # Title
            content = post.get('content', 'Untitled')
            title_text = content.split('\n')[0][:100]
            if len(content) > 100:
                title_text += '...'
            ET.SubElement(entry, 'title').text = title_text

            # ID
            ET.SubElement(entry, 'id').text = self._get_post_url(post)

            # Link
            link = ET.SubElement(entry, 'link')
            link.set('href', self._get_post_url(post))
            link.set('rel', 'alternate')
            link.set('type', 'text/html')

            # Content
            content_el = ET.SubElement(entry, 'content')
            content_el.set('type', 'html')
            content_el.text = self._format_post_content(post)

            # Summary (truncated)
            summary = content[:300]
            if len(content) > 300:
                summary += '...'
            ET.SubElement(entry, 'summary').text = summary

            # Author
            author = post.get('author', {})
            author_el = ET.SubElement(entry, 'author')
            ET.SubElement(author_el, 'name').text = author.get('display_name') or author.get('username', 'Anonymous')
            if author.get('id'):
                ET.SubElement(author_el, 'uri').text = self._get_author_url(author)

            # Dates
            created_at = post.get('created_at')
            updated_at = post.get('updated_at')

            if created_at:
                if isinstance(created_at, str):
                    ET.SubElement(entry, 'published').text = created_at
                else:
                    ET.SubElement(entry, 'published').text = created_at.isoformat()

            if updated_at:
                if isinstance(updated_at, str):
                    ET.SubElement(entry, 'updated').text = updated_at
                else:
                    ET.SubElement(entry, 'updated').text = updated_at.isoformat()
            elif created_at:
                ET.SubElement(entry, 'updated').text = ET.SubElement(entry, 'published').text if hasattr(entry.find('published'), 'text') else datetime.now(timezone.utc).isoformat()

            # Categories
            tags = post.get('tags', [])
            for tag in tags:
                cat = ET.SubElement(entry, 'category')
                cat.set('term', tag)

        return ET.tostring(feed, encoding='unicode', xml_declaration=True)

    def generate_json_feed(self, feed_type: str = 'global', limit: int = 50,
                           user_id: int = None, community_id: int = None,
                           title: str = None) -> str:
        """
        Generate JSON Feed 1.1.

        Args:
            feed_type: Type of feed
            limit: Maximum number of items
            user_id: User ID for personalized feeds
            community_id: Community ID for community feeds
            title: Custom feed title

        Returns:
            JSON Feed string
        """
        posts = self._get_posts(feed_type, limit, user_id, community_id)

        feed_title = title or f"{FEED_CONFIG['title']} - {feed_type.title()} Feed"

        json_feed = {
            'version': 'https://jsonfeed.org/version/1.1',
            'title': feed_title,
            'home_page_url': self.base_url,
            'feed_url': f"{self.base_url}/api/social/feeds/json?type={feed_type}",
            'description': FEED_CONFIG['description'],
            'language': FEED_CONFIG['language'],
            'items': []
        }

        for post in posts:
            author = post.get('author', {})

            item = {
                'id': str(post.get('id', '')),
                'url': self._get_post_url(post),
                'content_html': self._format_post_content(post),
                'content_text': post.get('content', ''),
            }

            # Title
            content = post.get('content', 'Untitled')
            title_text = content.split('\n')[0][:100]
            if len(content) > 100:
                title_text += '...'
            item['title'] = title_text

            # Summary
            summary = content[:300]
            if len(content) > 300:
                summary += '...'
            item['summary'] = summary

            # Author
            item['authors'] = [{
                'name': author.get('display_name') or author.get('username', 'Anonymous'),
                'url': self._get_author_url(author) if author.get('id') else None
            }]

            # Dates
            if post.get('created_at'):
                created = post['created_at']
                if isinstance(created, str):
                    item['date_published'] = created
                else:
                    item['date_published'] = created.isoformat()

            if post.get('updated_at'):
                updated = post['updated_at']
                if isinstance(updated, str):
                    item['date_modified'] = updated
                else:
                    item['date_modified'] = updated.isoformat()

            # Tags
            item['tags'] = post.get('tags', [])

            # Attachments (media)
            media = post.get('media_urls', [])
            if media:
                item['attachments'] = []
                for url in media:
                    mime = 'image/jpeg'
                    if '.png' in url.lower():
                        mime = 'image/png'
                    elif '.gif' in url.lower():
                        mime = 'image/gif'
                    elif '.webp' in url.lower():
                        mime = 'image/webp'
                    elif '.mp4' in url.lower():
                        mime = 'video/mp4'

                    item['attachments'].append({
                        'url': url,
                        'mime_type': mime
                    })

            json_feed['items'].append(item)

        return json.dumps(json_feed, indent=2, default=str)


def get_user_feed_rss(db, user_id: int, limit: int = 50) -> str:
    """Generate RSS feed for a specific user's posts."""
    generator = FeedGenerator(db)

    try:
        from .models import Post, User
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return generator.generate_rss(feed_type='global', limit=0)

        title = f"{user.display_name or user.username}'s Posts - Hevolve"
        # Get user's posts directly
        posts = db.query(Post).filter(
            Post.author_id == user_id,
            Post.deleted_at.is_(None)
        ).order_by(Post.created_at.desc()).limit(limit).all()

        # Temporarily override _get_posts
        original_get_posts = generator._get_posts
        generator._get_posts = lambda *args, **kwargs: [p.to_dict() for p in posts]

        result = generator.generate_rss(feed_type='user', title=title)
        generator._get_posts = original_get_posts
        return result

    except Exception as e:
        logger.error(f"Error generating user feed: {e}")
        return generator.generate_rss(feed_type='global', limit=0)


def get_community_feed_rss(db, community_id: int, limit: int = 50) -> str:
    """Generate RSS feed for a specific community."""
    generator = FeedGenerator(db)

    try:
        from .models import Community
        community = db.query(Community).filter(Community.id == community_id).first()
        if not community:
            return generator.generate_rss(feed_type='global', limit=0)

        title = f"s/{community.name} - Hevolve"
        return generator.generate_rss(
            feed_type='community',
            community_id=community_id,
            limit=limit,
            title=title
        )

    except Exception as e:
        logger.error(f"Error generating community feed: {e}")
        return generator.generate_rss(feed_type='global', limit=0)
