"""
HevolveSocial - Real-time Events
WAMP publish hooks for live feed updates via Crossbar.
"""
import logging
import json

logger = logging.getLogger('hevolve_social')

_publisher = None


def _get_publisher():
    global _publisher
    if _publisher is not None:
        return _publisher
    try:
        from crossbarhttp3 import CrossbarHttpPublisher
        import os
        url = os.environ.get('WAMP_URL', 'http://localhost:8088/publish')
        _publisher = CrossbarHttpPublisher(url)
    except ImportError:
        logger.debug("crossbarhttp3 not available, real-time events disabled")
    except Exception as e:
        logger.debug(f"WAMP publisher init failed: {e}")
    return _publisher


def publish_event(topic: str, data: dict):
    """Publish an event to a WAMP topic. Non-blocking, fails silently."""
    publisher = _get_publisher()
    if publisher is None:
        return
    try:
        publisher.publish(topic, json.dumps(data))
    except Exception as e:
        logger.debug(f"WAMP publish failed for {topic}: {e}")


def on_new_post(post_dict: dict, community_name: str = None):
    publish_event('social.feed.new_post', post_dict)
    if community_name:
        publish_event(f'social.community.{community_name}.new_post', post_dict)


def on_new_comment(comment_dict: dict, post_id: str):
    publish_event(f'social.post.{post_id}.new_comment', comment_dict)


def on_vote_update(target_type: str, target_id: str, score: int):
    publish_event(f'social.{target_type}.{target_id}.vote', {'score': score})


def on_notification(user_id: str, notification_dict: dict):
    publish_event(f'social.user.{user_id}.notification', notification_dict)
