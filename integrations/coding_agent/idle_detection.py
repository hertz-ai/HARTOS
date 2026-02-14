"""
HevolveSocial - Idle Detection Service

Detects when agents are idle (not serving active user tasks) and manages
opt-in preferences for contributing idle compute to distributed coding.
"""
import logging
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
        try:
            from create_recipe import user_agents, user_tasks
        except ImportError:
            return False

        # Not in user_tasks at all = idle
        if user_prompt not in user_tasks:
            return True

        # In user_tasks but check if the action is finished
        action = user_tasks.get(user_prompt)
        if action is None:
            return True

        # Check lifecycle state
        try:
            from lifecycle_hooks import action_states, ActionState
            states = action_states.get(user_prompt, {})
            if not states:
                return True
            # Idle if ALL actions are TERMINATED or COMPLETED
            for key, state in states.items():
                if state not in (ActionState.TERMINATED, ActionState.COMPLETED,
                                 ActionState.ERROR):
                    return False
            return True
        except ImportError:
            # If lifecycle_hooks unavailable, check user_agents presence
            return user_prompt not in user_agents

    @staticmethod
    def get_idle_opted_in_agents(db: Session) -> List[Dict]:
        """Get all idle agents that have opted in to distributed coding."""
        from integrations.social.models import User

        opted_in = db.query(User).filter(
            User.idle_compute_opt_in == True,
        ).all()

        idle_agents = []
        for user in opted_in:
            # Check if any session for this user is idle
            try:
                from create_recipe import user_tasks
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
            except ImportError:
                # If create_recipe not available, consider all opted-in as idle
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
