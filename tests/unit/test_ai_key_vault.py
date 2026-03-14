"""
Tests for desktop.ai_key_vault.AIKeyVault.

Verifies singleton, channel key resolution, delegation to SecretsManager,
credential storage, env preloading, pending request tracking, and endpoints.

Run with: pytest tests/unit/test_ai_key_vault.py -v --noconftest
"""
import os
import sys
import json
import threading
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from desktop.ai_key_vault import AIKeyVault, get_ai_key_vault, is_local_request


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset vault singleton before/after each test."""
    AIKeyVault.reset()
    import desktop.ai_key_vault as mod
    mod._instance = None
    yield
    AIKeyVault.reset()
    mod._instance = None


@pytest.fixture
def mock_sm():
    """Mock SecretsManager to avoid needing HEVOLVE_MASTER_KEY."""
    sm = MagicMock()
    sm.get_secret.return_value = ''
    sm._cache = {}

    with patch('desktop.ai_key_vault.AIKeyVault._secrets_manager',
               return_value=sm):
        yield sm


# ═══════════════════════════════════════════════════════════════
# 1. Singleton behavior
# ═══════════════════════════════════════════════════════════════

class TestSingleton:

    def test_get_instance_returns_same_object(self):
        a = AIKeyVault.get_instance()
        b = AIKeyVault.get_instance()
        assert a is b

    def test_reset_clears_singleton(self):
        a = AIKeyVault.get_instance()
        AIKeyVault.reset()
        b = AIKeyVault.get_instance()
        assert a is not b

    def test_module_level_function(self):
        v = get_ai_key_vault()
        assert isinstance(v, AIKeyVault)
        assert v is get_ai_key_vault()

    def test_thread_safe_singleton(self):
        results = []

        def grab():
            results.append(AIKeyVault.get_instance())

        threads = [threading.Thread(target=grab) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(id(r) for r in results)) == 1


# ═══════════════════════════════════════════════════════════════
# 2. Channel key resolution
# ═══════════════════════════════════════════════════════════════

class TestChannelKeyResolution:

    def test_basic(self):
        assert AIKeyVault._resolve_channel_key('discord', 'BOT_TOKEN') == 'DISCORD_BOT_TOKEN'

    def test_case_normalization(self):
        assert AIKeyVault._resolve_channel_key('Discord', 'bot_token') == 'DISCORD_BOT_TOKEN'

    def test_empty_channel(self):
        assert AIKeyVault._resolve_channel_key('', 'API_KEY') == 'API_KEY'

    def test_dedup_prefix(self):
        assert AIKeyVault._resolve_channel_key('discord', 'DISCORD_BOT_TOKEN') == 'DISCORD_BOT_TOKEN'

    def test_slack_multikey(self):
        assert AIKeyVault._resolve_channel_key('slack', 'APP_TOKEN') == 'SLACK_APP_TOKEN'


# ═══════════════════════════════════════════════════════════════
# 3. Retrieval
# ═══════════════════════════════════════════════════════════════

class TestRetrieval:

    def test_get_tool_key_delegates(self, mock_sm):
        mock_sm.get_secret.return_value = 'sk-abc123'
        vault = AIKeyVault.get_instance()
        result = vault.get_tool_key('OPENAI_API_KEY')
        assert result == 'sk-abc123'
        mock_sm.get_secret.assert_called_with('OPENAI_API_KEY')

    def test_get_tool_key_missing(self, mock_sm):
        mock_sm.get_secret.return_value = ''
        vault = AIKeyVault.get_instance()
        assert vault.get_tool_key('NONEXISTENT') == ''

    def test_get_channel_secret_resolves(self, mock_sm):
        mock_sm.get_secret.return_value = 'tg-token'
        vault = AIKeyVault.get_instance()
        result = vault.get_channel_secret('telegram', 'BOT_TOKEN')
        assert result == 'tg-token'
        mock_sm.get_secret.assert_called_with('TELEGRAM_BOT_TOKEN')

    def test_get_channel_secret_value(self, mock_sm):
        mock_sm.get_secret.return_value = 'disc-token-xyz'
        vault = AIKeyVault.get_instance()
        assert vault.get_channel_secret('discord', 'BOT_TOKEN') == 'disc-token-xyz'


# ═══════════════════════════════════════════════════════════════
# 4. Storage
# ═══════════════════════════════════════════════════════════════

class TestStorage:

    def test_store_delegates_to_sm(self, mock_sm):
        vault = AIKeyVault.get_instance()
        vault.store_credential('MY_KEY', 'my-value')
        mock_sm.set_secret.assert_called_once_with('MY_KEY', 'my-value')

    def test_store_injects_env(self, mock_sm):
        vault = AIKeyVault.get_instance()
        vault.store_credential('TEST_VAULT_KEY', 'test-value')
        assert os.environ.get('TEST_VAULT_KEY') == 'test-value'
        os.environ.pop('TEST_VAULT_KEY', None)

    def test_store_with_channel_type(self, mock_sm):
        vault = AIKeyVault.get_instance()
        resolved = vault.store_credential('BOT_TOKEN', 'abc', channel_type='discord')
        assert resolved == 'DISCORD_BOT_TOKEN'
        mock_sm.set_secret.assert_called_once_with('DISCORD_BOT_TOKEN', 'abc')
        assert os.environ.get('DISCORD_BOT_TOKEN') == 'abc'
        os.environ.pop('DISCORD_BOT_TOKEN', None)

    def test_store_fallback_env_only(self, mock_sm):
        """When vault is unavailable, falls back to env-only."""
        mock_sm.set_secret.side_effect = RuntimeError("no master key")
        vault = AIKeyVault.get_instance()
        vault.store_credential('FALLBACK_KEY', 'val')
        assert os.environ.get('FALLBACK_KEY') == 'val'
        os.environ.pop('FALLBACK_KEY', None)


# ═══════════════════════════════════════════════════════════════
# 5. Preload
# ═══════════════════════════════════════════════════════════════

class TestPreload:

    def test_preload_loads_cached_secrets(self, mock_sm):
        mock_sm._cache = {'PRELOAD_A': 'val_a', 'PRELOAD_B': 'val_b'}
        vault = AIKeyVault.get_instance()
        count = vault.preload_env()
        assert count == 2
        assert os.environ.get('PRELOAD_A') == 'val_a'
        assert os.environ.get('PRELOAD_B') == 'val_b'
        os.environ.pop('PRELOAD_A', None)
        os.environ.pop('PRELOAD_B', None)

    def test_preload_skips_existing(self, mock_sm):
        os.environ['EXISTING_KEY'] = 'original'
        mock_sm._cache = {'EXISTING_KEY': 'vault_value'}
        vault = AIKeyVault.get_instance()
        count = vault.preload_env()
        assert count == 0
        assert os.environ['EXISTING_KEY'] == 'original'
        os.environ.pop('EXISTING_KEY', None)


# ═══════════════════════════════════════════════════════════════
# 6. Pending request tracking
# ═══════════════════════════════════════════════════════════════

class TestPendingRequests:

    def test_add_pending(self, mock_sm):
        vault = AIKeyVault.get_instance()
        rid = vault.add_pending_request('OPENAI_API_KEY', used_by='web_search')
        assert rid
        pending = vault.get_pending_requests()
        assert len(pending) == 1
        assert pending[0]['key_name'] == 'OPENAI_API_KEY'
        assert pending[0]['used_by'] == 'web_search'

    def test_pending_dedup(self, mock_sm):
        vault = AIKeyVault.get_instance()
        rid1 = vault.add_pending_request('GROQ_API_KEY')
        rid2 = vault.add_pending_request('GROQ_API_KEY')
        assert rid1 == rid2
        assert len(vault.get_pending_requests()) == 1

    def test_store_clears_pending(self, mock_sm):
        vault = AIKeyVault.get_instance()
        vault.add_pending_request('CLEAR_ME_KEY')
        assert vault.has_pending('CLEAR_ME_KEY')
        vault.store_credential('CLEAR_ME_KEY', 'val')
        assert not vault.has_pending('CLEAR_ME_KEY')
        os.environ.pop('CLEAR_ME_KEY', None)

    def test_has_pending(self, mock_sm):
        vault = AIKeyVault.get_instance()
        assert not vault.has_pending('NOPE')
        vault.add_pending_request('NOPE')
        assert vault.has_pending('NOPE')


# ═══════════════════════════════════════════════════════════════
# 7. Full roundtrip
# ═══════════════════════════════════════════════════════════════

class TestRoundtrip:

    def test_request_store_retrieve(self, mock_sm):
        vault = AIKeyVault.get_instance()

        # Agent requests a credential
        rid = vault.add_pending_request(
            'SERPAPI_API_KEY', resource_type='api_key',
            label='SerpAPI Key', used_by='web_search',
        )
        assert vault.has_pending('SERPAPI_API_KEY')

        # User submits via frontend
        mock_sm.get_secret.return_value = 'serp-key-123'
        vault.store_credential('SERPAPI_API_KEY', 'serp-key-123')

        # Pending cleared
        assert not vault.has_pending('SERPAPI_API_KEY')

        # Agent can now retrieve it
        assert vault.get_tool_key('SERPAPI_API_KEY') == 'serp-key-123'

        os.environ.pop('SERPAPI_API_KEY', None)


# ═══════════════════════════════════════════════════════════════
# 8. Credential endpoint tests
# ═══════════════════════════════════════════════════════════════

class TestCredentialEndpoints:

    @pytest.fixture
    def client(self, mock_sm):
        """Flask test client with credential endpoints."""
        from flask import Flask, request as flask_request, jsonify
        app = Flask(__name__)
        app.config['TESTING'] = True

        @app.route('/api/credentials/submit', methods=['POST'])
        def submit():
            data = flask_request.get_json(silent=True) or {}
            key_name = (data.get('key_name') or '').strip()
            value = (data.get('value') or '').strip()
            if not key_name or not value:
                return jsonify({'error': 'key_name and value are required'}), 400
            vault = AIKeyVault.get_instance()
            resolved = vault.store_credential(
                key_name=key_name, value=value,
                channel_type=data.get('channel_type', ''),
            )
            return jsonify({'success': True, 'key_name': resolved})

        @app.route('/api/credentials/pending', methods=['GET'])
        def pending():
            vault = AIKeyVault.get_instance()
            return jsonify({'pending': vault.get_pending_requests()})

        with app.test_client() as c:
            yield c

    def test_submit_success(self, client, mock_sm):
        resp = client.post('/api/credentials/submit',
                           json={'key_name': 'TEST_EP_KEY', 'value': 'ep-val'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['key_name'] == 'TEST_EP_KEY'
        os.environ.pop('TEST_EP_KEY', None)

    def test_submit_missing_key(self, client):
        resp = client.post('/api/credentials/submit',
                           json={'value': 'val'})
        assert resp.status_code == 400

    def test_submit_empty_value(self, client):
        resp = client.post('/api/credentials/submit',
                           json={'key_name': 'K', 'value': ''})
        assert resp.status_code == 400

    def test_pending_empty(self, client):
        resp = client.get('/api/credentials/pending')
        assert resp.status_code == 200
        assert resp.get_json()['pending'] == []

    def test_pending_after_add_and_submit(self, client, mock_sm):
        vault = AIKeyVault.get_instance()
        vault.add_pending_request('WAIT_KEY', label='Waiting Key')
        resp = client.get('/api/credentials/pending')
        pending = resp.get_json()['pending']
        assert len(pending) == 1
        assert pending[0]['key_name'] == 'WAIT_KEY'

        # Submit clears it
        client.post('/api/credentials/submit',
                    json={'key_name': 'WAIT_KEY', 'value': 'done'})
        resp = client.get('/api/credentials/pending')
        assert resp.get_json()['pending'] == []
        os.environ.pop('WAIT_KEY', None)


# ═══════════════════════════════════════════════════════════════
# 9. Localhost enforcement
# ═══════════════════════════════════════════════════════════════

class TestLocalhostEnforcement:

    def test_localhost_ipv4(self):
        assert is_local_request('127.0.0.1') is True

    def test_localhost_ipv6(self):
        assert is_local_request('::1') is True

    def test_localhost_name(self):
        assert is_local_request('localhost') is True

    def test_bind_all(self):
        assert is_local_request('0.0.0.0') is True

    def test_external_ip_rejected(self):
        assert is_local_request('192.168.1.100') is False

    def test_public_ip_rejected(self):
        assert is_local_request('8.8.8.8') is False

    def test_empty_rejected(self):
        assert is_local_request('') is False

    def test_none_rejected(self):
        assert is_local_request(None) is False


class TestEndpointLocalhostGate:
    """Credential endpoints must reject non-local requests."""

    @pytest.fixture
    def gated_client(self, mock_sm):
        from flask import Flask, request as flask_request, jsonify
        app = Flask(__name__)
        app.config['TESTING'] = True

        @app.route('/api/credentials/submit', methods=['POST'])
        def submit():
            if not is_local_request(flask_request.remote_addr):
                return jsonify({'error': 'localhost only'}), 403
            data = flask_request.get_json(silent=True) or {}
            key_name = (data.get('key_name') or '').strip()
            value = (data.get('value') or '').strip()
            if not key_name or not value:
                return jsonify({'error': 'key_name and value required'}), 400
            vault = AIKeyVault.get_instance()
            resolved = vault.store_credential(key_name=key_name, value=value)
            return jsonify({'success': True, 'key_name': resolved})

        @app.route('/api/credentials/pending', methods=['GET'])
        def pending():
            if not is_local_request(flask_request.remote_addr):
                return jsonify({'error': 'localhost only'}), 403
            vault = AIKeyVault.get_instance()
            return jsonify({'pending': vault.get_pending_requests()})

        with app.test_client() as c:
            yield c

    def test_submit_from_localhost_ok(self, gated_client, mock_sm):
        """Flask test client uses 127.0.0.1 by default — should pass."""
        resp = gated_client.post('/api/credentials/submit',
                                 json={'key_name': 'LOCAL_KEY', 'value': 'v'})
        assert resp.status_code == 200
        os.environ.pop('LOCAL_KEY', None)

    def test_pending_from_localhost_ok(self, gated_client):
        resp = gated_client.get('/api/credentials/pending')
        assert resp.status_code == 200
