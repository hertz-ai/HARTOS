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
import os

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


# Fields that must NEVER reach a public WAMP topic (community.feed,
# community.message, social.post.*).  User.to_dict() does not currently
# include email/phone — but any future schema addition would silently
# leak through every broadcast site below.  Strip defensively at the
# boundary so the leak surface is one helper, not every call site.
#
# Why each field:
#   email, phone, phone_number, password_hash, api_token  → PII / creds
#   voice_profile                                          → biometric pointer
#   idle_compute_opt_in                                    → infra disclosure
#   location_sharing_enabled                               → privacy preference
#   referral_code                                          → cross-tracking risk
#   last_active_at                                         → presence/inference leak
_AUTHOR_PUBLIC_BLOCKLIST = (
    'email', 'phone', 'phone_number', 'password_hash', 'api_token',
    'voice_profile', 'idle_compute_opt_in', 'location_sharing_enabled',
    'referral_code', 'last_active_at',
)


def _sanitize_author_for_public(author: dict) -> dict:
    """Return a shallow copy of `author` with sensitive fields removed.
    Idempotent — safe to call on already-sanitized dicts."""
    if not isinstance(author, dict):
        return author
    return {k: v for k, v in author.items() if k not in _AUTHOR_PUBLIC_BLOCKLIST}


def _sanitize_for_public_broadcast(payload: dict) -> dict:
    """Shallow-copy `payload` with `author` (if present) sanitized.
    Used for post/comment dicts bound for community.feed / social.post.*.
    Returns a new dict; original is not mutated."""
    if not isinstance(payload, dict):
        return payload
    cleaned = dict(payload)
    if isinstance(cleaned.get('author'), dict):
        cleaned['author'] = _sanitize_author_for_public(cleaned['author'])
    return cleaned


# ─── Canonical lifecycle fan-out (#49-#52, #54) ─────────────────
#
# Every post/comment lifecycle event (new / update / delete) shares
# the same topic shape: community.feed (global) + community.message.{id}
# (per-community).  Single helper enforces:
#   - PII sanitizer applied once
#   - msg_id stamped once (idempotency key, same generator as chat.new)
#   - event discriminator field so subscribers can patch / append /
#     remove a cached entry without guessing from payload shape
#
# This replaces the prior parallel paths where on_new_comment published
# to social.post.{id}.new_comment (no subscribers — the payload died
# on the wire).  All comment events now route through community.message
# which web/desktop/Android already subscribe to.


def _stamp_event(safe: dict, event: str) -> dict:
    """Add the event discriminator + idempotency key.  Mutates and
    returns `safe` (which is already a shallow copy from the
    sanitizer)."""
    from .chat_messages import make_msg_id
    safe['event'] = event
    # Don't clobber a msg_id the caller already supplied (e.g. chat
    # turns embedding their own ULID).
    safe.setdefault('msg_id', make_msg_id())
    return safe


# ─── Outbound channel bridge (Phase 1 omni-channel, 2026-05-30) ──────
#
# A new post in a broadcast-designated community fans OUT to external
# channels (Discord/Telegram/Slack/…) via the canonical
# announcement_broadcaster — the same fire-and-forget path hive-proof
# announcements already use (no parallel path).  This is the "Nunba
# content made public on other channels to bring in new users" leg.
#
# Safe + private by construction:
#   - The payload is ALREADY PII-sanitized here (`safe`), and the
#     external text uses only title/content/link — never the author —
#     so emails/phones never leave the box.
#   - DOUBLE-GATED: (1) the community must be broadcast-enabled, AND
#     (2) broadcast_announcement only sends when an operator has set
#     `announce_chat_id` on ≥1 channel.  Nothing leaves until BOTH hold.
#   - Only event == 'post.new' (edits/deletes never re-broadcast).
#   - Best-effort: a channel error never blocks the internal fan-out.
#
# HEVOLVE_BROADCAST_COMMUNITIES (comma list, or '*' for all) selects
# which communities broadcast.  Default = the public flywheel
# communities only; personal/user communities stay internal unless the
# operator opts them in.
_DEFAULT_BROADCAST_COMMUNITIES = 'announcements,platform,developers,dev_community'
_broadcast_communities_cache = None


def _broadcast_communities() -> set:
    global _broadcast_communities_cache
    if _broadcast_communities_cache is None:
        raw = os.environ.get('HEVOLVE_BROADCAST_COMMUNITIES',
                             _DEFAULT_BROADCAST_COMMUNITIES)
        _broadcast_communities_cache = {
            c.strip().lower() for c in raw.split(',') if c.strip()}
    return _broadcast_communities_cache


def _community_broadcasts_externally(community_name) -> bool:
    if not community_name:
        return False
    allow = _broadcast_communities()
    return '*' in allow or str(community_name).lower() in allow


def _format_post_for_channel(safe: dict) -> str:
    """Compact external-channel message from a sanitized post dict:
    title + trimmed body + link.  Never includes the author (PII)."""
    title = (safe.get('title') or '').strip()
    body = (safe.get('content') or '').strip()
    if len(body) > 500:
        body = body[:500].rstrip() + '…'
    link = (safe.get('link_url') or '').strip()
    parts = [p for p in (title, body, link) if p]
    return '\n\n'.join(parts) or '(new post)'


