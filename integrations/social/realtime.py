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


# Topics that may fan out to all authenticated subscribers (public feed,
# aggregate counters, community rooms the user has joined).  Anything
# else MUST be per-user — the topic string must end with the user_id
# so the WAMP router can gate subscriptions via role-based authorizer.
_PUBLIC_TOPIC_PREFIXES = (
    'community.feed',
    'community.message',
    'social.post.',       # post-scoped (aggregate vote counts)
    'social.comment.',    # comment-scoped
    'social.user.',       # user-scoped (public profile fan-out)
    'chat.social',        # per-user, user_id threaded via data
    'dm.',                # per-conversation, gated elsewhere
    'presence.',          # per-user presence
    'game.',              # game session id in the topic
    'setup_progress',     # boot-time setup progress (pre-auth, no user_id)
    'setup.',             # boot-time setup (broader)
    'system.',            # system-wide events (catalog/orchestrator)
    'catalog.',           # model catalog updates
    'model.',             # per-model lifecycle
    'tts.',               # per-user audio-ready event (audio URL per request)
    'admin.',             # admin-console broadcasts
)


def _authorize_topic_for_user_id(topic: str, user_id: str) -> bool:
    """Validate that `topic` is publishable for `user_id`.

    A topic is considered owned-by-user_id iff:
      - it is in the public prefix whitelist above (community / aggregate), OR
      - it ends with `.{user_id}` or `/{user_id}` (per-user fanout topic).

    Returns True when the pair is OK, False when a cross-user publish
    is being attempted.  Callers log + refuse on False so the WAMP
    router's subscribe-side authorizer has a defense-in-depth pair
    enforcing the same invariant at the publish site.
    """
    if not topic:
        return False
    # Public / aggregate — every authenticated user may receive.
    for pref in _PUBLIC_TOPIC_PREFIXES:
        if topic == pref or topic.startswith(pref):
            return True
    # Per-user topic must end with the publisher's user_id.
    if user_id:
        if topic.endswith(f'.{user_id}') or topic.endswith(f'/{user_id}'):
            return True
    return False


def publish_event(topic: str, data: dict, user_id: str = ''):
    """Publish via MessageBus (LOCAL + PEERLINK + CROSSBAR). Falls back to direct HTTP.

    Authorization guard: cross-user topic publish is refused at this
    entry so a compiled-in bug that emits e.g. user A's notification
    to topic `...social.B` is caught here instead of leaking to B's
    subscribe channel.  The WAMP router's role-based subscribe
    authorizer (when configured) enforces the same invariant on the
    receive side — defense in depth.
    """
    if not _authorize_topic_for_user_id(topic, user_id):
        logger.warning(
            f"WAMP publish refused: user_id={user_id!r} cannot publish "
            f"to topic={topic!r} (cross-user topic subscribe guard)"
        )
        return
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
    from core.platform.events import broadcast_sse_safe
    broadcast_sse_safe('notification', {
        'user_id': user_id,
        **notification_dict,
    }, user_id=user_id)
