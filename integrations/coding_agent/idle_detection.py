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
    def get_idle_opted_in_agents(db: Session) -> List[Dict]:
        """Get all idle agents that have opted in to distributed coding.

        Agents with ``settings.paused == True`` (set via /api/admin/agents/<id>/pause)
        are excluded so an admin-paused agent truly stops receiving work.  This is
        the single enforcement point — no parallel path (Gate 4 / CLAUDE.md).
        """
        from integrations.social.models import User

        opted_in = db.query(User).filter(
            User.idle_compute_opt_in == True,
        ).all()

        idle_agents = []
        for user in opted_in:
            # Skip admin-paused agents entirely.  The `settings` JSON column is
            # the single source of truth; /api/admin/agents/<id>/pause writes it,
            # /resume clears it.
            user_settings = user.settings or {}
            if user_settings.get('paused') is True:
                continue
            # Worker-thread guard (same rationale as is_agent_idle above):
            # consult sys.modules instead of `from create_recipe import …`.
            # If create_recipe isn't loaded yet, treat the user's sessions
            # as idle (same fall-through the original ImportError branch
            # used).  The main-thread bootstrap loads create_recipe lazily
            # — once that finishes, subsequent ticks see a populated
            # user_tasks and the all_idle gate runs as designed.
            cr_mod = sys.modules.get('create_recipe')
            user_tasks = getattr(cr_mod, 'user_tasks', None) if cr_mod else None
            if user_tasks is None:
                idle_agents.append({
                    'user_id': user.id,
                    'username': user.username,
                    'user_type': user.user_type,
                })
                continue
            # Sessions are keyed as f'{user_id}_{prompt_id}'
            user_sessions = [k for k in user_tasks.keys()
                             if k.startswith(f'{user.id}_')]
            all_idle = all(
                IdleDetectionService.is_agent_idle(s) for s in user_sessions
            ) if user_sessions else True
            if all_idle:
                idle_agents.append({
                    'user_id': user.id,
                    'username': user.username,
                    'user_type': user.user_type,
                })

        return idle_agents

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
