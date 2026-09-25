"""Regression test for POST /api/social/channels/bindings (create_binding).

Found + fixed 2026-07-21, while investigating the same "connected but
agent never replies" bug class as WhatsApp's gateway_qr flow (see
test_whatsapp_live_adapter.py), this time for token-based channels
(Telegram/Discord/Slack/...) connected via the mobile app's generic
ChannelSetupScreen "Connect" form.

Two distinct bugs:

1. The connect form (ChannelSetupScreen.js handleConnect) sends each
   metadata.setup_fields key at the TOP LEVEL of the request body — e.g.
   Telegram's field is 'bot_token', so the body is
   {'channel_type': 'telegram', 'bot_token': '123:ABC'}. create_binding()
   only ever read channel_sender_id/channel_chat_id/auth_method/metadata
   — 'bot_token' matched none of those, so the credential was silently
   dropped and never persisted anywhere. Fixed via _extract_credential(),
   which looks the value up by the channel's own declared setup_fields
   key and folds it into metadata_json.

2. Even with the credential persisted, nothing ever constructed a live
   adapter and registered it into the running ChannelRegistry — the
   binding row alone doesn't make FlaskChannelIntegration._handle_message
   receive anything. Fixed via _wire_live_adapter(), which calls the
   existing FlaskChannelIntegration.register_channel() factory (adds the
   adapter to the registry) and then explicitly schedules adapter.start()
   on the running channel event loop — register_channel() alone never
   does this since the one-time boot-time start_all() has already run by
   the time a user connects a channel at runtime.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest
from flask import Flask

from integrations.social.models import Base, get_engine, UserChannelBinding, db_session
from integrations.social.api import social_bp
from integrations.social.api_channels import channel_user_bp
from integrations.social.rate_limiter import get_limiter


@pytest.fixture
def app():
    test_app = Flask(__name__)
    test_app.config['TESTING'] = True
    test_app.register_blueprint(social_bp)
    test_app.register_blueprint(channel_user_bp)
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    get_limiter()._buckets.clear()
    yield test_app
    Base.metadata.drop_all(engine)


@pytest.fixture
def client(app):
    return app.test_client()


def _register(client, username='binding_tester'):
    resp = client.post('/api/social/auth/register', json={
        'username': username,
        'password': 'testpass123',
        'display_name': username.title(),
    })
    token = resp.get_json()['data']['api_token']
    return {'Authorization': f'Bearer {token}'}, resp.get_json()['data']['id']


def _mock_integration(existing_adapter=None):
    integration = MagicMock()
    integration.registry.get.return_value = existing_adapter
    integration._loop = MagicMock()
    integration._loop.is_running.return_value = True
    integration.register_channel.return_value = True
    return integration


def _fake_adapter(status, token):
    """An already-registered adapter in a specific health/credential state.

    A bare MagicMock is not enough here: _wire_live_adapter compares
    get_status() against real ChannelStatus members and config.token
    against the incoming credential, and a Mock silently matches neither.
    """
    from integrations.channels.base import ChannelStatus  # noqa: F401
    adapter = MagicMock()
    adapter.get_status.return_value = status
    adapter.config.token = token
    return adapter


class TestExtractCredential:
    def test_finds_declared_field_at_top_level(self):
        from integrations.social.api_channels import _extract_credential
        meta = {'setup_fields': [{'key': 'bot_token'}]}
        key, value = _extract_credential({'bot_token': '123:ABC'}, meta)
        assert key == 'bot_token'
        assert value == '123:ABC'

    def test_skips_auto_fields(self):
        from integrations.social.api_channels import _extract_credential
        meta = {'setup_fields': [
            {'key': 'api_url', 'auto': True},
            {'key': 'api_key'},
        ]}
        key, value = _extract_credential({'api_key': 'secret'}, meta)
        assert key == 'api_key'
        assert value == 'secret'

    def test_no_setup_fields_returns_none(self):
        from integrations.social.api_channels import _extract_credential
        key, value = _extract_credential({'anything': 'x'}, {'setup_fields': []})
        assert key is None and value is None


class TestCreateBindingPersistsCredential:
    """Bug 1: the token must actually end up in metadata_json."""

    def test_bot_token_persisted_to_metadata(self, client):
        headers, _ = _register(client)
        with patch('integrations.social.api_channels._wire_live_adapter',
                    return_value={'success': True}):
            resp = client.post('/api/social/channels/bindings', headers=headers, json={
                'channel_type': 'telegram', 'bot_token': '123456:ABC-DEF',
            })
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()['data']
        assert data['metadata_json'] is not None
        assert data['metadata_json'].get('bot_token') == '123456:ABC-DEF'

    def test_credential_survives_rebind(self, client):
        """Reconnecting (existing binding path) must not drop the token."""
        headers, _ = _register(client)
        with patch('integrations.social.api_channels._wire_live_adapter',
                    return_value={'success': True}):
            client.post('/api/social/channels/bindings', headers=headers, json={
                'channel_type': 'telegram', 'bot_token': 'first-token',
            })
            resp = client.post('/api/social/channels/bindings', headers=headers, json={
                'channel_type': 'telegram', 'bot_token': 'second-token',
            })
        assert resp.get_json()['data']['metadata_json']['bot_token'] == 'second-token'


class TestCreateBindingWiresLiveAdapter:
    """Bug 2: a live adapter must actually get registered + started."""

    def test_wire_live_adapter_called_with_credential(self, client):
        headers, _ = _register(client)
        with patch('integrations.social.api_channels._wire_live_adapter',
                    return_value={'success': True}) as wire_fn:
            resp = client.post('/api/social/channels/bindings', headers=headers, json={
                'channel_type': 'telegram', 'bot_token': '123:ABC',
            })
        assert resp.status_code == 201
        wire_fn.assert_called_once_with('telegram', '123:ABC')

    def test_live_adapter_failure_does_not_break_binding_creation(self, client):
        """A dead/unreachable Telegram API must not prevent the binding
        itself from being saved (worse than before would be a regression)."""
        headers, _ = _register(client)
        with patch('integrations.social.api_channels._wire_live_adapter',
                    return_value={'success': False, 'error': 'boom'}):
            resp = client.post('/api/social/channels/bindings', headers=headers, json={
                'channel_type': 'telegram', 'bot_token': '123:ABC',
            })
        assert resp.status_code == 201
        assert resp.get_json()['success'] is True

    def test_binding_only_channel_skips_wiring_gracefully(self, client):
        """A channel with no setup_fields (e.g. 'web') must not error out
        trying to wire a credential that doesn't exist."""
        headers, _ = _register(client)
        resp = client.post('/api/social/channels/bindings', headers=headers, json={
            'channel_type': 'web',
        })
        assert resp.status_code == 201, resp.get_json()


