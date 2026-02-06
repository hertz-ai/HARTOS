"""
HevolveSocial - Coding Goal Manager

Simple CRUD for coding goals. The actual task decomposition, execution,
and result tracking is handled by the existing CREATE/REUSE agent pipeline
via /chat — this just tracks what repo/objective to work on.
"""
import re
import logging
from typing import Dict, List
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


class CodingGoalManager:
    """CRUD for coding goals. All execution goes through /chat."""

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
        if not branch or not re.match(r'^[a-zA-Z0-9_./\-]+$', branch):
            raise ValueError(f'Invalid branch name: {branch}')
        if '..' in branch:
            raise ValueError('Branch name cannot contain path traversal')
        return branch

    @staticmethod
    def create_goal(db: Session, title: str, description: str, repo_url: str,
                    branch: str = 'main', target_path: str = '',
                    created_by: str = None) -> Dict:
        """Create a new coding goal."""
        from integrations.social.models import CodingGoal

        repo_url = CodingGoalManager._sanitize_repo_url(repo_url)
        branch = CodingGoalManager._sanitize_branch(branch)

        goal = CodingGoal(
            title=title,
            description=description,
            repo_url=repo_url,
            repo_branch=branch,
            target_path=target_path,
            status='active',
            created_by=created_by,
        )
        db.add(goal)
        db.flush()
        return goal.to_dict()

    @staticmethod
    def get_goal(db: Session, goal_id: str) -> Dict:
        """Get a single goal."""
        from integrations.social.models import CodingGoal

        goal = db.query(CodingGoal).filter_by(id=goal_id).first()
        if not goal:
            return {'success': False, 'error': 'Goal not found'}
        return {'success': True, 'goal': goal.to_dict()}

    @staticmethod
    def update_goal_status(db: Session, goal_id: str, status: str) -> Dict:
        """Update goal status."""
        from integrations.social.models import CodingGoal

        goal = db.query(CodingGoal).filter_by(id=goal_id).first()
        if not goal:
            return {'success': False, 'error': 'Goal not found'}

        goal.status = status
        db.flush()
        return {'success': True, 'goal': goal.to_dict()}

    @staticmethod
    def list_goals(db: Session, status: str = None) -> List[Dict]:
        """List coding goals, optionally filtered by status."""
        from integrations.social.models import CodingGoal

        q = db.query(CodingGoal)
        if status:
            q = q.filter_by(status=status)
        return [g.to_dict() for g in q.order_by(CodingGoal.created_at.desc()).all()]

    @staticmethod
    def build_prompt(goal_dict: dict) -> str:
        """Build a /chat prompt from a goal. The CREATE agent handles decomposition."""
        return (
            f"You are working on the GitHub repository {goal_dict['repo_url']} "
            f"(branch {goal_dict.get('repo_branch', 'main')}).\n"
            f"Target path: {goal_dict.get('target_path', '(entire repo)')}\n\n"
            f"Goal: {goal_dict['title']}\n"
            f"Description: {goal_dict.get('description', '')}\n\n"
            f"Clone the repo, analyze the codebase, and make improvements "
            f"aligned with the goal above. Focus on code quality, bug fixes, "
            f"and missing implementations."
        )
