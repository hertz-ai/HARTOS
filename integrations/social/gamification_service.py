"""
HevolveSocial - Gamification Service
Achievements, challenges, seasons, streaks, collectibles.
"""
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import (
    User, Achievement, UserAchievement, Season, Challenge, UserChallenge,
    ResonanceWallet,
)
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')

# ─── Achievement Definitions ───

SEED_ACHIEVEMENTS = [
    # Onboarding
    {'slug': 'welcome', 'name': 'Welcome!', 'description': 'Complete onboarding', 'category': 'onboarding', 'rarity': 'common', 'pulse_reward': 50, 'xp_reward': 25,
     'criteria_json': json.dumps({'type': 'onboarding_complete'})},
    {'slug': 'first_post', 'name': 'First Words', 'description': 'Create your first post', 'category': 'content', 'rarity': 'common', 'pulse_reward': 25, 'xp_reward': 15,
     'criteria_json': json.dumps({'type': 'post_count', 'threshold': 1})},
    {'slug': 'first_comment', 'name': 'Joining the Conversation', 'description': 'Leave your first comment', 'category': 'content', 'rarity': 'common', 'pulse_reward': 15, 'xp_reward': 10,
     'criteria_json': json.dumps({'type': 'comment_count', 'threshold': 1})},
    {'slug': 'first_follow', 'name': 'Connected', 'description': 'Follow your first user or agent', 'category': 'social', 'rarity': 'common', 'pulse_reward': 10, 'xp_reward': 10,
     'criteria_json': json.dumps({'type': 'following_count', 'threshold': 1})},

    # Content milestones
    {'slug': 'prolific_10', 'name': 'Prolific Writer', 'description': 'Create 10 posts', 'category': 'content', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 25, 'xp_reward': 50,
     'criteria_json': json.dumps({'type': 'post_count', 'threshold': 10})},
    {'slug': 'prolific_50', 'name': 'Content Machine', 'description': 'Create 50 posts', 'category': 'content', 'rarity': 'rare', 'pulse_reward': 150, 'spark_reward': 75, 'xp_reward': 150,
     'criteria_json': json.dumps({'type': 'post_count', 'threshold': 50})},
    {'slug': 'prolific_200', 'name': 'Legendary Author', 'description': 'Create 200 posts', 'category': 'content', 'rarity': 'legendary', 'pulse_reward': 500, 'spark_reward': 250, 'xp_reward': 500,
     'criteria_json': json.dumps({'type': 'post_count', 'threshold': 200})},

    # Social milestones
    {'slug': 'popular_10', 'name': 'Getting Noticed', 'description': 'Gain 10 followers', 'category': 'social', 'rarity': 'common', 'pulse_reward': 25, 'xp_reward': 25,
     'criteria_json': json.dumps({'type': 'follower_count', 'threshold': 10})},
    {'slug': 'popular_50', 'name': 'Rising Star', 'description': 'Gain 50 followers', 'category': 'social', 'rarity': 'uncommon', 'pulse_reward': 75, 'spark_reward': 30, 'xp_reward': 75,
     'criteria_json': json.dumps({'type': 'follower_count', 'threshold': 50})},
    {'slug': 'popular_200', 'name': 'Influencer', 'description': 'Gain 200 followers', 'category': 'social', 'rarity': 'rare', 'pulse_reward': 200, 'spark_reward': 100, 'xp_reward': 200,
     'criteria_json': json.dumps({'type': 'follower_count', 'threshold': 200})},
    {'slug': 'popular_1000', 'name': 'Celebrity', 'description': 'Gain 1000 followers', 'category': 'social', 'rarity': 'legendary', 'pulse_reward': 750, 'spark_reward': 500, 'xp_reward': 750,
     'criteria_json': json.dumps({'type': 'follower_count', 'threshold': 1000})},

    # Streak
    {'slug': 'streak_7', 'name': 'Week Warrior', 'description': '7-day login streak', 'category': 'streak', 'rarity': 'uncommon', 'spark_reward': 50, 'xp_reward': 50,
     'criteria_json': json.dumps({'type': 'streak_days', 'threshold': 7})},
    {'slug': 'streak_30', 'name': 'Monthly Devotee', 'description': '30-day login streak', 'category': 'streak', 'rarity': 'rare', 'spark_reward': 200, 'xp_reward': 200,
     'criteria_json': json.dumps({'type': 'streak_days', 'threshold': 30})},
    {'slug': 'streak_100', 'name': 'Centurion', 'description': '100-day login streak', 'category': 'streak', 'rarity': 'legendary', 'spark_reward': 1000, 'xp_reward': 1000,
     'criteria_json': json.dumps({'type': 'streak_days', 'threshold': 100})},

    # Agent-specific
    {'slug': 'agent_creator', 'name': 'Creator', 'description': 'Create your first agent', 'category': 'agent', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 100, 'xp_reward': 100,
     'criteria_json': json.dumps({'type': 'agent_created', 'threshold': 1})},
    {'slug': 'recipe_master', 'name': 'Recipe Master', 'description': 'Share 5 recipes', 'category': 'agent', 'rarity': 'rare', 'spark_reward': 150, 'xp_reward': 150,
     'criteria_json': json.dumps({'type': 'recipe_shared', 'threshold': 5})},

    # Task completion
    {'slug': 'task_completer', 'name': 'Task Doer', 'description': 'Complete 5 tasks', 'category': 'task', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 50, 'xp_reward': 75,
     'criteria_json': json.dumps({'type': 'task_completed', 'threshold': 5})},
    {'slug': 'task_master', 'name': 'Task Master', 'description': 'Complete 50 tasks', 'category': 'task', 'rarity': 'rare', 'pulse_reward': 200, 'spark_reward': 200, 'xp_reward': 300,
     'criteria_json': json.dumps({'type': 'task_completed', 'threshold': 50})},

    # Reputation
    {'slug': 'trusted', 'name': 'Trusted Member', 'description': 'Reach Signal 1.0', 'category': 'reputation', 'rarity': 'uncommon', 'pulse_reward': 50, 'xp_reward': 50,
     'criteria_json': json.dumps({'type': 'signal_threshold', 'threshold': 1.0})},
    {'slug': 'authority', 'name': 'Authority', 'description': 'Reach Signal 5.0', 'category': 'reputation', 'rarity': 'rare', 'pulse_reward': 150, 'xp_reward': 150,
     'criteria_json': json.dumps({'type': 'signal_threshold', 'threshold': 5.0})},
    {'slug': 'pillar', 'name': 'Community Pillar', 'description': 'Reach Signal 20.0', 'category': 'reputation', 'rarity': 'legendary', 'pulse_reward': 500, 'spark_reward': 250, 'xp_reward': 500,
     'criteria_json': json.dumps({'type': 'signal_threshold', 'threshold': 20.0})},

    # Referrals
    {'slug': 'referrer_1', 'name': 'Advocate', 'description': 'Refer 1 activated user', 'category': 'growth', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 100, 'xp_reward': 75,
     'criteria_json': json.dumps({'type': 'referral_activated', 'threshold': 1})},
    {'slug': 'referrer_10', 'name': 'Ambassador', 'description': 'Refer 10 activated users', 'category': 'growth', 'rarity': 'rare', 'pulse_reward': 200, 'spark_reward': 500, 'xp_reward': 300,
     'criteria_json': json.dumps({'type': 'referral_activated', 'threshold': 10})},

    # Campaigns
    {'slug': 'first_campaign', 'name': 'Marketer', 'description': 'Launch your first campaign', 'category': 'campaign', 'rarity': 'uncommon', 'spark_reward': 50, 'xp_reward': 75,
     'criteria_json': json.dumps({'type': 'campaign_launched', 'threshold': 1})},

    # Encounter / bond
    {'slug': 'first_encounter', 'name': 'Serendipity', 'description': 'Your first encounter', 'category': 'social', 'rarity': 'common', 'pulse_reward': 10, 'xp_reward': 15,
     'criteria_json': json.dumps({'type': 'encounter_count', 'threshold': 1})},
    {'slug': 'bond_5', 'name': 'Deep Bond', 'description': 'Reach bond level 5 with someone', 'category': 'social', 'rarity': 'rare', 'pulse_reward': 100, 'xp_reward': 100,
     'criteria_json': json.dumps({'type': 'max_bond_level', 'threshold': 5})},

    # Leveling
    {'slug': 'level_5', 'name': 'Regular', 'description': 'Reach Level 5', 'category': 'leveling', 'rarity': 'common', 'spark_reward': 25, 'xp_reward': 0,
     'criteria_json': json.dumps({'type': 'level', 'threshold': 5})},
    {'slug': 'level_10', 'name': 'Veteran', 'description': 'Reach Level 10', 'category': 'leveling', 'rarity': 'uncommon', 'spark_reward': 100, 'xp_reward': 0,
     'criteria_json': json.dumps({'type': 'level', 'threshold': 10})},
    {'slug': 'level_20', 'name': 'Master', 'description': 'Reach Level 20', 'category': 'leveling', 'rarity': 'rare', 'spark_reward': 300, 'xp_reward': 0,
     'criteria_json': json.dumps({'type': 'level', 'threshold': 20})},
    {'slug': 'level_50', 'name': 'Founding Pillar', 'description': 'Reach Level 50', 'category': 'leveling', 'rarity': 'legendary', 'spark_reward': 2000, 'xp_reward': 0,
     'criteria_json': json.dumps({'type': 'level', 'threshold': 50})},

    # Community
    {'slug': 'community_creator', 'name': 'Community Builder', 'description': 'Create a community', 'category': 'community', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 50, 'xp_reward': 75,
     'criteria_json': json.dumps({'type': 'community_created', 'threshold': 1})},
    {'slug': 'region_pioneer', 'name': 'Regional Pioneer', 'description': 'Join a region', 'category': 'community', 'rarity': 'common', 'pulse_reward': 15, 'xp_reward': 20,
     'criteria_json': json.dumps({'type': 'region_joined', 'threshold': 1})},
    {'slug': 'governor', 'name': 'Governor', 'description': 'Become a region moderator', 'category': 'community', 'rarity': 'rare', 'pulse_reward': 100, 'spark_reward': 100, 'xp_reward': 150,
     'criteria_json': json.dumps({'type': 'region_role', 'role': 'moderator'})},

    # Voting/karma
    {'slug': 'upvotes_100', 'name': 'Appreciated', 'description': 'Receive 100 upvotes', 'category': 'reputation', 'rarity': 'uncommon', 'pulse_reward': 50, 'xp_reward': 50,
     'criteria_json': json.dumps({'type': 'upvotes_received', 'threshold': 100})},
    {'slug': 'upvotes_1000', 'name': 'Beloved', 'description': 'Receive 1000 upvotes', 'category': 'reputation', 'rarity': 'rare', 'pulse_reward': 200, 'spark_reward': 100, 'xp_reward': 200,
     'criteria_json': json.dumps({'type': 'upvotes_received', 'threshold': 1000})},

    # Boosting
    {'slug': 'first_boost', 'name': 'Rocket Fuel', 'description': 'Boost content for the first time', 'category': 'economy', 'rarity': 'common', 'xp_reward': 20,
     'criteria_json': json.dumps({'type': 'boost_count', 'threshold': 1})},
    {'slug': 'big_spender', 'name': 'Big Spender', 'description': 'Spend 1000 Spark on boosts', 'category': 'economy', 'rarity': 'rare', 'pulse_reward': 100, 'xp_reward': 150,
     'criteria_json': json.dumps({'type': 'spark_spent', 'threshold': 1000})},

    # Multiplayer games
    {'slug': 'first_game', 'name': 'Player One', 'description': 'Play your first multiplayer game', 'category': 'game', 'rarity': 'common', 'pulse_reward': 25, 'xp_reward': 20,
     'criteria_json': json.dumps({'type': 'game_count', 'threshold': 1})},
    {'slug': 'games_10', 'name': 'Regular Player', 'description': 'Play 10 games', 'category': 'game', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 25, 'xp_reward': 50,
     'criteria_json': json.dumps({'type': 'game_count', 'threshold': 10})},
    {'slug': 'games_50', 'name': 'Game Veteran', 'description': 'Play 50 games', 'category': 'game', 'rarity': 'rare', 'pulse_reward': 150, 'spark_reward': 75, 'xp_reward': 150,
     'criteria_json': json.dumps({'type': 'game_count', 'threshold': 50})},
    {'slug': 'game_win_streak_5', 'name': 'On Fire', 'description': 'Win 5 games in a row', 'category': 'game', 'rarity': 'rare', 'pulse_reward': 100, 'spark_reward': 50, 'xp_reward': 100,
     'criteria_json': json.dumps({'type': 'game_win_streak', 'threshold': 5})},
    {'slug': 'collab_master', 'name': 'Team Player', 'description': 'Complete 10 collaborative puzzles', 'category': 'game', 'rarity': 'uncommon', 'pulse_reward': 75, 'spark_reward': 50, 'xp_reward': 75,
     'criteria_json': json.dumps({'type': 'collab_puzzle_count', 'threshold': 10})},

    # Compute lending
    {'slug': 'first_compute_share', 'name': 'Sharing is Caring', 'description': 'Enable compute sharing for the first time', 'category': 'compute', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 100, 'xp_reward': 100,
     'criteria_json': json.dumps({'type': 'compute_opt_in'})},
    {'slug': 'compute_1h', 'name': 'First Hour', 'description': 'Contribute 1 GPU-hour to the hive', 'category': 'compute', 'rarity': 'common', 'pulse_reward': 25, 'spark_reward': 50, 'xp_reward': 50,
     'criteria_json': json.dumps({'type': 'compute_gpu_hours', 'threshold': 1})},
    {'slug': 'compute_24h', 'name': 'Day Worker', 'description': 'Contribute 24 GPU-hours', 'category': 'compute', 'rarity': 'uncommon', 'pulse_reward': 100, 'spark_reward': 200, 'xp_reward': 200,
     'criteria_json': json.dumps({'type': 'compute_gpu_hours', 'threshold': 24})},
    {'slug': 'compute_100h', 'name': 'Hive Builder', 'description': 'Contribute 100 GPU-hours', 'category': 'compute', 'rarity': 'rare', 'pulse_reward': 300, 'spark_reward': 500, 'xp_reward': 500,
     'criteria_json': json.dumps({'type': 'compute_gpu_hours', 'threshold': 100})},
    {'slug': 'compute_1000h', 'name': 'Hive Pillar', 'description': 'Contribute 1000 GPU-hours', 'category': 'compute', 'rarity': 'legendary', 'pulse_reward': 1000, 'spark_reward': 2000, 'xp_reward': 2000,
     'criteria_json': json.dumps({'type': 'compute_gpu_hours', 'threshold': 1000})},
    {'slug': 'compute_helped_10', 'name': 'Helper', 'description': 'Your compute helped 10 different users', 'category': 'compute', 'rarity': 'uncommon', 'pulse_reward': 50, 'spark_reward': 75, 'xp_reward': 75,
     'criteria_json': json.dumps({'type': 'compute_users_helped', 'threshold': 10})},
]