class TestWireLiveAdapterHelper:
    """Direct tests of _wire_live_adapter's control flow."""

    def test_registers_and_schedules_start(self):
        from integrations.social.api_channels import _wire_live_adapter

        integration = _mock_integration(existing_adapter=None)
        fake_adapter = MagicMock()
        # After register_channel() succeeds, registry.get() should return
        # the newly-registered adapter — simulate that state transition.
        integration.registry.get.side_effect = [None, fake_adapter]

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch('asyncio.run_coroutine_threadsafe') as run_coro:
            result = _wire_live_adapter('telegram', '123:ABC')

        assert result['success'] is True
        integration.register_channel.assert_called_once_with('telegram', token='123:ABC')
        run_coro.assert_called_once()
        assert run_coro.call_args.args[1] is integration._loop

    def test_idempotent_when_already_registered_and_healthy(self):
        """A live adapter holding the SAME credential is left alone.

        Narrowed 2026-08-04: this used to assert that ANY registered
        adapter short-circuited the helper, which is precisely the bug
        covered by test_rewires_dead_adapter_on_retry below. Healthy AND
        same-credential is the only case that legitimately no-ops.
        """
        from integrations.channels.base import ChannelStatus
        from integrations.social.api_channels import _wire_live_adapter

        already_live = _fake_adapter(ChannelStatus.CONNECTED, '123:ABC')
        integration = _mock_integration(existing_adapter=already_live)

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch('asyncio.run_coroutine_threadsafe') as run_coro:
            result = _wire_live_adapter('telegram', '123:ABC')

        assert result['success'] is True
        integration.register_channel.assert_not_called()
        run_coro.assert_not_called()

    def test_rewires_dead_adapter_on_retry(self):
        """A dead adapter must be rebuilt, not treated as already-wired.

        Real failure this covers (reproduced live against Discord on
        2026-08-04): a first connect attempt fails — mistyped token,
        revoked bot, or privileged intents not yet enabled in the Discord
        developer portal — leaving the adapter registered in ERROR.
        Resubmitting hit the old unconditional "already registered"
        short-circuit, so NO new connection was ever attempted: the
        channel stayed dead until someone restarted the server, while the
        API kept reporting success. The credential is deliberately
        unchanged here — enabling intents in the portal fixes the
        connection without changing the token at all.
        """
        from integrations.channels.base import ChannelStatus
        from integrations.social.api_channels import _wire_live_adapter

        dead = _fake_adapter(ChannelStatus.ERROR, '123:ABC')
        rebuilt = MagicMock()
        integration = _mock_integration(existing_adapter=dead)
        integration.registry.get.side_effect = [dead, rebuilt]

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch('asyncio.run_coroutine_threadsafe') as run_coro:
            result = _wire_live_adapter('telegram', '123:ABC')

        assert result['success'] is True
        integration.registry.unregister.assert_called_once_with('telegram')
        integration.register_channel.assert_called_once_with('telegram', token='123:ABC')
        # stop() on the stale adapter, then start() on its replacement.
        assert run_coro.call_count == 2

    def test_rewires_when_credential_changed(self):
        """A healthy adapter still holding a superseded token is replaced."""
        from integrations.channels.base import ChannelStatus
        from integrations.social.api_channels import _wire_live_adapter

        stale = _fake_adapter(ChannelStatus.CONNECTED, 'OLD:TOKEN')
        rebuilt = MagicMock()
        integration = _mock_integration(existing_adapter=stale)
        integration.registry.get.side_effect = [stale, rebuilt]

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch('asyncio.run_coroutine_threadsafe'):
            result = _wire_live_adapter('telegram', 'NEW:TOKEN')

        assert result['success'] is True
        integration.register_channel.assert_called_once_with('telegram', token='NEW:TOKEN')

    def test_connecting_adapter_is_left_in_flight(self):
        """An attempt still in progress is not restarted out from under itself."""
        from integrations.channels.base import ChannelStatus
        from integrations.social.api_channels import _wire_live_adapter

        in_flight = _fake_adapter(ChannelStatus.CONNECTING, '123:ABC')
        integration = _mock_integration(existing_adapter=in_flight)

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch('asyncio.run_coroutine_threadsafe'):
            result = _wire_live_adapter('telegram', '123:ABC')

        assert result['success'] is True
        integration.register_channel.assert_not_called()

    def test_teardown_failure_does_not_block_replacement(self):
        """A stale adapter that won't stop cleanly must not wedge the retry.

        An adapter that never finished connecting can raise on stop() —
        exactly the state a failed first attempt leaves behind — so that
        must not be allowed to block the reconnect.
        """
        from integrations.channels.base import ChannelStatus
        from integrations.social.api_channels import _wire_live_adapter

        dead = _fake_adapter(ChannelStatus.ERROR, '123:ABC')
        rebuilt = MagicMock()
        integration = _mock_integration(existing_adapter=dead)
        integration.registry.get.side_effect = [dead, rebuilt]

        def _explode(coro, loop):
            handle = MagicMock()
            handle.result.side_effect = RuntimeError('adapter never connected')
            return handle

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch('asyncio.run_coroutine_threadsafe', side_effect=_explode):
            result = _wire_live_adapter('telegram', '123:ABC')

        assert result['success'] is True
        integration.register_channel.assert_called_once_with('telegram', token='123:ABC')

    def test_no_credential_is_a_graceful_noop(self):
        from integrations.social.api_channels import _wire_live_adapter
        result = _wire_live_adapter('web', None)
        assert result['success'] is True

    def test_reports_failure_when_register_channel_fails(self):
        from integrations.social.api_channels import _wire_live_adapter

        integration = _mock_integration(existing_adapter=None)
        integration.register_channel.return_value = False

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ):
            result = _wire_live_adapter('telegram', 'bad-token')

        assert result['success'] is False
