"""
HevolveSocial — Per-post privacy gate.

Phase 7c.5.  Plan reference: sunny-gliding-eich.md, Part E.10.

Privacy levels:
  - 'public'    visible to everyone (default; existing rows = NULL = public)
  - 'friends'   visible only to author + active-friends of author
  - 'community' visible only to author + members of post.community_id
  - 'private'   visible only to author

NULL is normalised to 'public' for backward compatibility — every
post that existed before v48 stays exactly as visible as it was.

Two surfaces:
  - can_view_post(db, viewer_user, post)
        Single-post authoritative check.  Used by GET /posts/<id>.
  - visible_posts_filter(viewer_user)
        SQLAlchemy filter expression for use in list queries
        (PostService.list_posts).  Pre-filters at SQL for performance.

Hidden / soft-deleted posts are still gated by is_hidden / is_deleted
in the caller.  privacy.py does NOT override moderator hide or
soft delete; it only enforces the user-set privacy level.

Transport:  N/A — read-only synchronous helpers used by api.py
endpoints.  No fan-out implications: privacy is applied at READ
time, not at write time, so existing MessageBus.publish call sites
need no changes.

Block interaction:  separate concern.  The Block table tears down
the PeerLink trust ratchet (Plan R.5) and is enforced by
FriendService.is_friend (which returns False for blocked pairs).
A blocked viewer still falls through public_clause for public
posts; explicit feed-level Block filtering is a 7c.6 follow-up
tracked in Plan W (the existing services.py FollowService and
encounter visibility paths already do this for their surfaces).
"""

import logging

from sqlalchemy import or_, text

logger = logging.getLogger('hevolve_social')


PRIVACY_LEVELS = ('public', 'friends', 'community', 'private')


def _normalize(level):
    """Bad input → 'public'.  Used by both surfaces below so the
    NULL-means-public invariant lives in exactly one place.
    """
    if not level:
        return 'public'
    if level not in PRIVACY_LEVELS:
        return 'public'
    return level


def can_view_post(db, viewer_user, post) -> bool:
    """Authoritative single-post check.  viewer_user may be None.

    Returns True iff the viewer is allowed to see this specific post
    given its privacy level + the viewer's relationship to the author.

    Order of checks (cheapest first):
      1. Public  → True (everyone, including anonymous).
      2. Author  → True (always sees own posts at any level).
      3. Private → False (no friend / community escape hatch).
      4. Friends → FriendService.is_friend(viewer, author).
      5. Community → membership lookup against post.community_id.
    """
    level = _normalize(getattr(post, 'privacy', None))

    if level == 'public':
        return True

    if viewer_user is None:
        return False

    if post.author_id == viewer_user.id:
        return True

    if level == 'private':
        return False

    if level == 'friends':
        try:
            from .friend_service import FriendService
            return FriendService.is_friend(db, viewer_user.id, post.author_id)
        except Exception as e:
            logger.warning("privacy.can_view_post friends check failed: %s", e)
            return False

    if level == 'community':
        if not post.community_id:
            # Misconfigured: community-scope on a post without a
            # community.  Fail safe — treat as private.
            return False
        try:
            row = db.execute(text(
                "SELECT 1 FROM memberships "
                "WHERE parent_kind = 'community' AND parent_id = :cid "
                "AND member_id = :uid LIMIT 1"),
                {'cid': post.community_id, 'uid': viewer_user.id}
            ).fetchone()
            return row is not None
        except Exception as e:
            logger.warning(
                "privacy.can_view_post community check failed: %s", e)
            return False

    # Defensive — _normalize should have collapsed unknown levels to
    # 'public'.  Anything reaching here is a programmer bug.
    return False


def visible_posts_filter(viewer_user):
    """Return a SQLAlchemy filter expression to AND into a Post query.

    Pre-filters at SQL for performance: cuts the candidate set before
    pagination so we don't fetch 1000 posts to filter to 50.  The
    single-post check (can_view_post) remains the canonical authority
    and should be re-applied at handler level for edge cases (e.g.,
    a post that becomes inaccessible between query and serve).

    Anonymous viewer (None) → public-only.

    Authenticated viewer → public OR own OR (friends + active friend)
    OR (community + membership row exists).  Friendship + Membership
    are not ORM models, so the friends/community arms use raw SQL
    EXISTS subqueries with bound parameters (no string interpolation
    of user-controlled data — the bind layer escapes everything).
    """
    from .models import Post

    public_clause = or_(Post.privacy.is_(None), Post.privacy == 'public')
    if viewer_user is None:
        return public_clause

    vid = viewer_user.id

    # 'friends' arm: status='active' on the sorted (a,b) row that
    # contains both the post author and the viewer.  Two arms because
    # author may be on either side of the sorted pair.  The
    # `author_id <> :viewer` predicate makes the arms mutually
    # exclusive with own_clause so the EXISTS subquery doesn't fire
    # for the viewer's own posts (P3-07 reviewer fix).
    friends_arm = text(
        "(posts.privacy = 'friends' "
        " AND posts.author_id <> :__priv_vid_friends "
        " AND EXISTS ("
        "  SELECT 1 FROM friendships f "
        "  WHERE f.status = 'active' "
        "  AND ((f.user_a_id = posts.author_id "
        "        AND f.user_b_id = :__priv_vid_friends) "
        "    OR (f.user_b_id = posts.author_id "
        "        AND f.user_a_id = :__priv_vid_friends))))"
    ).bindparams(__priv_vid_friends=vid)

    # 'community' arm: viewer must have a memberships row for this
    # post's community.  community_id may be NULL (cross-posted /
    # personal feed posts) — we deliberately do not match those: a
    # post tagged 'community' must have a community_id, otherwise
    # it's misconfigured and should not surface.
    # Same `<> :viewer` exclusion as friends_arm for arm-disjointness.
    # Memberships rows are hard-deleted on community leave (no
    # left_at column today — P3-09 contract: if memberships ever
    # grows a soft-leave column, this EXISTS must be tightened).
    # Tenant scoping is applied by the existing tenant_filter listener
    # on the outer Post query; the EXISTS subqueries are NOT
    # tenant-filtered (memberships and friendships are global tables;
    # cross-tenant friendship/membership is impossible by other
    # invariants).
    community_arm = text(
        "(posts.privacy = 'community' AND posts.community_id IS NOT NULL "
        " AND posts.author_id <> :__priv_vid_community "
        " AND EXISTS ("
        "  SELECT 1 FROM memberships m "
        "  WHERE m.parent_kind = 'community' "
        "  AND m.parent_id = posts.community_id "
        "  AND m.member_id = :__priv_vid_community))"
    ).bindparams(__priv_vid_community=vid)

    own_clause = Post.author_id == vid

    # Note: 'private' is covered by own_clause only (author can always
    # see their own posts at any level).  An explicit private branch
    # is unnecessary and would just duplicate own_clause.
    return or_(public_clause, own_clause, friends_arm, community_arm)


__all__ = ['PRIVACY_LEVELS', 'can_view_post', 'visible_posts_filter']
