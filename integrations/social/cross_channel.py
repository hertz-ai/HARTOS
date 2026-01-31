"""
HevolveSocial - Cross-Channel Ingestion
Ingest messages from Discord/Telegram/etc. into the social feed as posts.
"""
import logging
from typing import Optional
from .models import get_db, Post, User
from .services import PostService, UserService

logger = logging.getLogger('hevolve_social')


def ingest_channel_message(channel: str, sender_name: str, content: str,
                           message_id: str = None, media_urls: list = None
                           ) -> Optional[str]:
    """
    Ingest a message from an external channel as a social post.
    Deduplicates by source_message_id.
    Returns the post ID if created, None if duplicate.
    """
    db = get_db()
    try:
        # Deduplicate
        if message_id:
            existing = db.query(Post).filter(
                Post.source_channel == channel,
                Post.source_message_id == message_id
            ).first()
            if existing:
                return None

        # Get or create user for this channel sender
        username = f"{channel}_{sender_name}".lower().replace(' ', '_')[:50]
        user = db.query(User).filter(User.username == username).first()
        if not user:
            try:
                user = UserService.register_agent(
                    db, username, f"User from {channel}", f"channel_{channel}",
                    skip_name_validation=True)
            except ValueError:
                user = db.query(User).filter(User.username == username).first()

        # Create post
        content_type = 'media' if media_urls else 'text'
        post = PostService.create(
            db, user, content[:300], content,
            content_type=content_type, media_urls=media_urls,
            source_channel=channel, source_message_id=message_id,
        )
        db.commit()
        return post.id
    except Exception as e:
        db.rollback()
        logger.debug(f"Channel ingest error ({channel}): {e}")
        return None
    finally:
        db.close()
