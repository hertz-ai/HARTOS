"""Tests for HevolveSocial database models."""
import os
import sys
import pytest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SOCIAL_DB_PATH'] = ':memory:'

from integrations.social.models import (
    Base, get_engine, get_db, init_db,
    User, Community, Post, Comment, Vote, Follow,
    CommunityMembership, AgentSkillBadge, TaskRequest,
    Notification, Report, RecipeShare,
)


@pytest.fixture(autouse=True)
def fresh_db():
    """Create fresh tables for each test."""
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


class TestUserModel:
    def test_create_human_user(self, db):
        user = User(username='alice', display_name='Alice', user_type='human')
        db.add(user)
        db.commit()
        assert user.id is not None
        assert user.user_type == 'human'
        assert user.karma_score == 0

    def test_create_agent_user(self, db):
        user = User(username='bot_1', display_name='Bot One',
                     user_type='agent', agent_id='agent_001')
        db.add(user)
        db.commit()
        assert user.agent_id == 'agent_001'

    def test_unique_username(self, db):
        db.add(User(username='unique', display_name='U1', user_type='human'))
        db.commit()
        db.add(User(username='unique', display_name='U2', user_type='human'))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()

    def test_to_dict(self, db):
        user = User(username='dicttest', display_name='Dict Test',
                     bio='hello', user_type='human')
        db.add(user)
        db.commit()
        d = user.to_dict()
        assert d['username'] == 'dicttest'
        assert d['bio'] == 'hello'
        # password_hash and api_token excluded by default
        assert 'password_hash' not in d
        assert 'api_token' not in d

    def test_to_dict_with_token(self, db):
        user = User(username='tokentest', display_name='TT',
                     user_type='human', api_token='secret123')
        db.add(user)
        db.commit()
        d = user.to_dict(include_token=True)
        assert d['api_token'] == 'secret123'


class TestPostModel:
    def test_create_post(self, db):
        user = User(username='poster', display_name='Poster', user_type='human')
        db.add(user)
        db.commit()
        post = Post(author_id=user.id, title='Hello', content='World',
                     content_type='text')
        db.add(post)
        db.commit()
        assert post.id is not None
        assert post.score == 0
        assert post.comment_count == 0

    def test_post_to_dict(self, db):
        user = User(username='poster2', display_name='P2', user_type='human')
        db.add(user)
        db.commit()
        post = Post(author_id=user.id, title='T', content='C')
        db.add(post)
        db.commit()
        d = post.to_dict()
        assert d['title'] == 'T'
        assert d['author_id'] == user.id

    def test_post_with_community(self, db):
        user = User(username='sm_poster', display_name='SM', user_type='human')
        db.add(user)
        db.commit()
        sub = Community(name='test_sub', display_name='Test', creator_id=user.id)
        db.add(sub)
        db.commit()
        post = Post(author_id=user.id, community_id=sub.id,
                     title='In Sub', content='Body')
        db.add(post)
        db.commit()
        assert post.community_id == sub.id


class TestCommentModel:
    def test_create_comment(self, db):
        user = User(username='commenter', display_name='C', user_type='human')
        db.add(user)
        db.commit()
        post = Post(author_id=user.id, title='P', content='C')
        db.add(post)
        db.commit()
        comment = Comment(post_id=post.id, author_id=user.id,
                           content='Nice post!')
        db.add(comment)
        db.commit()
        assert comment.depth == 0

    def test_nested_comment(self, db):
        user = User(username='nester', display_name='N', user_type='human')
        db.add(user)
        db.commit()
        post = Post(author_id=user.id, title='P', content='C')
        db.add(post)
        db.commit()
        parent = Comment(post_id=post.id, author_id=user.id,
                          content='Parent', depth=0)
        db.add(parent)
        db.commit()
        child = Comment(post_id=post.id, author_id=user.id,
                         content='Child', parent_id=parent.id, depth=1)
        db.add(child)
        db.commit()
        assert child.parent_id == parent.id
        assert child.depth == 1


class TestVoteModel:
    def test_create_vote(self, db):
        user = User(username='voter', display_name='V', user_type='human')
        db.add(user)
        db.commit()
        post = Post(author_id=user.id, title='P', content='C')
        db.add(post)
        db.commit()
        vote = Vote(user_id=user.id, target_type='post',
                     target_id=post.id, value=1)
        db.add(vote)
        db.commit()
        assert vote.value == 1

    def test_unique_vote_constraint(self, db):
        user = User(username='voter2', display_name='V2', user_type='human')
        db.add(user)
        db.commit()
        post = Post(author_id=user.id, title='P', content='C')
        db.add(post)
        db.commit()
        db.add(Vote(user_id=user.id, target_type='post',
                     target_id=post.id, value=1))
        db.commit()
        db.add(Vote(user_id=user.id, target_type='post',
                     target_id=post.id, value=-1))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()


class TestFollowModel:
    def test_follow(self, db):
        u1 = User(username='follower', display_name='F', user_type='human')
        u2 = User(username='followed', display_name='Fd', user_type='human')
        db.add_all([u1, u2])
        db.commit()
        follow = Follow(follower_id=u1.id, following_id=u2.id)
        db.add(follow)
        db.commit()
        assert follow.follower_id == u1.id


class TestCommunityModel:
    def test_create_community(self, db):
        user = User(username='creator', display_name='Cr', user_type='human')
        db.add(user)
        db.commit()
        sub = Community(name='python', display_name='Python',
                       description='All things Python', creator_id=user.id)
        db.add(sub)
        db.commit()
        assert sub.member_count == 0

    def test_membership(self, db):
        user = User(username='member', display_name='M', user_type='human')
        db.add(user)
        db.commit()
        sub = Community(name='java', display_name='Java', creator_id=user.id)
        db.add(sub)
        db.commit()
        mem = CommunityMembership(user_id=user.id, community_id=sub.id,
                                 role='admin')
        db.add(mem)
        db.commit()
        assert mem.role == 'admin'


class TestNotificationModel:
    def test_create_notification(self, db):
        user = User(username='notif_user', display_name='NU', user_type='human')
        db.add(user)
        db.commit()
        notif = Notification(user_id=user.id, type='upvote',
                              message='Someone upvoted your post')
        db.add(notif)
        db.commit()
        assert notif.is_read is False


class TestInitDb:
    def test_init_db_creates_tables(self):
        init_db()
        engine = get_engine()
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert 'users' in tables
        assert 'posts' in tables
        assert 'comments' in tables
        assert 'votes' in tables
