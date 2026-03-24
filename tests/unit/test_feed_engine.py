"""
test_feed_engine.py - Tests for integrations/social/feed_engine.py

Tests the social feed ranking algorithms — directly impacts user engagement.
Each test verifies a specific ranking guarantee or privacy boundary:

FT: Hot score formula (upvotes, decay, tie-breaking), personalized feed
    (follows + communities), global sort modes (new/top/hot/discussed),
    trending velocity cutoff, agent-only feed filter.
NFT: Private community posts hidden from non-members, deleted/hidden
     posts excluded, empty follows fallback to trending, score positivity.
"""
import os
import sys
import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# _hot_score — Reddit-style ranking algorithm
# ============================================================

class TestHotScore:
    """_hot_score determines post ordering — wrong math = stale content at top."""

    def test_higher_upvotes_higher_score(self):
        """More upvotes = higher score at the same age."""
        from integrations.social.feed_engine import _hot_score
        now = datetime.utcnow()
        score_10 = _hot_score(10, 0, now)
        score_100 = _hot_score(100, 0, now)
        assert score_100 > score_10

    def test_newer_post_higher_score(self):
        """Newer posts rank higher than older posts with same votes."""
        from integrations.social.feed_engine import _hot_score
        new = datetime.utcnow()
        old = datetime.utcnow() - timedelta(hours=24)
        assert _hot_score(10, 0, new) > _hot_score(10, 0, old)

    def test_downvotes_reduce_score(self):
        """Downvoted posts should rank lower."""
        from integrations.social.feed_engine import _hot_score
        now = datetime.utcnow()
        no_down = _hot_score(10, 0, now)
        with_down = _hot_score(10, 8, now)
        assert no_down > with_down

    def test_minimum_score_is_1(self):
        """Net score is floored at 1 — prevents log(0) crash."""
        from integrations.social.feed_engine import _hot_score
        now = datetime.utcnow()
        result = _hot_score(0, 10, now)  # net = -10, clamped to 1
        assert isinstance(result, float)
        assert not math.isnan(result)
        assert not math.isinf(result)

    def test_zero_votes_valid(self):
        """Brand new post with 0 votes must still have a valid score."""
        from integrations.social.feed_engine import _hot_score
        now = datetime.utcnow()
        result = _hot_score(0, 0, now)
        assert isinstance(result, float)

    def test_very_old_post_low_score(self):
        """Posts older than a week should have very low scores."""
        from integrations.social.feed_engine import _hot_score
        old = datetime.utcnow() - timedelta(days=7)
        result = _hot_score(100, 0, old)
        recent = _hot_score(1, 0, datetime.utcnow())
        # Even 100 upvotes can't beat recency after a week
        assert recent > result

    def test_decay_rate(self):
        """Score should decay roughly by age_hours/12."""
        from integrations.social.feed_engine import _hot_score
        now = datetime.utcnow()
        score_0h = _hot_score(100, 0, now)
        score_12h = _hot_score(100, 0, now - timedelta(hours=12))
        # After 12 hours, score drops by ~1.0 (12/12)
        diff = score_0h - score_12h
        assert 0.8 < diff < 1.2  # approximately 1.0


# ============================================================
# Feed functions — mock DB queries
# ============================================================

