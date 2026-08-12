"""Resolve the owning user_id for agent/goal/memory-scoped EventBus events (#58).

The P3a SSE guard refuses topics with no user_id, so per-agent activity can't
leak cross-user on a multi-tenant node — which silently dropped ~5,200/day of
real per-agent dashboard updates (agent.action.completed, action_state.changed,
memory.item_added) because their publishers never stamped the owner.

These events DO have an owning user; it just wasn't included.  This resolves it
from whatever context the publisher holds, so the EXISTING guard routes the
event to that user's SSE (admin subscribes with elevated scope and still sees
all).  Returns None when no owner is determinable (e.g. genuinely infra-level
events like inference.completed) — the guard then keeps refusing it on SSE,
exactly as before: no regression, no leak.

Single source: every publisher that needs per-user routing calls this, rather
than each re-deriving the owner.
"""
from typing import Optional


def goal_owner_user_id(goal) -> Optional[str]:
    """Canonical goal-owner precedence: owner_id > created_by > user_id.

    SINGLE SOURCE for "who owns this goal" — reused by owner_user_id() below
    AND by the steering bridge (integrations.social.dashboard_service), which
    previously inlined a 2-field subset (owner_id or created_by).  Keeping one
    helper stops the two from drifting.  getattr-safe; returns None if unknown.
    """
    if goal is None:
        return None
    uid = (getattr(goal, 'owner_id', None)
           or getattr(goal, 'created_by', None)
           or getattr(goal, 'user_id', None))
    return str(uid) if uid else None


def owner_user_id(user_prompt=None, goal_id=None, metadata=None) -> Optional[str]:
    """Best-effort owning user_id from a publisher's available context.

    - user_prompt: the canonical ``"{user_id}_{prompt_id}"`` key (action_state).
    - metadata:    a dict that may carry ``user_id`` (memory items).
    - goal_id:     an AgentGoal id → its owner (agent action completion).

    Never raises; returns None if nothing resolves.
    """
    # 1. "{user_id}_{prompt_id}" — cheap, no DB.
    if isinstance(user_prompt, str) and '_' in user_prompt:
        uid = user_prompt.split('_', 1)[0].strip()
        if uid:
            return uid

    # 2. metadata carrying an explicit user_id.
    if isinstance(metadata, dict):
        uid = metadata.get('user_id')
        if uid:
            return str(uid)

    # 3. goal owner via the AgentGoal row.
    if goal_id:
        try:
            from integrations.social.models import db_session, AgentGoal
            with db_session() as db:
                g = db.query(AgentGoal).filter(
                    AgentGoal.id == str(goal_id)).first()
                uid = goal_owner_user_id(g)
                if uid:
                    return uid
        except Exception:
            pass

    # 4. Single-tenant fallback (HART OS appliance).  The P3a guard exists to
    #    stop per-agent activity leaking ACROSS users on a multi-tenant node.
    #    When the node has exactly ONE user there is no other user to leak to,
    #    so the owner is unambiguous and returning it is safe by construction.
    #    Nodes with 0 or >1 users fall through to None exactly as before, so
    #    multi-tenant behaviour is unchanged.
    #
    #    Why this is needed: measured on the live appliance 2026-08-12,
    #    agent_goals is EMPTY (0 rows), so step 3 above always resolves None and
    #    EVERY agent.action.completed was refused -- 266 of them in two hours --
    #    with a warning that blamed a missing user_id rather than an empty goals
    #    table.  That is what left the agents panel showing "will appear once the
    #    connection is restored".  Do NOT "fix" this by adding 'agent.' to
    #    _SSE_GLOBAL_PREFIXES: the comment at core/platform/events.py:112 forbids
    #    exactly that, because it would leak cross-user activity metadata.
    uid = _sole_local_user_id()
    if uid:
        return uid

    return None


_SOLE_USER_CACHE = []          # [] unknown, [None] known-not-single, [uid] known


def _sole_local_user_id() -> Optional[str]:
    """The id of the ONLY user on this node, or None if there are 0 or >1.

    Cached after the first resolution: this is called on every agent event
    (agent.action.completed alone runs ~4,882 times a day) and must not add a
    DB round trip per emit.  A negative result is cached too, so a genuinely
    multi-tenant node pays exactly one query.  Errors are NOT cached -- the DB
    may just be busy, and caching a transient failure would permanently disable
    per-user routing.
    """
    if _SOLE_USER_CACHE:
        return _SOLE_USER_CACHE[0]
    try:
        from integrations.social.models import db_session, User
        with db_session() as db:
            rows = db.query(User).limit(2).all()
            if len(rows) == 1:
                uid = getattr(rows[0], 'id', None)
                if uid:
                    _SOLE_USER_CACHE.append(str(uid))
                    return str(uid)
            # 0 or >1 users: never fall back.  Cache the negative.
            _SOLE_USER_CACHE.append(None)
    except Exception:
        return None
    return _SOLE_USER_CACHE[0]
