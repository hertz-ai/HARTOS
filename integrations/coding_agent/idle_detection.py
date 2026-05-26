"""
HevolveSocial - Idle Detection Service

Detects when agents are idle (not serving active user tasks) and manages
opt-in preferences for contributing idle compute to distributed coding.

Worker-thread import-lock guard: the daemon thread that calls
get_idle_opted_in_agents() must NEVER trigger a `from create_recipe …`
or `from lifecycle_hooks …` import.  Both modules drag the heavyweight
langchain / autogen / transformers chain through Python's per-module
import lock, and worker threads waiting on that lock can't release it
when the watchdog "restarts" them — Python has no thread.kill, the
zombie keeps holding the lock, the next attempt blocks behind it, and
the daemon never dispatches a goal (witnessed 2026-04-29: same shape
as the hart_intelligence import deadlock at world_model_bridge.py:299).
We consult sys.modules instead — only consume the module when the
main-thread bootstrap has already finished loading it.
"""
import logging
import sys
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


class IdleDetectionService:
    """Detects idle agents and manages opt-in for distributed coding."""

    @staticmethod
    def is_agent_idle(user_prompt: str) -> bool:
        """Check if a specific agent session is idle.

        An agent is idle when:
        1. Its user_prompt key does NOT exist in user_tasks, OR
        2. It exists but has no active (non-terminated) actions
        """
        # Worker-thread guard: never trigger `from create_recipe import …`
        # here — it acquires the per-module import lock for the heavy
        # langchain/autogen/transformers chain and deadlocks the daemon.
        # Use sys.modules so we only consume the module if it's already
        # loaded by the main-thread bootstrap.  No module loaded yet ⇒
        # treat as idle (same fall-through as the original ImportError
        # branch — caller already handles missing user_tasks gracefully).
        cr_mod = sys.modules.get('create_recipe')
        if cr_mod is None:
            return False
        user_agents = getattr(cr_mod, 'user_agents', None)
        user_tasks = getattr(cr_mod, 'user_tasks', None)
        if user_tasks is None:
            return False

        # Not in user_tasks at all = idle
        if user_prompt not in user_tasks:
            return True

        # In user_tasks but check if the action is finished
        action = user_tasks.get(user_prompt)
        if action is None:
            return True

        # Check lifecycle state.  Same sys.modules guard — lifecycle_hooks
        # is part of the same heavy import chain.
        lh_mod = sys.modules.get('lifecycle_hooks')
        if lh_mod is None:
            return user_agents is None or user_prompt not in user_agents
        action_states = getattr(lh_mod, 'action_states', None)
        ActionState = getattr(lh_mod, 'ActionState', None)
        if action_states is None or ActionState is None:
            return user_agents is None or user_prompt not in user_agents
        states = action_states.get(user_prompt, {})
        if not states:
            return True
        # Idle if ALL actions are TERMINATED or COMPLETED or ERROR
        for key, state in states.items():
            if state not in (ActionState.TERMINATED, ActionState.COMPLETED,
                             ActionState.ERROR):
                return False
        return True

    @staticmethod
    def _check_user_dispatchable(user) -> bool:
        """Shared dispatchability check: not admin-paused AND no active sessions.

        Used by BOTH ``get_idle_opted_in_agents`` (distributed-compute
        privacy contract) AND ``get_idle_agent_personas`` (local agent
        goal dispatch).  Single canonical idle-check — no parallel
        paths (Gate 4 / CLAUDE.md).

        Returns True if the user is eligible to receive work right now.
        """
        # Skip admin-paused entirely.  The `settings` JSON column is
        # the single source of truth; /api/admin/agents/<id>/pause
        # writes it, /resume clears it.
        user_settings = user.settings or {}
        if user_settings.get('paused') is True:
            return False
        # Worker-thread guard (same rationale as ``is_agent_idle``):
        # consult sys.modules instead of ``from create_recipe import …``.
        # If create_recipe isn't loaded yet, treat sessions as idle —
        # same fall-through the original ImportError branch used.  Main
        # thread loads create_recipe lazily; once that finishes,
        # subsequent ticks see a populated ``user_tasks`` and the
        # all_idle gate runs as designed.
        cr_mod = sys.modules.get('create_recipe')
        user_tasks = getattr(cr_mod, 'user_tasks', None) if cr_mod else None
        if user_tasks is None:
            return True
        user_sessions = [k for k in user_tasks.keys()
                         if k.startswith(f'{user.id}_')]
        if not user_sessions:
            return True
        return all(
            IdleDetectionService.is_agent_idle(s) for s in user_sessions
        )

    @staticmethod
    def _user_to_dict(user) -> Dict:
        """Project a User row into the dict shape both consumers expect."""
        return {
            'user_id': user.id,
            'username': user.username,
            'user_type': user.user_type,
        }

    @staticmethod
    def get_idle_opted_in_agents(db: Session) -> List[Dict]:
        """Get all idle agents that have opted in to distributed coding.

        Agents with ``settings.paused == True`` (set via /api/admin/agents/<id>/pause)
        are excluded so an admin-paused agent truly stops receiving work.  This is
        the single enforcement point — no parallel path (Gate 4 / CLAUDE.md).
        """
        from integrations.social.models import User
        """Get all idle users who have OPTED IN to distributed coding.

        **Intent (privacy contract):** humans who explicitly toggled
        ``idle_compute_opt_in=True`` consent to having their idle
        compute used by other Hive nodes.  This is the canonical
        eligibility filter for:

        - ``coding_daemon._loop`` — peer compute sharing
        - ``peer_discovery._self_info`` — gossip advertising of
          available idle capacity

        **Do NOT use this for local agent_daemon goal dispatch** —
        that wants ``get_idle_agent_personas`` instead, which dispatches
        goals to ``user_type='agent'`` rows (Echo, Quest, etc.) without
        gating on the human-consent flag they don't apply to.
        """
        from integrations.social.models import User
        opted_in = db.query(User).filter(
            User.idle_compute_opt_in == True,
        ).all()
        return [
            IdleDetectionService._user_to_dict(u)
            for u in opted_in
            if IdleDetectionService._check_user_dispatchable(u)
        ]

        idle_agents = []
        for user in opted_in:
            # Skip admin-paused agents entirely.  The `settings` JSON column is
            # the single source of truth; /api/admin/agents/<id>/pause writes it,
            # /resume clears it.
            user_settings = user.settings or {}
            if user_settings.get('paused') is True:
                continue
            # Check if any session for this user is idle
            try:
                from create_recipe import user_tasks
                # Sessions are keyed as f'{user_id}_{prompt_id}'
                user_sessions = [k for k in user_tasks.keys()
                                 if k.startswith(f'{user.id}_')]
                all_idle = all(
                    IdleDetectionService.is_agent_idle(s) for s in user_sessions
                ) if user_sessions else True
    @staticmethod
    def get_idle_agent_personas(db: Session) -> List[Dict]:
        """Get all idle ``user_type='agent'`` personas eligible for goal dispatch.

        **Intent:** the agent_daemon dispatches goals (Echo's marketing
        explainer, Quest's contest recap, Contest Curator's idea
        capture, …) to autonomous agent personas.  Those personas are
        DB rows with ``user_type='agent'`` — they exist *to* do work.
        There is no human to consent on their behalf, so the
        ``idle_compute_opt_in`` flag (which gates distributed-compute
        sharing for humans) does NOT apply.

        Eligibility rules:
          1. ``user_type == 'agent'``
          2. NOT admin-paused (``settings.paused != True``)
          3. No active non-terminated sessions (``is_agent_idle`` true)

        Captured 2026-05-01 root-cause: previously the daemon called
        ``get_idle_opted_in_agents``, which silently returned `[]` on
        installs where no user had toggled the human consent flag —
        the seeded marketing personas (Echo/Quest/Contest Curator)
        never dispatched because they weren't gated by the right flag.
        """
        from integrations.social.models import User
        agent_users = db.query(User).filter(
            User.user_type == 'agent',
        ).all()
        return [
            IdleDetectionService._user_to_dict(u)
            for u in agent_users
            if IdleDetectionService._check_user_dispatchable(u)
        ]

    @staticmethod
    def opt_in(db: Session, user_id: str) -> Dict:
        """Opt a user in to contribute idle compute for distributed coding."""
        from integrations.social.models import User

        user = db.query(User).filter_by(id=user_id).first()
        if not user:
            return {'success': False, 'error': 'User not found'}

        user.idle_compute_opt_in = True
        db.flush()
        return {'success': True, 'user_id': user_id, 'idle_compute_opt_in': True}

    @staticmethod
    def opt_out(db: Session, user_id: str) -> Dict:
        """Opt a user out of contributing idle compute."""
        from integrations.social.models import User

        user = db.query(User).filter_by(id=user_id).first()
        if not user:
            return {'success': False, 'error': 'User not found'}

        user.idle_compute_opt_in = False
        db.flush()
        return {'success': True, 'user_id': user_id, 'idle_compute_opt_in': False}

    @staticmethod
    def get_idle_stats(db: Session) -> Dict:
        """Get idle agent statistics for this node."""
        from integrations.social.models import User

        total_opted_in = db.query(User).filter(
            User.idle_compute_opt_in == True,
        ).count()

        idle_agents = IdleDetectionService.get_idle_opted_in_agents(db)

        return {
            'total_opted_in': total_opted_in,
            'currently_idle': len(idle_agents),
            'idle_agents': idle_agents,
        }
