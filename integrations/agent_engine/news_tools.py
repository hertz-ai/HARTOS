"""
News Agent Tools — AutoGen tools for news curation and push notifications.

Tier 2 tools (agent_engine context). Same registration pattern as revenue_tools.py.
Uses existing FeedImporter, NotificationService, and feed_engine infrastructure.
"""
import json
import logging
from typing import Annotated, Optional

logger = logging.getLogger('hevolve_social')


def register_news_tools(helper, assistant, user_id: str):
    """Register news curation and push notification tools with an AutoGen agent."""

    def fetch_news_feeds(
        feed_urls: Annotated[str, "Comma-separated RSS/Atom feed URLs to fetch"],
        max_items: Annotated[int, "Maximum items to return per feed"] = 10,
    ) -> str:
        """Fetch and parse RSS/Atom/JSON feeds. Returns titles, links, categories."""
        try:
            from integrations.social.feed_import import FeedImporter

            importer = FeedImporter()
            all_items = []
            errors = []

            for url in feed_urls.split(','):
                url = url.strip()
                if not url:
                    continue
                try:
                    metadata, items, _ = importer.fetch_feed(url)
                    for item in items[:max_items]:
                        all_items.append({
                            'title': item.title,
                            'link': item.link,
                            'author': item.author,
                            'published': item.published.isoformat() if item.published else None,
                            'categories': item.categories,
                            'source': metadata.title or url,
                            'content_preview': item.content[:200] if item.content else '',
                        })
                except Exception as e:
                    errors.append({'url': url, 'error': str(e)})

            return json.dumps({
                'items': all_items,
                'total': len(all_items),
                'errors': errors,
            })
        except Exception as e:
            return json.dumps({'error': str(e)})

    def subscribe_news_feed(
        feed_url: Annotated[str, "RSS/Atom/JSON feed URL to subscribe to"],
        categories: Annotated[str, "Comma-separated category tags for this feed"] = '',
    ) -> str:
        """Subscribe to a new RSS/Atom feed for ongoing monitoring."""
        try:
            from integrations.social.feed_import import FeedSubscriptionService
            from integrations.social.models import get_db

            db = get_db()
            try:
                svc = FeedSubscriptionService(db)
                result = svc.subscribe(
                    user_id=int(user_id) if user_id.isdigit() else 0,
                    feed_url=feed_url.strip(),
                    auto_import=True,
                )
                if categories:
                    result['categories'] = [c.strip() for c in categories.split(',')]
                return json.dumps(result)
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    def send_news_notification(
        title: Annotated[str, "Notification title (news headline)"],
        message: Annotated[str, "Notification body (summary + source attribution)"],
        source_url: Annotated[str, "Link to the original article"],
        scope: Annotated[str, "Target scope: all, regional, or a specific user_id"] = 'all',
        category: Annotated[str, "News category tag"] = 'news',
    ) -> str:
        """Push a curated news item as notification to users."""
        try:
            from integrations.social.models import get_db, User, Notification
            from integrations.social.services import NotificationService
            from sqlalchemy import func

            db = get_db()
            try:
                # Determine target users
                if scope == 'all':
                    target_users = db.query(User.id).filter(
                        User.is_active == True  # noqa: E712
                    ).limit(1000).all()
                    target_ids = [str(u.id) for u in target_users]
                elif scope == 'regional':
                    # For regional: broadcast to all active users
                    # Region filtering will be refined when regional user assignment exists
                    target_users = db.query(User.id).filter(
                        User.is_active == True  # noqa: E712
                    ).limit(500).all()
                    target_ids = [str(u.id) for u in target_users]
                else:
                    # Specific user_id
                    target_ids = [scope]

                sent_count = 0
                full_message = f"{message}\n\nSource: {source_url}"

                for uid in target_ids:
                    try:
                        NotificationService.create(
                            db,
                            user_id=uid,
                            type=f'news_{category}',
                            source_user_id=user_id,
                            target_type='news',
                            target_id=source_url[:64],
                            message=f"{title}\n{full_message}",
                        )
                        sent_count += 1
                    except Exception:
                        continue

                if sent_count:
                    db.commit()

                return json.dumps({
                    'success': True,
                    'sent_count': sent_count,
                    'scope': scope,
                    'category': category,
                    'title': title,
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    def get_trending_news(
        limit: Annotated[int, "Maximum number of trending items to return"] = 10,
    ) -> str:
        """Get trending/hot news items from already-imported feed posts."""
        try:
            from integrations.social.models import get_db
            from integrations.social.feed_engine import get_trending_feed

            db = get_db()
            try:
                posts = get_trending_feed(db, limit=limit)
                items = []
                for post in posts:
                    p = post.to_dict() if hasattr(post, 'to_dict') else {}
                    items.append({
                        'id': p.get('id'),
                        'title': p.get('title', ''),
                        'content_preview': (p.get('content', '') or '')[:200],
                        'author_id': p.get('author_id'),
                        'vote_count': p.get('vote_count', 0),
                        'comment_count': p.get('comment_count', 0),
                        'created_at': p.get('created_at'),
                    })
                return json.dumps({'trending': items, 'count': len(items)})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    def get_news_metrics(
        days: Annotated[int, "Number of days to look back"] = 7,
        scope: Annotated[str, "Filter by notification type prefix: news_world, news_local, or all"] = 'all',
    ) -> str:
        """Get news notification delivery stats (sent count, read rate)."""
        try:
            from integrations.social.models import get_db, Notification
            from sqlalchemy import func
            from datetime import datetime, timedelta

            db = get_db()
            try:
                cutoff = datetime.utcnow() - timedelta(days=days)
                query = db.query(Notification).filter(
                    Notification.created_at >= cutoff,
                    Notification.type.like('news_%'),
                )
                if scope != 'all':
                    query = query.filter(Notification.type == scope)

                total = query.count()
                read_count = query.filter(Notification.is_read == True).count()  # noqa: E712

                # Group by type
                type_counts = db.query(
                    Notification.type,
                    func.count(Notification.id),
                ).filter(
                    Notification.created_at >= cutoff,
                    Notification.type.like('news_%'),
                ).group_by(Notification.type).all()

                return json.dumps({
                    'period_days': days,
                    'total_sent': total,
                    'total_read': read_count,
                    'read_rate': round(read_count / max(total, 1), 3),
                    'by_type': {t: c for t, c in type_counts},
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    tools = [
        ('fetch_news_feeds',
         'Fetch and parse RSS/Atom feeds, returning titles, links, and categories',
         fetch_news_feeds),
        ('subscribe_news_feed',
         'Subscribe to a new RSS/Atom feed URL for ongoing monitoring',
         subscribe_news_feed),
        ('send_news_notification',
         'Push a curated news item as notification to users (all, regional, or specific user)',
         send_news_notification),
        ('get_trending_news',
         'Get trending/hot news items from imported feed posts',
         get_trending_news),
        ('get_news_metrics',
         'Get news notification delivery stats: sent count, read rate, by category',
         get_news_metrics),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} news tools for user {user_id}")
