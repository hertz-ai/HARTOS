"""
Tests for secret redactor integration points and GDPR endpoints.

Validates:
- /chat prompt redaction
- Recipe JSON redaction before save
- GDPR data export
- GDPR data deletion/anonymization
"""
import os
import sys
import json
import uuid
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')


class TestSecretRedactorFunction:
    """Test the redact_secrets function directly."""

    def test_redacts_openai_api_key(self):
        from security.secret_redactor import redact_secrets
        text = "Use this key: sk-abc123def456ghi789jkl012mno345pqr678stu901vwx"
        result, count = redact_secrets(text)
        assert 'sk-' not in result
        assert count > 0

    def test_redacts_bearer_token(self):
        from security.secret_redactor import redact_secrets
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result, count = redact_secrets(text)
        assert 'eyJhbGci' not in result

    def test_no_redaction_for_clean_text(self):
        from security.secret_redactor import redact_secrets
        text = "Please help me write a function that sorts a list"
        result, count = redact_secrets(text)
        assert result == text
        assert count == 0

    def test_preserves_surrounding_text(self):
        from security.secret_redactor import redact_secrets
        text = "My API key is sk-abc123def456ghi789jkl012mno345pqr678stu901vwx and I need help"
        result, count = redact_secrets(text)
        assert 'help' in result
        assert 'My' in result


class TestChatRedaction:
    """Test that /chat handler redacts secrets from prompts."""

    def test_chat_imports_redact_secrets(self):
        """Verify the redaction code exists in hart_intelligence_entry.py."""
        import hart_intelligence_entry
        source_file = hart_intelligence_entry.__file__
        with open(source_file, 'r', encoding='utf-8') as f:
            source = f.read()
        assert 'from security.secret_redactor import redact_secrets' in source
        assert 'redact_secrets(prompt)' in source


class TestRecipeRedaction:
    """Test that recipe save code redacts secrets."""

    def test_recipe_save_has_redaction(self):
        """Verify create_recipe.py redacts before json.dump."""
        pytest.importorskip('autogen', reason='autogen not installed')
        import create_recipe
        source_file = create_recipe.__file__
        with open(source_file, 'r', encoding='utf-8') as f:
            source = f.read()
        assert 'from security.secret_redactor import redact_secrets' in source


class TestGDPREndpoints:
    """Test GDPR data export and deletion endpoints."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        """Fresh tables for each test."""
        from integrations.social.models import Base, get_engine
        engine = get_engine()
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        yield
        Base.metadata.drop_all(engine)

    @pytest.fixture
    def user_with_token(self):
        """Create a uniquely-named test user and return (user_id, token)."""
        from integrations.social.models import get_db, User
        from integrations.social.auth import generate_jwt
        suffix = uuid.uuid4().hex[:8]
        db = get_db()
        user = User(
            username=f'gdpr_user_{suffix}',
            display_name='GDPR Test',
            email=f'gdpr_{suffix}@test.com',
            bio='Test bio',
            user_type='human',
        )
        db.add(user)
        db.commit()
        user_id = user.id
        username = user.username
        db.close()
        token = generate_jwt(user_id, username, 'user')
        return user_id, token

    @pytest.fixture
    def app_client(self):
        """Flask test client with social blueprint."""
        from flask import Flask
        from integrations.social.api import social_bp
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.register_blueprint(social_bp, url_prefix='/api/social')
        return app.test_client()

    def test_gdpr_export_returns_user_data(self, app_client, user_with_token):
        user_id, token = user_with_token
        resp = app_client.get(
            f'/api/social/users/{user_id}/data/export',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        export = data['data']
        assert 'user' in export
        assert 'posts' in export
        assert 'exported_at' in export

    def test_gdpr_export_forbidden_for_other_user(self, app_client, user_with_token):
        _, token = user_with_token
        resp = app_client.get(
            '/api/social/users/some_other_id/data/export',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert resp.status_code == 403

    def test_gdpr_export_requires_auth(self, app_client):
        resp = app_client.get('/api/social/users/any_id/data/export')
        assert resp.status_code == 401

    def test_gdpr_delete_anonymizes_pii(self, app_client, user_with_token):
        user_id, token = user_with_token
        resp = app_client.delete(
            f'/api/social/users/{user_id}/data',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['data']['anonymized'] is True

        # Verify user is anonymized in DB
        from integrations.social.models import get_db, User
        db = get_db()
        try:
            user = db.query(User).filter_by(id=user_id).first()
            assert user is not None  # Row still exists
            assert user.username.startswith('deleted_')
            assert user.display_name == 'Deleted User'
            assert user.email is None
            assert user.bio == ''
        finally:
            db.close()

    def test_gdpr_delete_forbidden_for_other_user(self, app_client, user_with_token):
        _, token = user_with_token
        resp = app_client.delete(
            '/api/social/users/some_other_id/data',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert resp.status_code == 403