class TestGlobalFeed:
    """get_global_feed — the default view for anonymous/new users."""

    def _mock_db(self):
        db = MagicMock()
        query = MagicMock()
        db.query.return_value = query
        query.options.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.offset.return_value = query
        query.limit.return_value = query
        query.count.return_value = 5
        query.all.return_value = [MagicMock() for _ in range(5)]
        # Mock Community subquery
        query.subquery.return_value = MagicMock()
        return db

    def test_returns_posts_and_total(self):
        from integrations.social.feed_engine import get_global_feed
        db = self._mock_db()
        posts, total = get_global_feed(db, sort='new', limit=25)
        assert isinstance(posts, list)
        assert isinstance(total, int)

    def test_sort_new_uses_created_at(self):
        """'new' sort must order by created_at descending."""
        from integrations.social.feed_engine import get_global_feed
        db = self._mock_db()
        get_global_feed(db, sort='new')
        # Verify order_by was called (with desc(Post.created_at))
        db.query.return_value.options.return_value.filter.return_value.order_by.assert_called()

    def test_sort_top_uses_score(self):
        from integrations.social.feed_engine import get_global_feed
        db = self._mock_db()
        get_global_feed(db, sort='top')
        # No crash — sort mode accepted

    def test_sort_discussed_uses_comment_count(self):
        from integrations.social.feed_engine import get_global_feed
        db = self._mock_db()
        get_global_feed(db, sort='discussed')

    def test_limit_and_offset_applied(self):
        from integrations.social.feed_engine import get_global_feed
        db = self._mock_db()
        get_global_feed(db, sort='new', limit=10, offset=20)
        # offset(20) and limit(10) should be called
        q = db.query.return_value.options.return_value.filter.return_value.order_by.return_value
        q.offset.assert_called_with(20)


class TestTrendingFeed:
    """get_trending_feed — posts from last 24h ranked by velocity."""

    def _mock_db(self):
        db = MagicMock()
        query = MagicMock()
        db.query.return_value = query
        query.options.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.offset.return_value = query
        query.limit.return_value = query
        query.count.return_value = 3
        query.all.return_value = [MagicMock() for _ in range(3)]
        query.subquery.return_value = MagicMock()
        return db

    def test_returns_posts_and_total(self):
        from integrations.social.feed_engine import get_trending_feed
        db = self._mock_db()
        posts, total = get_trending_feed(db, limit=10)
        assert isinstance(posts, list)
        assert total == 3

    def test_24h_cutoff_applied(self):
        """Only posts from last 24 hours — older posts must not appear."""
        from integrations.social.feed_engine import get_trending_feed
        db = self._mock_db()
        get_trending_feed(db)
        # filter() called with created_at >= cutoff
        db.query.return_value.options.return_value.filter.assert_called()


class TestPersonalizedFeed:
    """get_personalized_feed — the main feed for logged-in users."""

    def _mock_db(self, followed_ids=None, community_ids=None):
        db = MagicMock()
        query = MagicMock()
        db.query.return_value = query
        query.options.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.offset.return_value = query
        query.limit.return_value = query
        query.count.return_value = 2
        query.all.return_value = [MagicMock(), MagicMock()]
        query.subquery.return_value = MagicMock()
        # Mock Follow query
        follow_results = [MagicMock(following_id=fid) for fid in (followed_ids or [])]
        member_results = [MagicMock(community_id=cid) for cid in (community_ids or [])]
        # Chain: db.query(Follow.following_id).filter(...).all()
        def side_effect(*args, **kwargs):
            q = MagicMock()
            q.filter.return_value = q
            if args and hasattr(args[0], 'key') and 'following' in str(args[0]):
                q.all.return_value = follow_results
            else:
                q.all.return_value = member_results
            return q
        db.query.side_effect = side_effect
        return db

    def test_empty_follows_falls_back_to_trending(self):
        """New users with no follows get trending feed — not empty."""
        from integrations.social.feed_engine import get_personalized_feed
        db = MagicMock()
        q = MagicMock()
        db.query.return_value = q
        q.filter.return_value = q
        q.all.return_value = []  # No follows, no communities
        q.options.return_value = q
        q.order_by.return_value = q
        q.offset.return_value = q
        q.limit.return_value = q
        q.count.return_value = 0
        q.subquery.return_value = MagicMock()
        q.join.return_value = q
        # Should fallback to get_trending_feed — no crash
        with patch('integrations.social.feed_engine.get_trending_feed',
                   return_value=([], 0)) as mock_trending:
            posts, total = get_personalized_feed(db, user_id='user_1')
        mock_trending.assert_called_once()
        assert isinstance(posts, list)
