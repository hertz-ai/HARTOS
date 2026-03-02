"""
HevolveSocial - Resonance Engine
Multi-dimensional reward system: Pulse, Spark, Signal, XP.
"""
import logging
import math
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import (
    User, ResonanceWallet, ResonanceTransaction
)

logger = logging.getLogger('hevolve_social')

# ─── Level/Title System ───

LEVEL_TITLES = {
    1: 'Newcomer', 3: 'Contributor', 5: 'Regular', 8: 'Established',
    10: 'Veteran', 15: 'Expert', 20: 'Master', 25: 'Luminary',
    30: 'Legend', 40: 'Architect', 50: 'Founding Pillar',
}


def xp_for_level(n: int) -> int:
    return int(100 * (1.15 ** (n - 1)))


def title_for_level(level: int) -> str:
    title = 'Newcomer'
    for threshold, t in sorted(LEVEL_TITLES.items()):
        if level >= threshold:
            title = t
    return title


# ─── Award Tables ───

AWARD_TABLE = {
    'post_upvote':      {'pulse': 1, 'xp': 2},
    'create_post':      {'spark': 5, 'signal': 0.01, 'xp': 10},
    'create_comment':   {'spark': 2, 'signal': 0.005, 'xp': 5},
    'complete_task':    {'pulse': 10, 'spark': 20, 'signal': 0.1, 'xp': 50},
    'recipe_shared':    {'spark': 15, 'signal': 0.05, 'xp': 25},
    'recipe_forked':    {'pulse': 5, 'spark': 10, 'signal': 0.02, 'xp': 15},
    'referral_activated': {'pulse': 20, 'spark': 50, 'signal': 0.1, 'xp': 100},
    'correct_moderation': {'signal': 0.05, 'xp': 10},
    'campaign_milestone': {'pulse': 5, 'spark': 25, 'signal': 0.03, 'xp': 30},
    'ad_impression_served': {'spark': 1},
    'hosting_uptime_bonus': {'spark': 10, 'pulse': 5, 'xp': 20},
    'hosting_milestone': {'spark': 50, 'pulse': 25, 'xp': 100},
    'learning_contribution':    {'spark': 25, 'pulse': 10, 'signal': 0.08, 'xp': 40},
    'learning_skill_shared':    {'spark': 15, 'signal': 0.05, 'xp': 25},
    'learning_credit_assigned': {'spark': 5, 'xp': 10},
    'experiment_proposed':      {'spark': 20, 'pulse': 10, 'signal': 0.10, 'xp': 30},
    'experiment_voted':         {'spark': 10, 'pulse': 5, 'xp': 15},
    'experiment_evaluated':     {'spark': 30, 'signal': 0.08, 'xp': 50},
    'experiment_suggestion':    {'spark': 15, 'signal': 0.05, 'xp': 20},
    # Multiplayer games
    'game_win':                 {'pulse': 15, 'spark': 10, 'xp': 30},
    'game_participate':         {'pulse': 5, 'spark': 5, 'xp': 15},
    'game_streak_3':            {'pulse': 10, 'spark': 15, 'xp': 25},
    'multiplayer_encounter':    {'pulse': 5, 'xp': 10},
    # Compute lending
    'compute_opt_in':           {'pulse': 25, 'spark': 50, 'xp': 50},
    'compute_hour':             {'spark': 10, 'xp': 20},
    'compute_day_streak':       {'spark': 25, 'pulse': 10, 'xp': 40},
}