def _maybe_broadcast_external(safe: dict, community_name) -> None:
    """Fan a new post out to external channels iff its community is
    broadcast-enabled.  Best-effort; never raises into the fan-out."""
    try:
        if not _community_broadcasts_externally(community_name):
            return
        text = _format_post_for_channel(safe)
        from integrations.channels.announcement_broadcaster import (
            broadcast_announcement)
        n = broadcast_announcement(text)
        if n:
            logger.info(
                "post.new in '%s' fanned out to %d external channel "
                "target(s)", community_name, n)
    except Exception as e:
        logger.debug("external post broadcast skipped: %s", e)


def _publish_post_event(event: str, post_dict: dict,
                        community_name: str = None) -> None:
    """One fan-out for every post lifecycle event.
    `event` ∈ {'post.new', 'post.update', 'post.delete'}."""
    safe = _stamp_event(_sanitize_for_public_broadcast(post_dict), event)
    publish_event('community.feed', safe)
    if community_name:
        data = dict(safe)
        data['community_id'] = community_name
        publish_event('community.message', data)
    # Outbound bridge: new posts in broadcast-enabled communities also
    # go public on external channels (Discord/Telegram/Slack/…).
    if event == 'post.new':
        _maybe_broadcast_external(safe, community_name)


def _publish_comment_event(event: str, comment_dict: dict,
                           community_name: str = None) -> None:
    """One fan-out for every comment lifecycle event.
    `event` ∈ {'comment.new', 'comment.delete'}.
    Routes via community.message.{id} so it lands on the same topic
    web/desktop/Android subscribe to.  If the comment's post has no
    community, the per-user NotificationService path is the only live
    channel — that fires from CommentService.create independently."""
    safe = _stamp_event(_sanitize_for_public_broadcast(comment_dict), event)
    if community_name:
        data = dict(safe)
        data['community_id'] = community_name
        publish_event('community.message', data)


def on_new_post(post_dict: dict, community_name: str = None):
    _publish_post_event('post.new', post_dict, community_name)


def on_post_update(post_dict: dict, community_name: str = None):
    _publish_post_event('post.update', post_dict, community_name)


def on_post_delete(post_dict: dict, community_name: str = None):
    """post_dict is the PRE-delete snapshot (id + author_id + community).
    Subscribers locate the cached entry by id and remove it."""
    _publish_post_event('post.delete', post_dict, community_name)


def on_new_comment(comment_dict: dict, community_name: str = None,
                   post_id: str = None):
    """Compat: legacy signature accepted `post_id` positionally; new
    callers should pass `community_name`.  `post_id` is ignored — the
    comment_dict already carries it as comment_dict['post_id']."""
    _publish_comment_event('comment.new', comment_dict, community_name)


def on_comment_delete(comment_dict: dict, community_name: str = None):
    _publish_comment_event('comment.delete', comment_dict, community_name)


def on_community_membership(community_id: str, user_id: str, action: str,
                            role: str = None):
    """Realtime fan-out for a community membership change (#55).

    `action` in {'join', 'leave', 'role_change'}.  Routes via the per-community
    topic (community.message → com.hertzai.hevolve.community.{id}) that web + RN
    already subscribe to, so members see the change live instead of only on the
    next /api/social/communities/{id} fetch.  Clients filter by event type
    ('community.membership' vs 'post.new'/'comment.new')."""
    if not community_id:
        return
    payload = _stamp_event({
        'community_id': community_id,
        'user_id': user_id,
        'action': action,
        'role': role,
    }, 'community.membership')
    publish_event('community.message', payload)


def on_vote_update(target_type: str, target_id: str, score: int):
    # Local-only (no frontend subscribes to per-target WAMP topics)
    publish_event(f'social.{target_type}.{target_id}.vote', {'score': score})


def on_notification(user_id: str, notification_dict: dict):
    # Stamp idempotency key once so the same payload lands on WAMP
    # and SSE with the same msg_id — clients dedupe transport races.
    from .chat_messages import make_msg_id
    msg_id = notification_dict.get('msg_id') or make_msg_id()
    payload = {
        'type': 'notification',
        'msg_id': msg_id,
        **notification_dict,
    }
    # Route to per-user social topic (RN + web subscribe to com.hertzai.hevolve.social.{user_id})
    publish_event('chat.social', payload, user_id=user_id)
    # Also broadcast to SSE clients (Nunba desktop) — scoped to the target user
    from core.platform.events import broadcast_sse_safe
    broadcast_sse_safe('notification', {
        'user_id': user_id,
        'msg_id': msg_id,
        **notification_dict,
    }, user_id=user_id)


def on_notification_read(user_id: str, ids):
    """Cross-device read-state fan-out (P1-S1, 2026-05-26).

    When NotificationService.mark_read / mark_all_read flips one or
    more rows, every device the user has open should see its unread
    badge decrement within ~1s instead of waiting for the next 30s
    poll cycle.  Uses the same chat.social WAMP topic and SSE pipe
    on_notification() already uses — clients filter by the inner
    `type` field ('notification.read' vs 'notification').

    `ids` is an iterable of notification id strings; empty / None
    is a silent no-op so callers don't need to guard.
    """
    if not ids:
        return
    id_list = list(ids)
    publish_event('chat.social', {
        'type': 'notification.read',
        'user_id': user_id,
        'ids': id_list,
    }, user_id=user_id)
    from core.platform.events import broadcast_sse_safe
    broadcast_sse_safe('notification.read', {
        'user_id': user_id,
        'ids': id_list,
    }, user_id=user_id)
