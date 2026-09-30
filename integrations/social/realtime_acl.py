"""
HevolveSocial — shared topic-shape parser used by both the
publish-side gate (realtime._authorize_topic_for_user_id) and the
subscribe-side gate (tenant_acl.authorize_subscribe).

Plan reference: sunny-gliding-eich.md, Part E.13.

Review M2 fix (post Pass-5 + WAMP ACL ship): the two gates were
each parsing topic strings independently — same split-by-`.`, same
`parts[2]` check, same special-cases for `conv` / `user`.  The
risk surfaced as Pass-2 N-NEW-4 (substring vs segment match) on
the publish side; if a future fix landed only in publish, the
subscribe gate would silently diverge.  This module is the single
source of truth.

Topic shape canon:
  tenant.<tid>.<scope>.<id>[.<event>]+

  scope ∈ {conv, user, community, call, ...}

Returns ParsedTopic (NamedTuple) with `tid`, `scope`, `id`, and
`event_suffix` so callers don't have to re-split.
"""

from __future__ import annotations

from collections import namedtuple
from typing import Optional


ParsedTopic = namedtuple(
    'ParsedTopic',
    ['is_tenant_scoped', 'tid', 'scope', 'id', 'event_suffix'])


def parse_topic(topic: Optional[str]) -> ParsedTopic:
    """Parse a `tenant.<tid>.<scope>.<id>[.<event>]+` topic into
    its named components.  Non-tenant topics return
    ParsedTopic(is_tenant_scoped=False, ...) with all fields None.

    Returns a stable shape regardless of topic validity — caller
    inspects `is_tenant_scoped` before trusting the other fields.
    Never raises.

    Examples:
      'tenant.t1.conv.c1.message'  →
          (True, 't1', 'conv', 'c1', 'message')
      'tenant.t1.user.alice'       →
          (True, 't1', 'user', 'alice', '')
      'community.feed'             →
          (False, None, None, None, None)
      ''                           →
          (False, None, None, None, None)
    """
    if not topic or not isinstance(topic, str):
        return ParsedTopic(False, None, None, None, None)

    if not topic.startswith('tenant.'):
        return ParsedTopic(False, None, None, None, None)

    parts = topic.split('.')
    # Need at least: ['tenant', tid, scope, id]
    if len(parts) < 4:
        return ParsedTopic(False, None, None, None, None)

    return ParsedTopic(
        is_tenant_scoped=True,
        tid=parts[1],
        scope=parts[2],
        id=parts[3],
        event_suffix='.'.join(parts[4:]),
    )


def topic_open_to(topic: Optional[str], user_id, publish: bool = False) -> bool:
    """Is a non-tenant ``topic`` one ``user_id`` may publish or subscribe?

    The one answer both gates ask (publish: realtime._authorize_topic_for_
    user_id; subscribe: tenant_acl.authorize_subscribe), built only from the
    canonical tables:
      * everyone's topic -- core.platform.events.topic_audience says
        AUDIENCE_EVERYONE (community feeds, vote scores, node infra); to
        PUBLISH one it must also be a server broadcast
        (events.topic_is_server_broadcast);
      * a bus topic whose Crossbar URI is per-user (chat.social ->
        com.hertzai.hevolve.social.{user_id}): MessageBus substitutes the
        publisher's own id, so only their devices receive it;
      * a concrete topic that names the user (security.edge_privacy
        .uri_names_user).
    A one-person topic is never everyone's.  No user, no per-user answer.
    """
    if not topic:
        return False
    from core.platform.events import (
        AUDIENCE_EVERYONE, topic_audience, topic_is_server_broadcast)
    if topic_audience(topic) == AUDIENCE_EVERYONE:
        return topic_is_server_broadcast(topic) if publish else True
    if not user_id:
        return False
    from core.peer_link.message_bus import crossbar_topic_is_per_user
    from security.edge_privacy import uri_names_user
    return crossbar_topic_is_per_user(topic) or uri_names_user(topic, user_id)


__all__ = ['parse_topic', 'ParsedTopic', 'topic_open_to']