class ResonanceService:

    @staticmethod
    def get_or_create_wallet(db: Session, user_id: str) -> ResonanceWallet:
        wallet = db.query(ResonanceWallet).filter_by(user_id=user_id).first()
        if not wallet:
            wallet = ResonanceWallet(user_id=user_id)
            db.add(wallet)
            db.flush()
        return wallet

    @staticmethod
    def get_wallet(db: Session, user_id: str) -> Optional[Dict]:
        wallet = db.query(ResonanceWallet).filter_by(user_id=user_id).first()
        if wallet:
            return wallet.to_dict()
        return None

    @staticmethod
    def _log_transaction(db: Session, user_id: str, currency: str,
                         amount: float, balance_after: float,
                         source_type: str, source_id: str = None,
                         description: str = ''):
        txn = ResonanceTransaction(
            user_id=user_id, currency=currency,
            amount=amount, balance_after=balance_after,
            source_type=source_type, source_id=source_id,
            description=description,
        )
        db.add(txn)

    @staticmethod
    def award_pulse(db: Session, user_id: str, amount: int,
                    source_type: str, source_id: str = None,
                    description: str = '') -> int:
        wallet = ResonanceService.get_or_create_wallet(db, user_id)
        wallet.pulse += amount
        wallet.season_pulse += amount
        ResonanceService._log_transaction(
            db, user_id, 'pulse', amount, wallet.pulse,
            source_type, source_id, description)
        return wallet.pulse

    @staticmethod
    def award_spark(db: Session, user_id: str, amount: int,
                    source_type: str, source_id: str = None,
                    description: str = '') -> int:
        wallet = ResonanceService.get_or_create_wallet(db, user_id)
        wallet.spark += amount
        wallet.spark_lifetime += amount
        wallet.season_spark += amount
        ResonanceService._log_transaction(
            db, user_id, 'spark', amount, wallet.spark,
            source_type, source_id, description)
        return wallet.spark

    @staticmethod
    def spend_spark(db: Session, user_id: str, amount: int,
                    source_type: str, source_id: str = None,
                    description: str = '') -> Tuple[bool, int]:
        wallet = ResonanceService.get_or_create_wallet(db, user_id)
        if wallet.spark < amount:
            return False, wallet.spark
        wallet.spark -= amount
        ResonanceService._log_transaction(
            db, user_id, 'spark', -amount, wallet.spark,
            source_type, source_id, description)
        return True, wallet.spark

    @staticmethod
    def award_signal(db: Session, user_id: str, amount: float,
                     source_type: str, source_id: str = None,
                     description: str = '') -> float:
        wallet = ResonanceService.get_or_create_wallet(db, user_id)
        wallet.signal += amount
        wallet.signal_last_decay = datetime.utcnow()
        ResonanceService._log_transaction(
            db, user_id, 'signal', amount, wallet.signal,
            source_type, source_id, description)
        return wallet.signal

    @staticmethod
    def award_xp(db: Session, user_id: str, amount: int,
                 source_type: str, source_id: str = None,
                 description: str = '') -> Dict:
        wallet = ResonanceService.get_or_create_wallet(db, user_id)
        wallet.xp += amount
        ResonanceService._log_transaction(
            db, user_id, 'xp', amount, wallet.xp,
            source_type, source_id, description)
        leveled_up = ResonanceService._check_level_up(db, wallet)
        return {
            'xp': wallet.xp, 'level': wallet.level,
            'level_title': wallet.level_title,
            'leveled_up': leveled_up,
        }

    @staticmethod
    def _check_level_up(db: Session, wallet: ResonanceWallet) -> bool:
        leveled = False
        while wallet.xp >= wallet.xp_next_level:
            wallet.xp -= wallet.xp_next_level
            wallet.level += 1
            wallet.level_title = title_for_level(wallet.level)
            wallet.xp_next_level = xp_for_level(wallet.level + 1)
            leveled = True
        if leveled:
            # Sync level to user table
            user = db.query(User).filter_by(id=wallet.user_id).first()
            if user:
                user.level = wallet.level
                user.level_title = wallet.level_title
        return leveled

    @staticmethod
    def award_action(db: Session, user_id: str, action: str,
                     source_id: str = None) -> Dict:
        """Award all currencies for a known action type."""
        awards = AWARD_TABLE.get(action, {})
        if not awards:
            return {}

        result = {}
        if 'pulse' in awards:
            result['pulse'] = ResonanceService.award_pulse(
                db, user_id, awards['pulse'], action, source_id,
                f'Earned for {action}')
        if 'spark' in awards:
            result['spark'] = ResonanceService.award_spark(
                db, user_id, awards['spark'], action, source_id,
                f'Earned for {action}')
        if 'signal' in awards:
            result['signal'] = ResonanceService.award_signal(
                db, user_id, awards['signal'], action, source_id,
                f'Earned for {action}')
        if 'xp' in awards:
            xp_result = ResonanceService.award_xp(
                db, user_id, awards['xp'], action, source_id,
                f'Earned for {action}')
            result.update(xp_result)

        return result

    @staticmethod
    def process_streak(db: Session, user_id: str) -> Dict:
        """Process daily login streak. Call once per day per user."""
        wallet = ResonanceService.get_or_create_wallet(db, user_id)
        today = datetime.utcnow().strftime('%Y-%m-%d')

        if wallet.last_active_date == today:
            return {'streak_days': wallet.streak_days, 'already_checked_in': True}

        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
        if wallet.last_active_date == yesterday:
            wallet.streak_days += 1
        else:
            wallet.streak_days = 1

        wallet.last_active_date = today
        if wallet.streak_days > wallet.streak_best:
            wallet.streak_best = wallet.streak_days

        # Award streak spark: +streak_day
        spark_bonus = wallet.streak_days
        ResonanceService.award_spark(
            db, user_id, spark_bonus, 'streak', None,
            f'Day {wallet.streak_days} streak bonus')

        # Award streak XP: +5 * streak_day
        xp_bonus = 5 * wallet.streak_days
        ResonanceService.award_xp(
            db, user_id, xp_bonus, 'streak', None,
            f'Day {wallet.streak_days} streak XP')

        return {
            'streak_days': wallet.streak_days,
            'streak_best': wallet.streak_best,
            'spark_bonus': spark_bonus,
            'xp_bonus': xp_bonus,
            'already_checked_in': False,
        }

    @staticmethod
    def apply_signal_decay(db: Session) -> int:
        """Batch job: apply 0.2%/day signal decay for users inactive >7 days."""
        cutoff = datetime.utcnow() - timedelta(days=7)
        wallets = db.query(ResonanceWallet).filter(
            ResonanceWallet.signal > 0,
            ResonanceWallet.signal_last_decay < cutoff
        ).all()

        decayed_count = 0
        for w in wallets:
            days_inactive = (datetime.utcnow() - (w.signal_last_decay or w.created_at)).days
            days_to_decay = max(0, days_inactive - 7)
            if days_to_decay > 0:
                decay_rate = 0.002 * days_to_decay
                decay_amount = w.signal * min(decay_rate, 0.5)  # cap at 50%
                w.signal = max(0, w.signal - decay_amount)
                w.signal_last_decay = datetime.utcnow()
                ResonanceService._log_transaction(
                    db, w.user_id, 'signal', -decay_amount, w.signal,
                    'decay', None, f'{days_to_decay}d inactivity decay')
                decayed_count += 1

        return decayed_count

    @staticmethod
    def get_leaderboard(db: Session, currency: str = 'pulse',
                        limit: int = 50, offset: int = 0,
                        region_id: str = None) -> List[Dict]:
        """Get leaderboard sorted by currency."""
        col_map = {
            'pulse': ResonanceWallet.pulse,
            'spark': ResonanceWallet.spark_lifetime,
            'signal': ResonanceWallet.signal,
            'xp': ResonanceWallet.xp,
            'level': ResonanceWallet.level,
        }
        sort_col = col_map.get(currency, ResonanceWallet.pulse)

        q = db.query(ResonanceWallet, User).join(
            User, User.id == ResonanceWallet.user_id)

        if region_id:
            q = q.filter(User.region_id == region_id)

        rows = q.order_by(desc(sort_col)).offset(offset).limit(limit).all()

        result = []
        for i, (wallet, user) in enumerate(rows, start=offset + 1):
            entry = wallet.to_dict()
            entry['rank'] = i
            entry['username'] = user.username
            entry['display_name'] = user.display_name
            entry['avatar_url'] = user.avatar_url
            entry['user_type'] = user.user_type
            result.append(entry)

        return result

    @staticmethod
    def get_transactions(db: Session, user_id: str,
                         currency: str = None,
                         limit: int = 50, offset: int = 0) -> List[Dict]:
        """Get transaction history for a user."""
        q = db.query(ResonanceTransaction).filter_by(user_id=user_id)
        if currency:
            q = q.filter_by(currency=currency)
        txns = q.order_by(desc(ResonanceTransaction.created_at)
                          ).offset(offset).limit(limit).all()
        return [t.to_dict() for t in txns]

    @staticmethod
    def get_breakdown(db: Session, user_id: str) -> Dict:
        """Get detailed breakdown of a user's resonance."""
        wallet = ResonanceService.get_wallet(db, user_id)
        if not wallet:
            return {}

        # Count transactions by source_type
        source_counts = db.query(
            ResonanceTransaction.source_type,
            func.count(ResonanceTransaction.id),
            func.sum(ResonanceTransaction.amount)
        ).filter_by(user_id=user_id).group_by(
            ResonanceTransaction.source_type).all()

        sources = {}
        for source_type, count, total in source_counts:
            sources[source_type] = {'count': count, 'total': float(total or 0)}

        wallet['sources'] = sources
        return wallet
