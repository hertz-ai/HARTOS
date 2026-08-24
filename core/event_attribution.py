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
import time
from typing import Optional

from core.constants import MACHINE_GOAL_AUTHORS


def goal_owner_user_id(goal) -> Optional[str]:
    """Canonical goal-owner precedence: owner_id > created_by > user_id.

    SINGLE SOURCE for "who owns this goal" — reused by owner_user_id() below
    AND by the steering bridge (integrations.social.dashboard_service), which
    previously inlined a 2-field subset (owner_id or created_by).  Keeping one
    helper stops the two from drifting.  getattr-safe; returns None if unknown.
    """
    if goal is None:
        return None
    # `created_by` is PROVENANCE, not identity.  For a human-created goal
    # it happens to hold a user id, which is why it sits in the precedence
    # -- but machine-seeded goals put a daemon name there, and returning
    # one yields a "user" that cannot exist.  Skip it so precedence falls
    # THROUGH to user_id rather than stopping on a label.
    #
    # Measured live 2026-08-16: owner_id 0/105 and user_id 0/105 populated,
    # so created_by decided every case -- 'error_advice' x52 and
    # 'system_bootstrap' x32 vs 6 real uuids.  Those 99 goals produced a
    # truthy user_id, PASSED the P3a SSE guard, and were broadcast at a
    # non-existent user: delivered to nobody, nothing logged.  Only the 15
    # with created_by=None hit the visible "refused" warning, so the loud
    # failure was the rare one.  Returning None routes them into that same
    # already-correct refusal path -- no new path, no privacy change.
    created_by = getattr(goal, 'created_by', None)
    if created_by in MACHINE_GOAL_AUTHORS:
        created_by = None
    uid = (getattr(goal, 'owner_id', None)
           or created_by
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

# Zero-user results are re-checked rather than cached (see _sole_local_user_id):
# a fresh appliance has 0 users only until the human signs up, and pinning that
# answer leaves every agent panel dark until a backend restart.  This throttles
# the re-query so the healing path costs at most one DB hit per interval.
_ZERO_USER_RECHECK_S = 60.0
_ZERO_USER_LAST_CHECK = [0.0]


def _sole_local_user_id() -> Optional[str]:
    """The id of the ONLY user on this node, or None if there are 0 or >1.

    Cached after the first resolution: this is called on every agent event
    (agent.action.completed alone runs ~4,882 times a day) and must not add a
    DB round trip per emit.

    The two negatives are cached DIFFERENTLY, on purpose:
      * >1 users  -- stable, cached forever: a multi-tenant node pays one query.
      * 0 users   -- TRANSIENT (a fresh appliance before the human signs up),
                     so it is re-checked every _ZERO_USER_RECHECK_S instead of
                     pinned.  Caching it was what left the agents panel dark
                     after signup until a backend restart.
      * errors    -- never cached: the DB may just be busy, and caching a
                     transient failure would permanently disable per-user
                     routing.
    """
    if _SOLE_USER_CACHE:
        return _SOLE_USER_CACHE[0]
    # A recent zero-user answer: hold it briefly rather than querying per emit.
    last = _ZERO_USER_LAST_CHECK[0]
    if last and (time.monotonic() - last) < _ZERO_USER_RECHECK_S:
        return None
    try:
        from integrations.social.models import db_session, User
        with db_session() as db:
            rows = db.query(User).limit(2).all()
            if len(rows) == 1:
                uid = getattr(rows[0], 'id', None)
                if uid:
                    _SOLE_USER_CACHE.append(str(uid))
                    return str(uid)
            if len(rows) > 1:
                # Genuinely multi-tenant: stable, never falls back.  Cache it.
                _SOLE_USER_CACHE.append(None)
                return None
            # ZERO users is NOT the same negative, and must not be cached the
            # same way.  It is the normal state of a FRESH appliance for the
            # minutes between first boot and the human signing up -- while the
            # daemons are already emitting (agent.action.completed fires from
            # daemon-seeded goals immediately).  Caching it pinned this to None
            # for the life of the process, so the moment the human DID create
            # their account the events kept being refused and the agents panel
            # stayed dark until someone restarted the backend.
            #
            # Measured on the reflashed Samsung box 2026-08-22: users=1,
            # agent_goals=0, and yet 666+ "SSE broadcast refused ... has no
            # user_id" per boot, with the panel showing a reconnect/retry
            # button.  The account existed; the cache still said None.
            #
            # Re-query instead, throttled, so an appliance heals within
            # _ZERO_USER_RECHECK_S of signup while a node nobody ever signs
            # into still costs at most one query per interval -- which keeps
            # the no-DB-round-trip-per-emit property this cache exists for
            # (agent.action.completed alone runs ~4,882 times a day).
            _ZERO_USER_LAST_CHECK[0] = time.monotonic()
    except Exception:
        return None
    return None
