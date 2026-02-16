"""Tests for HevolveSocial feed engine."""
import os
import sys
import pytest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.social.models import (
    Base, get_engine, get_db, User, Post, Community,
    Follow, CommunityMembership,
)
from integrations.social.feed_engine import (
    get_personalized_feed, get_global_feed,
    get_trending_feed, get_agent_feed,
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


def make_user(db, username, user_type='human'):
    u = User(username=username, display_name=username.title(),
             user_type=user_type)
    db.add(u)
    db.commit()
    return u


def make_post(db, user, title='Post', score=0, hours_ago=0):
    from integrations.social.models import _uuid
    p = Post(id=_uuid(), author_id=user.id, title=title,
             content=f'Content of {title}', score=score)
    db.add(p)
    db.commit()
    if hours_ago:
        # Update after commit so default isn't overridden
        p.created_at = datetime.utcnow() - timedelta(hours=hours_ago)
        db.commit()
    return p


class TestGlobalFeed:
    def test_empty_feed(self, db):
        posts, total = get_global_feed(db)
        assert total == 0
        assert posts == []

    def test_feed_returns_posts(self, db):
        user = make_user(db, 'feeduser')
        make_post(db, user, 'A')
        make_post(db, user, 'B')
        posts, total = get_global_feed(db)
        assert total == 2

    def test_sort_new(self, db):
        user = make_user(db, 'sortuser')
        make_post(db, user, 'Old', hours_ago=5)
        make_post(db, user, 'New', hours_ago=0)
        posts, _ = get_global_feed(db, sort='new')
        assert posts[0].title == 'New'

    def test_sort_top(self, db):
        user = make_user(db, 'topuser')
        make_post(db, user, 'Low', score=1)
        make_post(db, user, 'High', score=100)
        posts, _ = get_global_feed(db, sort='top')
        assert posts[0].title == 'High'

    def test_pagination(self, db):
        user = make_user(db, 'pageuser')
        for i in range(5):
            make_post(db, user, f'Post {i}')
        posts, total = get_global_feed(db, limit=2, offset=0)
        assert len(posts) == 2
        assert total == 5


class TestTrendingFeed:
    def test_trending_scores(self, db):
        user = make_user(db, 'trenduser')
        make_post(db, user, 'Trending', score=50, hours_ago=1)
        make_post(db, user, 'Old', score=50, hours_ago=48)
        posts, _ = get_trending_feed(db)
        # Recent high-score post should appear
        assert len(posts) >= 1


class TestAgentFeed:
    def test_agent_only(self, db):
        human = make_user(db, 'human1', 'human')
        agent = make_user(db, 'agent1', 'agent')
        make_post(db, human, 'Human Post')
        make_post(db, agent, 'Agent Post')
        posts, total = get_agent_feed(db)
        assert total == 1
        assert posts[0].title == 'Agent Post'


class TestPersonalizedFeed:
    def test_follows_in_feed(self, db):
        me = make_user(db, 'me')
        friend = make_user(db, 'friend')
        db.add(Follow(follower_id=me.id, following_id=friend.id))
        db.commit()
        make_post(db, friend, 'Friend Post')
        posts, _ = get_personalized_feed(db, me.id)
        titles = [p.title for p in posts]
        assert 'Friend Post' in titles

    def test_community_in_feed(self, db):
        me = make_user(db, 'me2')
        other = make_user(db, 'other')
        sub = Community(name='mysub', display_name='MySub', creator_id=me.id)
        db.add(sub)
        db.commit()
        db.add(CommunityMembership(user_id=me.id, community_id=sub.id, role='member'))
        db.commit()
        p = make_post(db, other, 'Sub Post')
        p.community_id = sub.id
        db.commit()
        posts, _ = get_personalized_feed(db, me.id)
        titles = [p.title for p in posts]
        assert 'Sub Post' in titles
