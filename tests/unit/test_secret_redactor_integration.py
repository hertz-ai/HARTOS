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

    @pytest.mark.parametrize('line', [
        'api_key=abcd1234efgh',
        'apikey: 9f8e7d6c',
        'secret=hunter2',                       # 7 chars — under the old {8,} floor
        'token=shortvalue',                     # under bearer_token's {32,} floor
        'api_key=12345678-1234-1234-1234-123456789abc',
        'X-Api-Key: 12345678-1234-1234-1234-123456789abc',
        '"secret": "12345678-1234-1234-1234-123456789abc"',   # JSON: key's own quote
        'secret=abc',            # 3 chars — a short secret is still a secret
        'token=nonesuch',        # status-word exclusion is WHOLE-value only
    ])
    def test_redacts_secret_named_assignments(self, line):
        """security/audit_log.py delegated its redaction here and LOST coverage.

        The block it deleted carried a generic
        `(password|passwd|pwd|secret|token|api_key|apikey)\\s*[=:]\\s*\\S+`.
        The canonical set had no equivalent, so every line below reached the
        AUDIT LOG in clear text — the artifact whose whole purpose is being safe
        to keep and hand over.

        They escaped three ways: password_assignment lists the right names but
        demands a QUOTED value (and in JSON the key's own closing quote breaks
        its `\\s*[=:]`), password_plaintext allows unquoted but only for
        password/passwd/pwd, and both require {8,} while `secret=hunter2` is 7.
        """
        from security.secret_redactor import redact_secrets
        result, count = redact_secrets(line)
        assert result != line, f'not redacted at all: {line!r} -> {result!r}'
        assert count > 0
        # the secret VALUE must be gone, not merely the key name
        for leak in ('abcd1234efgh', '9f8e7d6c', 'hunter2', 'shortvalue',
                     '123456789abc'):
            if leak in line:
                assert leak not in result, f'{leak!r} survived in {result!r}'

    @pytest.mark.parametrize('line', [
        'node_id=12345678-1234-1234-1234-123456789abc',
        'request_id=abc123def',
        'trace_id=9f8e7d6c',
        'bare 12345678-1234-1234-1234-123456789abc in prose',
        'auth=no',
        'token=on',
        'next_token_count=5',
        # Status values are DIAGNOSTICS. In an auth-failure investigation
        # "no token was present" and "a token was present" are different facts;
        # redacting both collapses them. Named explicitly rather than protected
        # by a length floor, which was only ever a proxy for this.
        'token=null',
        'auth_token=missing',
        'secret=none',
        'token=expired',
        'token=invalid',
    ])
    def test_identifiers_stay_visible(self, line):
        """The counterpart guard: widening the NAMES must never widen the SHAPE.

        heroku_key once matched bare UUIDs and redacted every identifier in the
        logs, destroying the "which node failed" diagnostic. Narrowing it was
        correct. So a fix for the gap above must not reintroduce that: only
        SECRET-named keys are redacted, and the {6,} floor keeps `auth=no` and
        `token=on` readable.
        """
        from security.secret_redactor import redact_secrets
        result, _ = redact_secrets(line)
        assert result == line, f'over-redacted a diagnostic: {line!r} -> {result!r}'

    def test_no_redaction_for_clean_text(self):
        from security.secret_redactor import redact_secrets
        text = "Please help me write a function that sorts a list"
        result, count = redact_secrets(text)
        assert result == text
        assert count == 0

    # Review of 2e0b388fd, measured: neither shape was known to the one
    # redactor, so both reached a setup failure's answer in clear text.
    @pytest.mark.parametrize('token, name', [
        ('hf_' + 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3l', 'huggingface_token'),     # 34 after hf_
        ('glpat-' + 'x9Yz_AbC-dEf12345678', 'gitlab_token'),                    # 20 after glpat-
        ('glpat-' + 'AbCdEfGhIjKlMnOpQrStUvWxYz012345', 'gitlab_token'),
    ])
    def test_redacts_hugging_face_and_gitlab_tokens(self, token, name):
        from security.secret_redactor import redact_secrets
        for line in (f'ERROR: 401 for https://huggingface.co/x with {token}',
                     f'git clone https://oauth2:{token}@gitlab.com/a/b.git failed',
                     token):
            result, count = redact_secrets(line)
            assert token not in result and count >= 1, result
        assert redact_secrets(f'key {token} refused') == (f'key [REDACTED:{name}] refused', 1)

    @pytest.mark.parametrize('text', [
        'set HEVOLVE_HF_TOKEN in the vault',           # a key's name is no token
        'HF_HUB_ENABLE_HF_TRANSFER=1',
        'hf_token() answered nothing',
        'the hf_short value',                          # too short to be one
        'glpat- is how a GitLab token starts',
    ])
    def test_a_word_that_only_starts_like_a_token_is_left_alone(self, text):
        from security.secret_redactor import redact_secrets
        assert redact_secrets(text) == (text, 0)

    # Three shapes a failed install prints (review probe on 2e0b388fd).
    @pytest.mark.parametrize('line, gone, kept', [
        ('ERROR: request failed: Authorization: Bearer FAKEbearerTOKENvalue12345678',
         'FAKEbearerTOKENvalue12345678', 'ERROR: request failed: Authorization: '),
        ('curl -H "Authorization: Bearer abc123def456ghi7" https://api.example/x',
         'abc123def456ghi7', 'https://api.example/x'),
        ('ERROR: pip install --index-url https://pypi.internal.example/simple '
         '--password FAKEpwd_plain_77 crawl4ai failed',
         'FAKEpwd_plain_77', 'crawl4ai failed'),
        ('twine upload --token pypi9AgEIcHlwaS5vcmc dist/*', 'pypi9AgEIcHlwaS5vcmc', 'dist/*'),
        ('tool --api-key=Zx81Qq77Lm --verbose', 'Zx81Qq77Lm', '--verbose'),
        ('ERROR: 401 for https://alice:tok/FAKE123secretzz@pypi.internal.example/simple',
         'tok/FAKE123secretzz', 'pypi.internal.example/simple'),
        ('git clone https://bob:a/b/c9@git.example.org/x.git', 'a/b/c9', 'git.example.org/x.git'),
    ], ids=['short-bearer', 'short-bearer-quoted', 'password-flag', 'token-flag',
            'api-key-flag', 'login-with-a-slash', 'login-with-slashes'])
    def test_redacts_a_short_bearer_a_password_flag_and_a_login_with_a_slash(self, line, gone,
                                                                            kept):
        from security.secret_redactor import redact_secrets
        result, count = redact_secrets(line)
        assert gone not in result and count >= 1, result
        assert kept in result, result
        assert 'alice' not in result and 'bob:' not in result

    # Ordinary text, key NAMES and addresses that only look alike stay as
    # they are written: nothing here is a credential.
    @pytest.mark.parametrize('text', [
        # prose with the words
        'the bearer of this letter is trusted',
        'Bearer tokens are sent in the Authorization header',
        'a bearer responsibilities document',
        'Authorization: Bearer <token>',
        'use Bearer authentication',
        # flags named, not given
        'pip needs --password to be set',
        '--password is required when --username is given',
        'pass --token on the command line',
        'the --api-key flag',
        '--secret must not be empty',
        '--password <value>',
        '--password PASSWORD',
        '--token $PIP_TOKEN',
        '--password %PIP_PASSWORD%',
        '--token {{secret:PIP_TOKEN}}',
        '--password --verbose',
        '--password-file creds.txt',
        '--tokenizer bert-base-uncased2',
        # key names
        'set HEVOLVE_MASTER_KEY and HEVOLVE_HF_TOKEN',
        'PIP_INDEX_URL and PIP_PASSWORD are read from the environment',
        # addresses
        'https://pypi.org/simple/crawl4ai/',
        'https://github.com/x/y@v1.2',
        'http://localhost:8080/a/b@c.example',
        'http://127.0.0.1:5000/api/tools/status',
        'https://host.example:443/path/to/x@2x.png',
        'git@github.com:org/repo.git',
        'mailto:someone@example.org',
        'see docs at https://example.org/a/b: then write to help@example.org',
        # a page reader's words
        'No matching distribution found for crawl4ai (from versions: 0.9.1, 0.9.5)',
        'Check the internet connection and the free disk space, then set crawl4ai up again.',
    ])
    def test_ordinary_text_is_not_masked(self, text):
        from security.secret_redactor import redact_secrets
        assert redact_secrets(text) == (text, 0)

    def test_a_long_bearer_is_still_the_long_rules(self):
        """Carried over: 32 characters and more belong to bearer_token."""
        from security.secret_redactor import redact_secrets
        value = 'abcdefghijklmnopqrstuvwxyz0123456789ABCD'
        result, count = redact_secrets(f'Authorization: Bearer {value}')
        assert value not in result and '[REDACTED:bearer_token]' in result
        assert '[REDACTED:short_bearer]' not in result

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
        source_file = getattr(hart_intelligence_entry, '__file__', None)
        if not source_file:
            pytest.skip("hart_intelligence_entry has no __file__ (compiled or partial import)")
        with open(source_file, 'r', encoding='utf-8') as f:
            source = f.read()
        assert 'from security.secret_redactor import redact_secrets' in source
        assert 'redact_secrets(prompt)' in source


class TestRecipeRedaction:
    """Test that recipe save code redacts secrets."""

    def test_recipe_save_has_redaction(self):
        """Verify create_recipe.py redacts before json.dump."""
        pytest.importorskip('autogen', reason='autogen not installed')
        from hartos import create_recipe
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
