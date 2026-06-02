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
                if g is not None:
                    uid = (getattr(g, 'owner_id', None)
                           or getattr(g, 'created_by', None)
                           or getattr(g, 'user_id', None))
                    if uid:
                        return str(uid)
        except Exception:
            pass

    return None
