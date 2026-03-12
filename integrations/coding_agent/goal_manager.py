"""
HevolveSocial - Coding Goal Manager (Unified Delegate)

Thin wrapper over the unified GoalManager in agent_engine.
All goals are stored as AgentGoal with goal_type='coding'.
Keeps backwards-compatible API for coding_agent.api and coding_daemon.
"""
import re
import logging
from typing import Dict, List

logger = logging.getLogger('hevolve_social')


class CodingGoalManager:
    """Backwards-compatible API. Delegates to unified GoalManager."""

    @staticmethod
    def _sanitize_repo_url(repo_url: str) -> str:
        """Validate and sanitize repo_url to prevent command injection."""
        if not repo_url or not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo_url):
            raise ValueError(f'Invalid repo_url format: {repo_url}')
        if '..' in repo_url:
            raise ValueError('repo_url cannot contain path traversal')
        return repo_url

    @staticmethod
    def _sanitize_branch(branch: str) -> str:
        """Validate branch name to prevent command injection."""
        if not branch or not re.match(r'^[a-zA-Z0-9_./\\-]+$', branch):
            raise ValueError(f'Invalid branch name: {branch}')
        if '..' in branch:
            raise ValueError('Branch name cannot contain path traversal')
        return branch

    @staticmethod
    def create_goal(db, title: str, description: str, repo_url: str,
                    branch: str = 'main', target_path: str = '',
                    created_by: str = None) -> Dict:
        """Create a coding goal via the unified GoalManager."""
        from integrations.agent_engine.goal_manager import GoalManager

        repo_url = CodingGoalManager._sanitize_repo_url(repo_url)
        branch = CodingGoalManager._sanitize_branch(branch)

        result = GoalManager.create_goal(
            db, goal_type='coding', title=title, description=description,
            config={
                'repo_url': repo_url,
                'repo_branch': branch,
                'target_path': target_path,
            },
            created_by=created_by,
        )
        return result.get('goal', result)

    @staticmethod
    def get_goal(db, goal_id: str) -> Dict:
        """Get a single goal."""
        from integrations.agent_engine.goal_manager import GoalManager
        return GoalManager.get_goal(db, goal_id)

    @staticmethod
    def update_goal_status(db, goal_id: str, status: str) -> Dict:
        """Update goal status."""
        from integrations.agent_engine.goal_manager import GoalManager
        return GoalManager.update_goal_status(db, goal_id, status)

    @staticmethod
    def list_goals(db, status: str = None) -> List[Dict]:
        """List coding goals."""
        from integrations.agent_engine.goal_manager import GoalManager
        return GoalManager.list_goals(db, goal_type='coding', status=status)

    @staticmethod
    def build_prompt(goal_dict: dict) -> str:
        """Build prompt via the unified GoalManager prompt builder registry."""
        from integrations.agent_engine.goal_manager import GoalManager

        # Ensure goal_type is set for the builder lookup
        if 'goal_type' not in goal_dict:
            goal_dict = dict(goal_dict, goal_type='coding')
        return GoalManager.build_prompt(goal_dict)
