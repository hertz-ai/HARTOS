"""
HevolveSocial - Onboarding Service
Progressive 7-step onboarding with auto-advancement and rewards.
"""
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List

from sqlalchemy.orm import Session

from .models import User, OnboardingProgress
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')

ONBOARDING_STEPS = [
    {'key': 'welcome', 'title': 'Welcome', 'reward_type': 'pulse', 'reward_amount': 50},
    {'key': 'first_follow', 'title': 'Follow Someone', 'reward_type': 'pulse', 'reward_amount': 25},
    {'key': 'join_community', 'title': 'Join a Community', 'reward_type': 'pulse', 'reward_amount': 50},
    {'key': 'first_vote', 'title': 'Vote on a Post', 'reward_type': 'spark', 'reward_amount': 25},
    {'key': 'first_comment', 'title': 'Leave a Comment', 'reward_type': 'spark', 'reward_amount': 25},
    {'key': 'first_post', 'title': 'Create a Post', 'reward_type': 'xp', 'reward_amount': 100},
    {'key': 'explore_agents', 'title': 'Explore Agents', 'reward_type': 'xp', 'reward_amount': 100},
    {'key': 'invite_friends', 'title': 'Invite Friends', 'reward_type': 'spark', 'reward_amount': 100},
]


class OnboardingService:

    @staticmethod
    def get_or_create_progress(db: Session, user_id: str) -> OnboardingProgress:
        progress = db.query(OnboardingProgress).filter_by(user_id=user_id).first()
        if not progress:
            progress = OnboardingProgress(
                user_id=user_id,
                steps_completed='{}',
                current_step='welcome',
            )
            db.add(progress)
            db.flush()
        return progress

    @staticmethod
    def get_progress(db: Session, user_id: str) -> Dict:
        progress = OnboardingService.get_or_create_progress(db, user_id)
        try:
            steps = json.loads(progress.steps_completed) if progress.steps_completed else {}
        except (json.JSONDecodeError, TypeError):
            steps = {}

        total = len(ONBOARDING_STEPS)
        done = sum(1 for s in ONBOARDING_STEPS if steps.get(s['key']))

        return {
            'user_id': user_id,
            'steps_completed': steps,
            'current_step': progress.current_step,
            'completed_at': progress.completed_at.isoformat() if progress.completed_at else None,
            'tutorial_dismissed': progress.tutorial_dismissed,
            'total_steps': total,
            'done_steps': done,
            'steps': [{
                'key': s['key'],
                'title': s['title'],
                'completed': bool(steps.get(s['key'])),
                'reward_type': s['reward_type'],
                'reward_amount': s['reward_amount'],
            } for s in ONBOARDING_STEPS],
        }

    @staticmethod
    def complete_step(db: Session, user_id: str, step_key: str) -> Dict:
        """Mark an onboarding step as complete and award rewards."""
        progress = OnboardingService.get_or_create_progress(db, user_id)
        try:
            steps = json.loads(progress.steps_completed) if progress.steps_completed else {}
        except (json.JSONDecodeError, TypeError):
            steps = {}

        if steps.get(step_key):
            return {'already_completed': True, 'step': step_key}

        # Find step definition
        step_def = next((s for s in ONBOARDING_STEPS if s['key'] == step_key), None)
        if not step_def:
            return {'error': 'Unknown step'}

        steps[step_key] = datetime.utcnow().isoformat()
        progress.steps_completed = json.dumps(steps)

        # Track specific timestamps
        timestamp_map = {
            'first_post': 'first_post_at',
            'first_comment': 'first_comment_at',
            'first_vote': 'first_vote_at',
            'first_follow': 'first_follow_at',
            'join_community': 'first_community_join_at',
        }
        attr = timestamp_map.get(step_key)
        if attr and hasattr(progress, attr) and not getattr(progress, attr):
            setattr(progress, attr, datetime.utcnow())

        # Award reward
        reward_result = {}
        rtype = step_def['reward_type']
        ramount = step_def['reward_amount']
        if rtype == 'pulse':
            ResonanceService.award_pulse(db, user_id, ramount, 'onboarding', step_key,
                                         f'Onboarding: {step_def["title"]}')
            reward_result = {'pulse': ramount}
        elif rtype == 'spark':
            ResonanceService.award_spark(db, user_id, ramount, 'onboarding', step_key,
                                         f'Onboarding: {step_def["title"]}')
            reward_result = {'spark': ramount}
        elif rtype == 'xp':
            ResonanceService.award_xp(db, user_id, ramount, 'onboarding', step_key,
                                       f'Onboarding: {step_def["title"]}')
            reward_result = {'xp': ramount}

        # Advance current step
        all_done = all(steps.get(s['key']) for s in ONBOARDING_STEPS)
        if all_done:
            progress.completed_at = datetime.utcnow()
            progress.current_step = 'complete'
            # Bonus for completing all steps
            ResonanceService.award_spark(db, user_id, 50, 'onboarding', 'complete',
                                         'Onboarding complete bonus')
            reward_result['completion_bonus'] = {'spark': 50}
        else:
            # Find next incomplete step
            for s in ONBOARDING_STEPS:
                if not steps.get(s['key']):
                    progress.current_step = s['key']
                    break

        return {
            'step': step_key,
            'completed': True,
            'rewards': reward_result,
            'all_complete': all_done,
            'next_step': progress.current_step,
        }

    @staticmethod
    def dismiss(db: Session, user_id: str) -> bool:
        progress = OnboardingService.get_or_create_progress(db, user_id)
        progress.tutorial_dismissed = True
        return True

    @staticmethod
    def get_suggestion(db: Session, user_id: str) -> Optional[Dict]:
        """Get the next suggested action for onboarding."""
        progress = OnboardingService.get_or_create_progress(db, user_id)
        if progress.completed_at or progress.tutorial_dismissed:
            return None

        try:
            steps = json.loads(progress.steps_completed) if progress.steps_completed else {}
        except (json.JSONDecodeError, TypeError):
            steps = {}

        for s in ONBOARDING_STEPS:
            if not steps.get(s['key']):
                return {
                    'step': s['key'],
                    'title': s['title'],
                    'reward_type': s['reward_type'],
                    'reward_amount': s['reward_amount'],
                }
        return None

    @staticmethod
    def auto_advance(db: Session, user_id: str, action: str):
        """Auto-advance onboarding based on natural user actions.
        Called by service hooks after relevant actions."""
        action_to_step = {
            'follow': 'first_follow',
            'join_community': 'join_community',
            'vote': 'first_vote',
            'comment': 'first_comment',
            'post': 'first_post',
            'view_agents': 'explore_agents',
            'share_referral': 'invite_friends',
        }
        step_key = action_to_step.get(action)
        if step_key:
            progress = db.query(OnboardingProgress).filter_by(user_id=user_id).first()
            if progress and not progress.completed_at:
                try:
                    steps = json.loads(progress.steps_completed) if progress.steps_completed else {}
                except (json.JSONDecodeError, TypeError):
                    steps = {}
                if not steps.get(step_key):
                    OnboardingService.complete_step(db, user_id, step_key)
