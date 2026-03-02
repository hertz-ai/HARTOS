"""
HevolveSocial - Engagement Guardrails
Health-first limits for games and compute. No addiction by design.
Humans always decide — these are suggestions, not blocks.
"""
import logging
from datetime import datetime, timedelta
from typing import Tuple, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import GameSession, GameParticipant

logger = logging.getLogger('hevolve_social')

# ─── Configurable Limits ───
MAX_GAMES_PER_DAY = 20              # Soft cap — friendly message, not a block
MAX_COMPUTE_CONTINUOUS_HOURS = 8    # Auto-pause suggestion after 8 hours
MIN_GAME_INTERVAL_MINUTES = 2      # Prevent rapid-fire game creation
COOLDOWN_AFTER_LOSS_STREAK = 3     # After 3 losses, suggest different activity
MAX_NOTIFICATIONS_PER_SESSION = 3  # Batch achievement notifications


class EngagementGuardrails:

    @staticmethod
    def check_game_limit(db: Session, user_id: str) -> Tuple[bool, Optional[str]]:
        """Check if user is within healthy game limits.
        Returns (allowed, message). Always allowed — message is a suggestion."""
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Count today's games
        games_today = db.query(func.count(GameParticipant.id)).filter(
            GameParticipant.user_id == user_id,
            GameParticipant.joined_at >= today_start,
        ).scalar() or 0

        if games_today >= MAX_GAMES_PER_DAY:
            return True, (
                f"You've played {games_today} games today — great session! "
                "Maybe try a thought experiment or browse the feed for a change of pace."
            )

        # Check loss streak
        recent_results = db.query(GameParticipant.result).filter(
            GameParticipant.user_id == user_id,
            GameParticipant.result.isnot(None),
        ).order_by(GameParticipant.finished_at.desc()).limit(COOLDOWN_AFTER_LOSS_STREAK).all()

        consecutive_losses = 0
        for (result,) in recent_results:
            if result == 'loss':
                consecutive_losses += 1
            else:
                break

        if consecutive_losses >= COOLDOWN_AFTER_LOSS_STREAK:
            return True, (
                f"{consecutive_losses} tough rounds — happens to everyone! "
                "How about trying a collaborative puzzle? Everyone wins together."
            )

        # Check rapid-fire
        last_game = db.query(GameParticipant.joined_at).filter(
            GameParticipant.user_id == user_id,
        ).order_by(GameParticipant.joined_at.desc()).first()

        if last_game and last_game[0]:
            minutes_since = (datetime.utcnow() - last_game[0]).total_seconds() / 60
            if minutes_since < MIN_GAME_INTERVAL_MINUTES:
                return True, "Take a breath between games — no rush."

        return True, None

    @staticmethod
    def check_compute_health(db: Session, user_id: str,
                             continuous_hours: float = 0) -> Tuple[bool, Optional[str]]:
        """Check compute sharing health. Returns (healthy, suggestion)."""
        if continuous_hours >= MAX_COMPUTE_CONTINUOUS_HOURS:
            return True, (
                f"Your compute has been sharing for {continuous_hours:.0f} hours. "
                "Everything's running smoothly. Taking a break is healthy too."
            )
        return True, None

    @staticmethod
    def should_suggest_break(db: Session, user_id: str) -> Tuple[bool, Optional[str]]:
        """Check if user has been active too long. Gentle suggestion only."""
        two_hours_ago = datetime.utcnow() - timedelta(hours=2)

        recent_activity = db.query(func.count(GameParticipant.id)).filter(
            GameParticipant.user_id == user_id,
            GameParticipant.joined_at >= two_hours_ago,
        ).scalar() or 0

        if recent_activity >= 8:
            return True, (
                "You've been playing for a while — great session! "
                "Maybe take a stretch break?"
            )
        return False, None
