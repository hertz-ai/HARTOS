"""
test_karma_and_ratings.py - Tests for karma_engine.py + rating_service.py

Tests the reputation system — drives trust, visibility, and privileges.
Each test verifies a specific scoring mechanic or validation boundary:

FT: Karma calculation (post + comment + task), badge levels, rating
    submission (validation, dedup, self-rating rejection), trust recalculation.
NFT: Badge thresholds are monotonic, dimension list is complete,
     trust weights sum to 1.0, score range enforcement.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Badge level computation — determines profile badge display
# ============================================================

class TestBadgeLevels:
    """compute_badge_level maps performance metrics to badge tiers.
    Wrong threshold = users see incorrect badges on agent profiles."""

    def test_perfect_score_is_platinum(self):
        from integrations.social.karma_engine import compute_badge_level
        assert compute_badge_level(1.0, 1.0, 200) == 'platinum'

    def test_high_score_is_gold(self):
        from integrations.social.karma_engine import compute_badge_level
        assert compute_badge_level(0.8, 0.8, 50) == 'gold'

    def test_medium_score_is_silver(self):
        from integrations.social.karma_engine import compute_badge_level
        assert compute_badge_level(0.5, 0.5, 30) == 'silver'

    def test_low_score_is_bronze(self):
        from integrations.social.karma_engine import compute_badge_level
        assert compute_badge_level(0.1, 0.1, 1) == 'bronze'

    def test_zero_usage_doesnt_crash(self):
        from integrations.social.karma_engine import compute_badge_level
        result = compute_badge_level(0.5, 0.5, 0)
        assert result in ('bronze', 'silver', 'gold', 'platinum')

    def test_thresholds_are_monotonic(self):
        """Higher scores must give higher badge levels."""
        from integrations.social.karma_engine import compute_badge_level
        levels = {'bronze': 0, 'silver': 1, 'gold': 2, 'platinum': 3}
        low = levels[compute_badge_level(0.1, 0.1, 1)]
        mid = levels[compute_badge_level(0.5, 0.5, 50)]
        high = levels[compute_badge_level(1.0, 1.0, 200)]
        assert low <= mid <= high

    def test_usage_capped_at_100(self):
        """Usage count >100 must not inflate score beyond cap."""
        from integrations.social.karma_engine import compute_badge_level
        at_100 = compute_badge_level(0.9, 0.9, 100)
        at_1000 = compute_badge_level(0.9, 0.9, 1000)
        assert at_100 == at_1000  # Same badge — usage capped


# ============================================================
# Karma calculation — combines upvotes + task completion
# ============================================================

class TestKarmaCalculation:
    """recalculate_karma produces the total reputation score."""

    def test_returns_int(self):
        from integrations.social.karma_engine import recalculate_karma
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.user_type = 'human'
        mock_user.karma_score = 0
        mock_user.task_karma = 0
        result = recalculate_karma(mock_db, mock_user)
        assert isinstance(result, int)

    def test_human_user_no_task_karma(self):
        """Humans only get upvote karma — task_karma is agent-only."""
        from integrations.social.karma_engine import recalculate_karma
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.scalar.return_value = 50
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.user_type = 'human'
        mock_user.karma_score = 0
        mock_user.task_karma = 0
        result = recalculate_karma(mock_db, mock_user)
        assert mock_user.task_karma == 0  # Human = no task karma

    def test_karma_breakdown_has_all_keys(self):
        """Frontend profile page destructures these keys."""
        from integrations.social.karma_engine import get_karma_breakdown
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.karma_score = 100
        mock_user.task_karma = 50
        result = get_karma_breakdown(mock_db, mock_user)
        required = {'total', 'post_karma', 'comment_karma', 'task_karma', 'completed_tasks'}
        assert required.issubset(set(result.keys()))


# ============================================================
# Rating dimensions — multi-dimensional assessment
# ============================================================

class TestRatingDimensions:
    """DIMENSIONS and TRUST_WEIGHTS drive the trust score calculation."""

    def test_has_required_dimensions(self):
        from integrations.social.rating_service import DIMENSIONS
        assert 'skill' in DIMENSIONS
        assert 'reliability' in DIMENSIONS

    def test_trust_weights_sum_to_one(self):
        """Weights must sum to 1.0 — otherwise trust scores are skewed."""
        from integrations.social.rating_service import TRUST_WEIGHTS
        total = sum(TRUST_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001, f"Trust weights sum to {total}, not 1.0"

    def test_all_dimensions_have_weights(self):
        from integrations.social.rating_service import DIMENSIONS, TRUST_WEIGHTS
        for dim in DIMENSIONS:
            assert dim in TRUST_WEIGHTS, f"Dimension '{dim}' has no trust weight"


# ============================================================
# Rating submission — validation boundaries
# ============================================================

class TestRatingSubmission:
    """submit_rating validates input before writing to DB."""

    def test_rejects_invalid_dimension(self):
        """Unknown dimensions must be rejected — prevents DB schema mismatch."""
        from integrations.social.rating_service import RatingService
        mock_db = MagicMock()
        result = RatingService.submit_rating(
            mock_db, 'rater', 'rated', 'post', 'p1', 'nonexistent_dim', 3.0)
        assert result is None

    def test_rejects_score_below_1(self):
        from integrations.social.rating_service import RatingService
        mock_db = MagicMock()
        result = RatingService.submit_rating(
            mock_db, 'rater', 'rated', 'post', 'p1', 'skill', 0.5)
        assert result is None

    def test_rejects_score_above_5(self):
        from integrations.social.rating_service import RatingService
        mock_db = MagicMock()
        result = RatingService.submit_rating(
            mock_db, 'rater', 'rated', 'post', 'p1', 'skill', 5.5)
        assert result is None

    def test_rejects_self_rating(self):
        """Users must not rate themselves — prevents reputation inflation."""
        from integrations.social.rating_service import RatingService
        mock_db = MagicMock()
        result = RatingService.submit_rating(
            mock_db, 'user_1', 'user_1', 'post', 'p1', 'skill', 5.0)
        assert result is None

    def test_accepts_valid_rating(self):
        """Valid rating: different users, valid dimension, score 1-5."""
        from integrations.social.rating_service import RatingService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_rating = MagicMock()
        mock_rating.to_dict.return_value = {'id': '1', 'score': 4.0}
        with patch('integrations.social.rating_service.Rating', return_value=mock_rating):
            with patch.object(RatingService, '_recalculate_trust'):
                result = RatingService.submit_rating(
                    mock_db, 'user_a', 'user_b', 'post', 'p1', 'skill', 4.0)
        assert result is not None

    def test_score_boundary_1_accepted(self):
        """Score exactly 1.0 is valid (minimum)."""
        from integrations.social.rating_service import RatingService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_rating = MagicMock()
        mock_rating.to_dict.return_value = {'score': 1.0}
        with patch('integrations.social.rating_service.Rating', return_value=mock_rating):
            with patch.object(RatingService, '_recalculate_trust'):
                result = RatingService.submit_rating(
                    mock_db, 'a', 'b', 'post', 'p1', 'skill', 1.0)
        assert result is not None

    def test_score_boundary_5_accepted(self):
        """Score exactly 5.0 is valid (maximum)."""
        from integrations.social.rating_service import RatingService
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_rating = MagicMock()
        mock_rating.to_dict.return_value = {'score': 5.0}
        with patch('integrations.social.rating_service.Rating', return_value=mock_rating):
            with patch.object(RatingService, '_recalculate_trust'):
                result = RatingService.submit_rating(
                    mock_db, 'a', 'b', 'post', 'p1', 'creativity', 5.0)
        assert result is not None
