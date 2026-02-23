"""Tests for HevolveSocial search functionality."""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.social.models import (
    Base, get_engine, get_db, User, Post, Community,
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


class TestPostSearch:
    def test_search_by_title(self, db):
        user = User(username='searcher', display_name='S', user_type='human')
        db.add(user)
        db.commit()
        db.add(Post(author_id=user.id, title='Python Tutorial',
                     content='Learn Python basics'))
        db.add(Post(author_id=user.id, title='Java Guide',
                     content='Learn Java basics'))
        db.commit()

        results = db.query(Post).filter(
            Post.title.ilike('%python%')
        ).all()
        assert len(results) == 1
        assert results[0].title == 'Python Tutorial'

    def test_search_by_content(self, db):
        user = User(username='searcher2', display_name='S2', user_type='human')
        db.add(user)
        db.commit()
        db.add(Post(author_id=user.id, title='Post A',
                     content='unique_search_term_abc'))
        db.add(Post(author_id=user.id, title='Post B',
                     content='something else'))
        db.commit()

        results = db.query(Post).filter(
            Post.content.ilike('%unique_search_term_abc%')
        ).all()
        assert len(results) == 1

    def test_search_no_results(self, db):
        results = db.query(Post).filter(
            Post.title.ilike('%nonexistent_xyz%')
        ).all()
        assert len(results) == 0


class TestUserSearch:
    def test_search_users(self, db):
        db.add(User(username='alice_dev', display_name='Alice Developer',
                     user_type='human'))
        db.add(User(username='bob_admin', display_name='Bob Admin',
                     user_type='human'))
        db.commit()
        results = db.query(User).filter(
            User.username.ilike('%alice%')
        ).all()
        assert len(results) == 1
        assert results[0].username == 'alice_dev'


class TestCommunitySearch:
    def test_search_communities(self, db):
        user = User(username='subcreator', display_name='SC', user_type='human')
        db.add(user)
        db.commit()
        db.add(Community(name='machine_learning', display_name='Machine Learning',
                        description='ML discussions', creator_id=user.id))
        db.add(Community(name='web_dev', display_name='Web Development',
                        description='Web stuff', creator_id=user.id))
        db.commit()
        results = db.query(Community).filter(
            Community.name.ilike('%machine%')
        ).all()
        assert len(results) == 1
        assert results[0].name == 'machine_learning'


class TestSearchIntegration:
    def test_semantic_search_import(self):
        """Verify search_integration module loads without error."""
        try:
            from integrations.social.search_integration import (
                compute_post_embedding, semantic_search_posts
            )
            assert callable(compute_post_embedding)
            assert callable(semantic_search_posts)
        except ImportError:
            pytest.skip("search_integration dependencies not available")
