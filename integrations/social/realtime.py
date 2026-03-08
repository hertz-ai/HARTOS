"""
HevolveSocial - Real-time Events

Publishes via MessageBus (LOCAL EventBus + PeerLink + Crossbar).
Falls back to direct HTTP if MessageBus unavailable.

Topic routing (MessageBus TOPIC_MAP):
  chat.social       → com.hertzai.hevolve.social.{user_id}  (RN + web subscribe)
  community.feed    → com.hertzai.community.feed             (RN global feed)
  community.message → com.hertzai.hevolve.community.{id}     (web per-community)
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


def publish_event(topic: str, data: dict, user_id: str = ''):
    """Publish via MessageBus (LOCAL + PEERLINK + CROSSBAR). Falls back to direct HTTP."""
    try:
        from core.peer_link.message_bus import get_message_bus
        bus = get_message_bus()
        bus.publish(topic, data, user_id=user_id)
        return
    except Exception:
        pass
    # Fallback: direct HTTP (original path)
    publisher = _get_publisher()
    if publisher is None:
        return
    try:
        publisher.publish(topic, json.dumps(data))
    except Exception as e:
        logger.debug(f"WAMP publish failed for {topic}: {e}")


def on_new_post(post_dict: dict, community_name: str = None):
    # Broadcast to global community feed (RN subscribes to com.hertzai.community.feed)
    publish_event('community.feed', post_dict)
    # Also per-community (web subscribes to com.hertzai.hevolve.community.{id})
    if community_name:
        data = dict(post_dict)
        data['community_id'] = community_name
        publish_event('community.message', data)


def on_new_comment(comment_dict: dict, post_id: str):
    # Local-only (no frontend subscribes to per-post WAMP topics)
    publish_event(f'social.post.{post_id}.new_comment', comment_dict)


def on_vote_update(target_type: str, target_id: str, score: int):
    # Local-only (no frontend subscribes to per-target WAMP topics)
    publish_event(f'social.{target_type}.{target_id}.vote', {'score': score})


def on_notification(user_id: str, notification_dict: dict):
    # Route to per-user social topic (RN + web subscribe to com.hertzai.hevolve.social.{user_id})
    publish_event('chat.social', {
        'type': 'notification',
        **notification_dict,
    }, user_id=user_id)
    # Also broadcast to SSE clients (Nunba desktop) — scoped to the target user
    try:
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod and hasattr(main_mod, 'broadcast_sse_event'):
            main_mod.broadcast_sse_event('notification', {
                'user_id': user_id,
                **notification_dict,
            }, user_id=user_id)
    except Exception:
        pass
