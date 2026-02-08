"""Tests for HevolveSocial API endpoints."""
import os
import sys
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SOCIAL_DB_PATH'] = ':memory:'

from flask import Flask
from integrations.social.models import Base, get_engine, init_db
from integrations.social.api import social_bp
from integrations.social.rate_limiter import get_limiter


@pytest.fixture
def app():
    """Create test Flask app with social blueprint."""
    test_app = Flask(__name__)
    test_app.config['TESTING'] = True
    test_app.register_blueprint(social_bp)
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # Reset rate limiter between tests to avoid cross-test rate limiting
    get_limiter()._buckets.clear()
    yield test_app
    Base.metadata.drop_all(engine)


@pytest.fixture
def client(app):
    return app.test_client()


def register_user(client, username='testuser', password='testpass123'):
    """Helper to register and get api_token."""
    resp = client.post('/api/social/auth/register', json={
        'username': username,
        'password': password,
        'display_name': username.title(),
    })
    data = resp.get_json()
    if data and data.get('success') and data.get('data'):
        return data['data'].get('api_token')
    return None


def auth_header(token):
    if not token:
        return {}
    return {'Authorization': f'Bearer {token}'}


class TestAuthEndpoints:
    def test_register(self, client):
        resp = client.post('/api/social/auth/register', json={
            'username': 'alice',
            'password': 'password123',
            'display_name': 'Alice',
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['success'] is True
        assert 'api_token' in data['data']

    def test_register_duplicate(self, client):
        register_user(client, 'dup_user')
        resp = client.post('/api/social/auth/register', json={
            'username': 'dup_user',
            'password': 'password123',
            'display_name': 'Dup',
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False

    def test_login(self, client):
        register_user(client, 'loginuser', 'mypass123!')
        resp = client.post('/api/social/auth/login', json={
            'username': 'loginuser',
            'password': 'mypass123!',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'token' in data['data']

    def test_login_wrong_password(self, client):
        register_user(client, 'wrongpw', 'correct123!')
        resp = client.post('/api/social/auth/login', json={
            'username': 'wrongpw',
            'password': 'incorrect1!',
        })
        assert resp.status_code == 401

    def test_me(self, client):
        token = register_user(client, 'meuser')
        resp = client.get('/api/social/auth/me', headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['data']['username'] == 'meuser'

    def test_me_no_auth(self, client):
        resp = client.get('/api/social/auth/me')
        assert resp.status_code == 401


class TestPostEndpoints:
    def test_create_post(self, client):
        token = register_user(client)
        resp = client.post('/api/social/posts', json={
            'title': 'My First Post',
            'content': 'Hello social network!',
        }, headers=auth_header(token))
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['data']['title'] == 'My First Post'

    def test_list_posts(self, client):
        token1 = register_user(client, 'poster1')
        token2 = register_user(client, 'poster2')
        client.post('/api/social/posts', json={
            'title': 'Post 1', 'content': 'Body 1',
        }, headers=auth_header(token1))
        client.post('/api/social/posts', json={
            'title': 'Post 2', 'content': 'Body 2',
        }, headers=auth_header(token2))
        resp = client.get('/api/social/posts')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['data']) >= 2

    def test_get_post(self, client):
        token = register_user(client)
        create_resp = client.post('/api/social/posts', json={
            'title': 'Get Me', 'content': 'Content',
        }, headers=auth_header(token))
        post_id = create_resp.get_json()['data']['id']
        resp = client.get(f'/api/social/posts/{post_id}')
        assert resp.status_code == 200
        assert resp.get_json()['data']['title'] == 'Get Me'

    def test_upvote_post(self, client):
        token = register_user(client)
        create_resp = client.post('/api/social/posts', json={
            'title': 'Upvote Me', 'content': 'C',
        }, headers=auth_header(token))
        post_id = create_resp.get_json()['data']['id']
        resp = client.post(f'/api/social/posts/{post_id}/upvote',
                           headers=auth_header(token))
        assert resp.status_code == 200

    def test_delete_post(self, client):
        token = register_user(client)
        create_resp = client.post('/api/social/posts', json={
            'title': 'Delete Me', 'content': 'C',
        }, headers=auth_header(token))
        post_id = create_resp.get_json()['data']['id']
        resp = client.delete(f'/api/social/posts/{post_id}',
                              headers=auth_header(token))
        assert resp.status_code == 200


class TestCommentEndpoints:
    def test_create_comment(self, client):
        token = register_user(client)
        post_resp = client.post('/api/social/posts', json={
            'title': 'Comment Target', 'content': 'C',
        }, headers=auth_header(token))
        post_id = post_resp.get_json()['data']['id']
        resp = client.post(f'/api/social/posts/{post_id}/comments', json={
            'content': 'Great post!',
        }, headers=auth_header(token))
        assert resp.status_code == 201

    def test_list_comments(self, client):
        token = register_user(client)
        post_resp = client.post('/api/social/posts', json={
            'title': 'C Target', 'content': 'C',
        }, headers=auth_header(token))
        post_id = post_resp.get_json()['data']['id']
        client.post(f'/api/social/posts/{post_id}/comments', json={
            'content': 'Comment 1',
        }, headers=auth_header(token))
        resp = client.get(f'/api/social/posts/{post_id}/comments')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['data']) >= 1


class TestCommunityEndpoints:
    def test_create_community(self, client):
        token = register_user(client)
        resp = client.post('/api/social/communities', json={
            'name': 'python_devs',
            'display_name': 'Python Developers',
            'description': 'For Python fans',
        }, headers=auth_header(token))
        assert resp.status_code == 201

    def test_list_communities(self, client):
        token = register_user(client)
        client.post('/api/social/communities', json={
            'name': 'list_test', 'display_name': 'List Test',
        }, headers=auth_header(token))
        resp = client.get('/api/social/communities')
        assert resp.status_code == 200


class TestFeedEndpoints:
    def test_global_feed(self, client):
        resp = client.get('/api/social/feed/all')
        assert resp.status_code == 200

    def test_trending_feed(self, client):
        resp = client.get('/api/social/feed/trending')
        assert resp.status_code == 200


class TestUserEndpoints:
    def test_get_user_profile(self, client):
        register_user(client, 'profileuser')
        resp = client.get('/api/social/users/profileuser')
        assert resp.status_code == 200
        assert resp.get_json()['data']['username'] == 'profileuser'

    def test_follow_user(self, client):
        token1 = register_user(client, 'follower1')
        register_user(client, 'followed1')
        resp = client.post('/api/social/users/followed1/follow',
                           headers=auth_header(token1))
        assert resp.status_code == 200


class TestRNCompatEndpoints:
    def test_get_all_posts_compat(self, client):
        token = register_user(client)
        client.post('/api/social/posts', json={
            'title': 'RN Post', 'content': 'For RN',
        }, headers=auth_header(token))
        resp = client.get('/api/social/compat/getAllPosts')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_comment_bypost_compat(self, client):
        token = register_user(client)
        post_resp = client.post('/api/social/posts', json={
            'title': 'RN Comment', 'content': 'C',
        }, headers=auth_header(token))
        post_id = post_resp.get_json()['data']['id']
        resp = client.get(f'/api/social/compat/comment_bypost?post_id={post_id}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'comment' in data


class TestSearchEndpoints:
    def test_search_posts(self, client):
        token = register_user(client)
        client.post('/api/social/posts', json={
            'title': 'Searchable Post', 'content': 'unique_keyword_xyz',
        }, headers=auth_header(token))
        resp = client.get('/api/social/search?q=unique_keyword_xyz&type=posts')
        assert resp.status_code == 200


class TestAdminEndpoints:
    def test_stats_requires_admin(self, client):
        token = register_user(client, 'nonadmin')
        resp = client.get('/api/social/admin/stats',
                          headers=auth_header(token))
        assert resp.status_code == 403
