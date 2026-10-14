"""A channel's bot token is kept encrypted in its binding when it can be.

Review of #130: the chat connect (register_channel) and the /bindings form
kept the token in UserChannelBinding.metadata_json as plain text, where boot
restore reads it back.  The secrets vault (security/secrets_manager.py,
Fernet under HEVOLVE_MASTER_KEY) is where secrets go.  Both writers now seal
the token with it and restore unseals it.  A node without HEVOLVE_MASTER_KEY
has no key to encrypt with: the token is still kept (the channel must come
back after a restart) in plain text, with a warning that says so and how to
fix it.

These run the real writers and restore against the real SecretsManager, with
a throwaway key and vault paths; only the DB, the admin config store and the
live adapter are mocked.
"""
import logging
import types
from unittest.mock import MagicMock, Mock, patch

import pytest
from flask import Flask, g

from security import secrets_manager
from security.secrets_manager import SEALED_PREFIX, SecretsManager
from tests.unit.module_swap import swap_modules

TOKEN = '123456:ABC-sealing-test'


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A SecretsManager with (key=True) or without a key, on temp files."""
    monkeypatch.setattr(secrets_manager, '_SALT_PATH', str(tmp_path / 'salt'))
    monkeypatch.setattr(secrets_manager, '_VAULT_PATH', str(tmp_path / 'vault'))

    def make(key=True):
        if key:
            monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'test-only-vault-passphrase')
        else:
            monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
        SecretsManager.reset()
        return SecretsManager.get_instance()
    yield make
    SecretsManager.reset()


# ─── SecretsManager: seal and open one value ────────────────────────────

class TestValueEncryption:
    def test_round_trip(self, vault):
        sm = vault()
        sealed = sm.encrypt_value(TOKEN)
        assert sealed.startswith(SEALED_PREFIX) and TOKEN not in sealed
        assert sm.decrypt_value(sealed) == TOKEN

    def test_plain_text_reads_as_itself(self, vault):
        """Rows written before sealing, or on a keyless node, stay readable."""
        assert vault().decrypt_value(TOKEN) == TOKEN
        assert vault(key=False).decrypt_value(TOKEN) == TOKEN

    def test_no_key_cannot_encrypt(self, vault):
        with pytest.raises(RuntimeError, match='HEVOLVE_MASTER_KEY'):
            vault(key=False).encrypt_value(TOKEN)

    def test_a_sealed_value_without_the_key_is_an_error(self, vault):
        sealed = vault().encrypt_value(TOKEN)
        with pytest.raises(ValueError, match='HEVOLVE_MASTER_KEY'):
            vault(key=False).decrypt_value(sealed)

    def test_a_sealed_value_under_another_key_is_an_error(self, vault, monkeypatch):
        sealed = vault().encrypt_value(TOKEN)
        monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'a-different-passphrase')
        SecretsManager.reset()
        with pytest.raises(ValueError):
            SecretsManager.get_instance().decrypt_value(sealed)


# ─── The writers: chat connect and the /bindings form ───────────────────

class _FakeApi:
    _channels = {}

    def _save_config(self):
        pass


def _chat_connect(channel, config_json):
    """The register_channel tool, with the binding DB, the admin config
    store and the live adapter as boundaries.  Returns the added row."""
    from integrations.channels.agent_tools import build_channel_tool_closures
    tools = build_channel_tool_closures({'user_id': 'u-1', 'prompt_id': None})
    register = next(t[2] for t in tools if t[0] == 'register_channel')
    added = []
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    db.add.side_effect = added.append
    with patch('integrations.channels.admin.api.get_api', return_value=_FakeApi()), \
            patch('integrations.social.models.get_db', return_value=db), \
            patch('integrations.social.api_channels._wire_live_adapter',
                  return_value={'success': True}) as wire, \
            patch('integrations.channels.agent_tools._report_when_live'):
        register(channel, config_json)
    (row,) = added
    return row, wire


def _form_connect(channel, body):
    """POST /api/social/channels/bindings as the signed-in user."""
    from integrations.social import api_channels
    app = Flask(__name__)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    added = []
    db.add.side_effect = added.append
    with patch.object(api_channels, '_wire_live_adapter',
                      return_value={'success': False, 'error': 'test'}) as wire, \
            app.test_request_context('/api/social/channels/bindings',
                                     method='POST', json=body):
        g.user_id, g.db = 'u-1', db
        api_channels.create_binding.__wrapped__()
    (row,) = added
    return row, wire


class TestWritersSealTheToken:
    def test_chat_connect_stores_it_encrypted(self, vault):
        sm = vault()
        row, wire = _chat_connect('telegram', '{"bot_token": "%s"}' % TOKEN)
        stored = row.metadata_json['bot_token']
        assert stored.startswith(SEALED_PREFIX) and TOKEN not in str(row.metadata_json)
        assert sm.decrypt_value(stored) == TOKEN
        # The live adapter still gets the real token.
        wire.assert_called_once_with('telegram', TOKEN)

    def test_the_form_stores_it_encrypted(self, vault):
        sm = vault()
        row, wire = _form_connect('telegram', {'channel_type': 'telegram',
                                               'bot_token': TOKEN})
        stored = row.metadata_json['bot_token']
        assert TOKEN not in str(row.metadata_json)
        assert sm.decrypt_value(stored) == TOKEN
        wire.assert_called_once_with('telegram', TOKEN)

    def test_without_a_key_it_is_kept_in_plain_text_and_said_loudly(
            self, vault, caplog):
        vault(key=False)
        with caplog.at_level(logging.WARNING):
            row, _wire = _chat_connect('telegram', '{"bot_token": "%s"}' % TOKEN)
        assert row.metadata_json == {'bot_token': TOKEN}
        warned = [r.getMessage() for r in caplog.records
                  if r.levelno == logging.WARNING and 'PLAIN TEXT' in r.getMessage()]
        assert len(warned) == 1 and 'HEVOLVE_MASTER_KEY' in warned[0]
        assert 'telegram' in warned[0]
        # The warning names the channel, never the token.
        assert TOKEN not in caplog.text


# ─── Boot restore reads what the writers stored ─────────────────────────

def _restore(meta):
    """restore_persisted_channels over one binding row; returns the token
    register_channel was given (None when it was not called)."""
    from integrations.channels.flask_integration import FlaskChannelIntegration
    fi = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    fi.registry = Mock()
    fi.registry.get.return_value = None
    fi.register_channel = Mock(return_value=True)
    row = types.SimpleNamespace(channel_type='telegram', metadata_json=meta,
                                id=1, updated_at=None, is_active=True)
    db = Mock()
    db.query.return_value.filter_by.return_value.all.return_value = [row]
    models = types.ModuleType('integrations.social.models')
    models.get_db = lambda: db
    models.UserChannelBinding = object
    with swap_modules({'integrations.social.models': models}):
        summary = fi.restore_persisted_channels()
    calls = fi.register_channel.call_args_list
    return (calls[0].kwargs.get('token') if calls else None), summary


class TestRestoreUnseals:
    def test_a_sealed_token_restores_the_channel(self, vault):
        sealed = vault().encrypt_value(TOKEN)
        token, summary = _restore({'bot_token': sealed})
        assert token == TOKEN and summary['restored'] == ['telegram']

    def test_a_plain_text_token_still_restores(self, vault):
        vault(key=False)
        token, summary = _restore({'bot_token': TOKEN})
        assert token == TOKEN and summary['restored'] == ['telegram']

    def test_a_token_that_cannot_be_decrypted_is_an_error_not_a_login(
            self, vault, caplog):
        """The ciphertext must never reach the adapter as if it were the
        token; the channel stays offline and the log says why."""
        sealed = vault().encrypt_value(TOKEN)
        vault(key=False)
        with caplog.at_level(logging.WARNING):
            token, summary = _restore({'bot_token': sealed})
        assert token is None and 'telegram' in summary['skipped']
        errors = [r.getMessage() for r in caplog.records
                  if r.levelno >= logging.ERROR]
        assert any('telegram' in e and 'decrypt' in e for e in errors), errors
        assert sealed not in caplog.text
