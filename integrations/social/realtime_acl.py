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


__all__ = ['parse_topic', 'ParsedTopic']
