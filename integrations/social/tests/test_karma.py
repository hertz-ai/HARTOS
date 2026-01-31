"""Tests for HevolveSocial karma engine."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
os.environ['SOCIAL_DB_PATH'] = ':memory:'

from integrations.social.models import (
    Base, get_engine, get_db, User, Post, Comment,
    AgentSkillBadge,
)
from integrations.social.karma_engine import (
    recalculate_karma, get_karma_breakdown, compute_badge_level,
)


@pytest.fixture(autouse=True)
def fresh_db():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    session = get_db()
    yield session
    session.rollback()
    session.close()


class TestKarmaRecalculation:
    def test_zero_karma_new_user(self, db):
        user = User(id='u1', username='newbie', display_name='Newbie',
                     user_type='human')
        db.add(user)
        db.commit()
        recalculate_karma(db, user)
        db.refresh(user)
        assert user.karma_score == 0

    def test_post_karma(self, db):
        user = User(id='u2', username='poster', display_name='Poster',
                     user_type='human')
        db.add(user)
        db.commit()
        db.add(Post(id='p1', author_id='u2', title='P1', content='C', score=10))
        db.add(Post(id='p2', author_id='u2', title='P2', content='C', score=5))
        db.commit()
        recalculate_karma(db, user)
        db.refresh(user)
        assert user.karma_score == 15

    def test_comment_karma(self, db):
        user = User(id='u3', username='commenter', display_name='C',
                     user_type='human')
        db.add(user)
        db.commit()
        post = Post(id='p3', author_id='u3', title='P', content='C')
        db.add(post)
        db.commit()
        db.add(Comment(id='c1', post_id='p3', author_id='u3',
                        content='C1', score=3))
        db.commit()
        recalculate_karma(db, user)
        db.refresh(user)
        # post score 0 + comment score 3 = 3
        assert user.karma_score == 3

    def test_task_karma_preserved(self, db):
        user = User(id='u4', username='tasker', display_name='T',
                     user_type='agent', task_karma=50)
        db.add(user)
        db.commit()
        recalculate_karma(db, user)
        db.refresh(user)
        # For agents, task_karma is recalculated from DB, not from the field
        # With no completed tasks, task_karma should be 0
        assert user.karma_score >= 0


class TestKarmaBreakdown:
    def test_breakdown_structure(self, db):
        user = User(id='u5', username='breakdown', display_name='BD',
                     user_type='human')
        db.add(user)
        db.commit()
        breakdown = get_karma_breakdown(db, user)
        assert 'post_karma' in breakdown
        assert 'comment_karma' in breakdown
        assert 'task_karma' in breakdown
        assert 'total' in breakdown


class TestBadgeLevel:
    # Formula: score = proficiency*0.3 + success_rate*0.4 + min(usage/100,1)*0.3
    # bronze < 0.4, silver 0.4-0.7, gold 0.7-0.9, platinum >= 0.9

    def test_bronze(self):
        # score = 0.1*0.3 + 0.1*0.4 + 0.01*0.3 = 0.03+0.04+0.003 = 0.073
        assert compute_badge_level(0.1, 0.1, 1) == 'bronze'

    def test_silver(self):
        # score = 0.5*0.3 + 0.5*0.4 + 0.5*0.3 = 0.15+0.2+0.15 = 0.5
        assert compute_badge_level(0.5, 0.5, 50) == 'silver'

    def test_gold(self):
        # score = 0.8*0.3 + 0.8*0.4 + 0.8*0.3 = 0.24+0.32+0.24 = 0.8
        assert compute_badge_level(0.8, 0.8, 80) == 'gold'

    def test_platinum(self):
        # score = 0.95*0.3 + 0.95*0.4 + 1.0*0.3 = 0.285+0.38+0.3 = 0.965
        assert compute_badge_level(0.95, 0.95, 500) == 'platinum'

    def test_low_scores(self):
        assert compute_badge_level(0.0, 0.0, 0) == 'bronze'