class GamificationService:

    @staticmethod
    def seed_achievements(db: Session) -> int:
        """Seed initial achievements if not already present."""
        count = 0
        for ach_data in SEED_ACHIEVEMENTS:
            existing = db.query(Achievement).filter_by(slug=ach_data['slug']).first()
            if not existing:
                ach = Achievement(
                    slug=ach_data['slug'],
                    name=ach_data['name'],
                    description=ach_data['description'],
                    icon_url=ach_data.get('icon_url', ''),
                    category=ach_data.get('category', 'general'),
                    rarity=ach_data.get('rarity', 'common'),
                    pulse_reward=ach_data.get('pulse_reward', 0),
                    spark_reward=ach_data.get('spark_reward', 0),
                    signal_reward=ach_data.get('signal_reward', 0.0),
                    xp_reward=ach_data.get('xp_reward', 0),
                    criteria_json=ach_data.get('criteria_json', '{}'),
                    is_seasonal=ach_data.get('is_seasonal', False),
                )
                db.add(ach)
                count += 1
        if count:
            db.flush()
        return count

    @staticmethod
    def get_all_achievements(db: Session) -> List[Dict]:
        """Get all available achievements."""
        achievements = db.query(Achievement).order_by(Achievement.category, Achievement.name).all()
        return [a.to_dict() for a in achievements]

    @staticmethod
    def get_user_achievements(db: Session, user_id: str) -> List[Dict]:
        """Get achievements unlocked by a user."""
        rows = db.query(UserAchievement, Achievement).join(
            Achievement, Achievement.id == UserAchievement.achievement_id
        ).filter(UserAchievement.user_id == user_id).order_by(
            desc(UserAchievement.unlocked_at)
        ).all()

        result = []
        for ua, ach in rows:
            entry = ach.to_dict()
            entry['unlocked_at'] = ua.unlocked_at.isoformat() if ua.unlocked_at else None
            entry['is_showcased'] = ua.is_showcased
            result.append(entry)
        return result

    @staticmethod
    def unlock_achievement(db: Session, user_id: str, achievement_slug: str) -> Optional[Dict]:
        """Unlock an achievement for a user. Returns achievement dict or None if already unlocked."""
        ach = db.query(Achievement).filter_by(slug=achievement_slug).first()
        if not ach:
            return None

        existing = db.query(UserAchievement).filter_by(
            user_id=user_id, achievement_id=ach.id
        ).first()
        if existing:
            return None

        ua = UserAchievement(
            user_id=user_id,
            achievement_id=ach.id,
            unlocked_at=datetime.utcnow(),
        )
        db.add(ua)
        db.flush()

        # Award resonance rewards
        if ach.pulse_reward:
            ResonanceService.award_pulse(db, user_id, ach.pulse_reward,
                                         'achievement', ach.id, f'Achievement: {ach.name}')
        if ach.spark_reward:
            ResonanceService.award_spark(db, user_id, ach.spark_reward,
                                         'achievement', ach.id, f'Achievement: {ach.name}')
        if ach.signal_reward:
            ResonanceService.award_signal(db, user_id, ach.signal_reward,
                                          'achievement', ach.id, f'Achievement: {ach.name}')
        if ach.xp_reward:
            ResonanceService.award_xp(db, user_id, ach.xp_reward,
                                       'achievement', ach.id, f'Achievement: {ach.name}')

        result = ach.to_dict()
        result['unlocked_at'] = ua.unlocked_at.isoformat()
        return result

    @staticmethod
    def toggle_showcase(db: Session, user_id: str, achievement_id: str) -> Optional[bool]:
        """Toggle showcase flag on a user achievement."""
        ua = db.query(UserAchievement).filter_by(
            user_id=user_id, achievement_id=achievement_id
        ).first()
        if not ua:
            return None
        ua.is_showcased = not ua.is_showcased
        return ua.is_showcased

    @staticmethod
    def check_achievements(db: Session, user_id: str, context: Dict = None) -> List[Dict]:
        """Check and auto-unlock achievements based on current user state.
        Called after significant actions (post, vote, follow, task complete, etc.)."""
        wallet = db.query(ResonanceWallet).filter_by(user_id=user_id).first()
        user = db.query(User).filter_by(id=user_id).first()
        if not user:
            return []

        # Get already unlocked slugs
        unlocked_slugs = set(
            row[0] for row in db.query(Achievement.slug).join(
                UserAchievement, UserAchievement.achievement_id == Achievement.id
            ).filter(UserAchievement.user_id == user_id).all()
        )

        newly_unlocked = []
        all_achievements = db.query(Achievement).filter(
            ~Achievement.slug.in_(unlocked_slugs) if unlocked_slugs else True
        ).all()

        for ach in all_achievements:
            if ach.slug in unlocked_slugs:
                continue

            try:
                criteria = json.loads(ach.criteria_json) if ach.criteria_json else {}
            except (json.JSONDecodeError, TypeError):
                continue

            ctype = criteria.get('type', '')
            threshold = criteria.get('threshold', 0)
            met = False

            if ctype == 'post_count':
                met = (user.post_count or 0) >= threshold
            elif ctype == 'comment_count':
                met = (user.comment_count or 0) >= threshold
            elif ctype == 'follower_count':
                met = (user.follower_count or 0) >= threshold
            elif ctype == 'following_count':
                met = (user.following_count or 0) >= threshold
            elif ctype == 'streak_days' and wallet:
                met = (wallet.streak_days or 0) >= threshold
            elif ctype == 'signal_threshold' and wallet:
                met = (wallet.signal or 0) >= threshold
            elif ctype == 'level' and wallet:
                met = (wallet.level or 1) >= threshold
            elif ctype == 'upvotes_received':
                met = (user.karma_score or 0) >= threshold
            elif ctype == 'onboarding_complete':
                # Check via context
                met = context and context.get('onboarding_complete')

            if met:
                result = GamificationService.unlock_achievement(db, user_id, ach.slug)
                if result:
                    newly_unlocked.append(result)

        return newly_unlocked

    # ─── Challenges ───

    @staticmethod
    def get_active_challenges(db: Session, user_id: str = None) -> List[Dict]:
        """Get currently active challenges."""
        now = datetime.utcnow()
        challenges = db.query(Challenge).filter(
            Challenge.starts_at <= now,
            Challenge.ends_at >= now,
        ).order_by(Challenge.ends_at).all()

        result = []
        for ch in challenges:
            entry = ch.to_dict()
            if user_id:
                uc = db.query(UserChallenge).filter_by(
                    user_id=user_id, challenge_id=ch.id
                ).first()
                if uc:
                    entry['user_progress'] = uc.progress
                    entry['user_target'] = uc.target
                    entry['completed'] = uc.completed_at is not None
                    entry['rewarded'] = uc.rewarded
                else:
                    entry['user_progress'] = 0
                    entry['completed'] = False
            result.append(entry)
        return result

    @staticmethod
    def get_challenge(db: Session, challenge_id: str, user_id: str = None) -> Optional[Dict]:
        """Get challenge details with optional user progress."""
        ch = db.query(Challenge).filter_by(id=challenge_id).first()
        if not ch:
            return None
        entry = ch.to_dict()
        if user_id:
            uc = db.query(UserChallenge).filter_by(
                user_id=user_id, challenge_id=ch.id
            ).first()
            if uc:
                entry['user_progress'] = uc.progress
                entry['user_target'] = uc.target
                entry['completed'] = uc.completed_at is not None
                entry['rewarded'] = uc.rewarded
        return entry

    @staticmethod
    def update_challenge_progress(db: Session, user_id: str,
                                   challenge_id: str, increment: int = 1) -> Optional[Dict]:
        """Update progress on a challenge for a user."""
        ch = db.query(Challenge).filter_by(id=challenge_id).first()
        if not ch:
            return None

        uc = db.query(UserChallenge).filter_by(
            user_id=user_id, challenge_id=ch.id
        ).first()
        if not uc:
            # Auto-join
            try:
                criteria = json.loads(ch.criteria_json) if ch.criteria_json else {}
            except (json.JSONDecodeError, TypeError):
                criteria = {}
            target = criteria.get('target', 10)
            uc = UserChallenge(
                user_id=user_id,
                challenge_id=ch.id,
                progress=0,
                target=target,
            )
            db.add(uc)
            db.flush()

        if uc.completed_at:
            return {'progress': uc.progress, 'target': uc.target, 'completed': True, 'already_complete': True}

        uc.progress += increment
        if uc.progress >= uc.target:
            uc.completed_at = datetime.utcnow()

        return {
            'progress': uc.progress,
            'target': uc.target,
            'completed': uc.completed_at is not None,
        }

    @staticmethod
    def claim_challenge_reward(db: Session, user_id: str,
                                challenge_id: str) -> Optional[Dict]:
        """Claim rewards for a completed challenge."""
        uc = db.query(UserChallenge).filter_by(
            user_id=user_id, challenge_id=challenge_id
        ).first()
        if not uc or not uc.completed_at:
            return None
        if uc.rewarded:
            return {'already_claimed': True}

        ch = db.query(Challenge).filter_by(id=challenge_id).first()
        if not ch:
            return None

        try:
            rewards = json.loads(ch.rewards) if isinstance(ch.rewards, str) else (ch.rewards or {})
        except (json.JSONDecodeError, TypeError):
            rewards = {}

        uc.rewarded = True

        result = {'rewards': rewards}
        if rewards.get('pulse'):
            ResonanceService.award_pulse(db, user_id, rewards['pulse'],
                                         'challenge', ch.id, f'Challenge: {ch.name}')
        if rewards.get('spark'):
            ResonanceService.award_spark(db, user_id, rewards['spark'],
                                         'challenge', ch.id, f'Challenge: {ch.name}')
        if rewards.get('signal'):
            ResonanceService.award_signal(db, user_id, rewards['signal'],
                                          'challenge', ch.id, f'Challenge: {ch.name}')
        if rewards.get('xp'):
            ResonanceService.award_xp(db, user_id, rewards['xp'],
                                       'challenge', ch.id, f'Challenge: {ch.name}')

        return result

    # ─── Seasons ───

    @staticmethod
    def get_current_season(db: Session) -> Optional[Dict]:
        """Get the currently active season."""
        now = datetime.utcnow()
        season = db.query(Season).filter(
            Season.starts_at <= now,
            Season.ends_at >= now,
            Season.is_active == True,
        ).first()
        return season.to_dict() if season else None

    @staticmethod
    def get_season_leaderboard(db: Session, season_id: str,
                                limit: int = 50, offset: int = 0) -> List[Dict]:
        """Get season leaderboard (by season_pulse + season_spark)."""
        rows = db.query(ResonanceWallet, User).join(
            User, User.id == ResonanceWallet.user_id
        ).order_by(
            desc(ResonanceWallet.season_pulse + ResonanceWallet.season_spark)
        ).offset(offset).limit(limit).all()

        result = []
        for i, (wallet, user) in enumerate(rows, start=offset + 1):
            result.append({
                'rank': i,
                'user_id': user.id,
                'username': user.username,
                'display_name': user.display_name,
                'avatar_url': user.avatar_url,
                'season_pulse': wallet.season_pulse,
                'season_spark': wallet.season_spark,
                'level': wallet.level,
                'level_title': wallet.level_title,
            })
        return result

    @staticmethod
    def get_season_achievements(db: Session, season_id: str) -> List[Dict]:
        """Get achievements for a specific season."""
        achievements = db.query(Achievement).filter_by(
            is_seasonal=True, season_id=season_id
        ).all()
        return [a.to_dict() for a in achievements]
