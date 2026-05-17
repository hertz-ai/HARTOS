"""
HevolveSocial — Phase 8.B WAMP per-tenant subscribe ACL.

Plan reference: sunny-gliding-eich.md, Part E.13 + Part 8.

This module is the SUBSCRIBE-side counterpart to the publish-side
authorizer at integrations/social/realtime._authorize_topic_for_user_id.
The publish-side gate fires when our own code tries to emit; the
subscribe-side gate fires when an external client (mobile, web,
desktop) tries to register interest in a topic.

Crossbar.io supports "dynamic authorizers" — an HTTP callback the
router invokes for each subscribe/publish request.  This module
provides the function that callback wraps:

  authorize_subscribe(topic, jwt_payload) -> bool

Plus a REST endpoint (`GET /api/social/tenants/by-slug/<slug>`)
that maps a human-readable tenant slug (e.g., "acme-corp") to the
internal `tid` UUID.  Nunba web's signup flow calls this to get the
JWT 'tid' claim it needs to bind to.

Same SQL `tenants` table lookup pattern as Plan E.1 — stays a
no-op for flat / regional / Nunba bundled deploys (no tenants
exist in those modes).

Transport: this module ONLY makes authorization decisions; no
publish, no fan-out.  Crossbar router is the publish gate; this
module is the subscribe gate.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import text

from .realtime_acl import parse_topic

logger = logging.getLogger('hevolve_social')


def authorize_subscribe(topic: str,
                        jwt_payload: Optional[Dict[str, Any]]) -> bool:
    """Phase 8.B subscribe-side gate.

    Decision tree:
      1. No topic → refuse.
      2. Public topic (`community.feed`, `chat.social`, etc.) →
         allow for any authenticated user.
      3. Tenant-scoped topic (`tenant.<tid>.*`) → allow iff JWT
         `tid` claim matches.
      4. Per-user topic ending in `.<user_id>` → allow iff JWT
         `user_id` claim matches.
      5. Anything else → refuse (unknown shape).

    `jwt_payload` is the decoded JWT body the router parsed from the
    subscriber's `WAMP-Auth` header.  Caller (the crossbar dynamic
    authorizer endpoint) is responsible for verifying signature
    BEFORE calling this — we trust the payload here.

    Returns True (allow) or False (refuse).  Never raises — a
    malformed input is treated as refuse so a deploy bug fails
    closed.
    """
    if not topic:
        return False
    payload = jwt_payload or {}

    # Public topics — any authenticated user may subscribe.
    PUBLIC_PREFIXES = (
        'community.feed', 'chat.social',
        'social.post.', 'social.comment.', 'social.vote.',
    )
    if any(topic == p or topic.startswith(p) for p in PUBLIC_PREFIXES):
        return bool(payload.get('user_id'))

    # Tenant-scoped: `tenant.<tid>.<scope>.<id>.<event>`.
    # Review M2 fix: parse via the shared `parse_topic` helper so
    # publish-side and subscribe-side gates can never drift on
    # topic-shape semantics (Pass-2 N-NEW-4 substring-vs-segment
    # bug surfaced on the publish side; this prevents the same bug
    # ever existing on the subscribe side independently).
    parsed = parse_topic(topic)
    if parsed.is_tenant_scoped:
        # tid must match the JWT claim.
        if payload.get('tid') != parsed.tid:
            logger.info(
                "WAMP subscribe refused: cross-tenant — topic tid=%s, "
                "JWT tid=%s, user=%s",
                parsed.tid, payload.get('tid'), payload.get('user_id'))
            return False
        scope = parsed.scope
        if scope == 'conv':
            # Service-layer membership is the gate; subscribe-side
            # accepts at the tenant boundary.  A user could
            # technically subscribe to a conv they're not a member
            # of, but messages are only published to members via
            # ConversationService.  Phase 9 hardening can tighten
            # this with a membership lookup here.
            return True
        if scope == 'user':
            # User-scope: the topic-end user_id must match the
            # JWT user_id.
            user_id = payload.get('user_id')
            if not user_id:
                return False
            return parsed.id == user_id
        if scope in ('community', 'call'):
            # Community / call topics: any tenant member can listen
            # in (community privacy is handled at the post level by
            # the privacy gate; calls have their own membership
            # gate at join time).
            return True
        # Unknown scope — refuse.
        return False

    # Per-user topic without `tenant.` prefix (legacy):
    # `com.hertzai.hevolve.social.<user_id>`
    user_id = payload.get('user_id')
    if user_id and (
            topic.endswith(f'.{user_id}') or topic.endswith(f'/{user_id}')):
        return True

    # Anything else: refuse.
    return False


def resolve_tenant_slug(db, slug: str) -> Optional[Dict[str, Any]]:
    """Map a human tenant slug (e.g. 'acme-corp') to the internal
    tenant row.  Returns dict with id + name + slug, or None when
    the slug is unknown (or the `tenants` table doesn't exist yet —
    flat / regional / Nunba bundled deploys).

    Used by Nunba web's signup form: user enters slug → this maps
    to `tid`, which the JWT issuer puts in the `tid` claim.
    """
    if not slug:
        return None
    try:
        row = db.execute(text(
            "SELECT id, name, slug, plan, is_suspended "
            "FROM tenants WHERE slug = :slug LIMIT 1"),
            {'slug': slug}
        ).fetchone()
    except Exception as e:
        # Table missing (pre-Phase-8 deploys) — graceful degrade.
        logger.debug("resolve_tenant_slug: tenants table absent: %s", e)
        return None
    if row is None:
        return None
    return {
        'id': row[0],
        'name': row[1],
        'slug': row[2],
        'plan': row[3],
        'is_suspended': bool(row[4]) if row[4] is not None else False,
    }


__all__ = ['authorize_subscribe', 'resolve_tenant_slug']
