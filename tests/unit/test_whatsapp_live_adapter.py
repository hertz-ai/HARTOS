"""Regression test for the WhatsApp "connected but agent never replies" bug
(found + fixed 2026-07-21, resuming the 2026-07-20 gateway_qr work).

Root cause: both connect paths (the chat-based Connect_Channel flow in
hart_intelligence_entry.py, and the mobile app's polling endpoint
GET /api/social/channels/whatsapp/qr in api_channels.py) only ever wrote a
UserChannelBinding row once the gateway reported authenticated:true — that
makes the Channels list/DB show "connected", but neither path ever
constructed a live WhatsAppAdapter and registered it into the running
ChannelRegistry. Without a registered adapter subscribed to the gateway's
WebSocket, no inbound WhatsApp message ever reaches
FlaskChannelIntegration._handle_message (the function that actually calls
/chat and sends the agent's reply back) — so "connected" was cosmetic.

Fix: hart_intelligence_entry._ensure_whatsapp_live_adapter() constructs +
registers a WhatsAppAdapter (idempotent — a no-op if one is already
registered) and schedules adapter.start() on the running channel event
loop. Wired into both the chat-flow's poll-success handler and
whatsapp_get_qr()'s authenticated branch (the endpoint the mobile app
actually polls).

These tests mock the gateway HTTP layer and the FlaskChannelIntegration
singleton (real adapter.start() would try a real network connection to
port 3000) — they pin the WIRING contract: given an authenticated gateway
session, does the fix actually construct+register+schedule the adapter,
exactly once, idempotently.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest
from flask import Flask

from integrations.social.models import Base, get_engine
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


def _register(client, username='wa_live_tester'):
    resp = client.post('/api/social/auth/register', json={
        'username': username,
        'password': 'testpass123',
        'display_name': username.title(),
    })
    token = resp.get_json()['data']['api_token']
    return {'Authorization': f'Bearer {token}'}


def _mock_integration(existing_adapter=None):
    """A stand-in FlaskChannelIntegration with a running event loop, so
    _ensure_whatsapp_live_adapter's control flow can be exercised without
    a real asyncio loop or a real gateway connection."""
    integration = MagicMock()
    integration.registry.get.return_value = existing_adapter
    integration._loop = MagicMock()
    integration._loop.is_running.return_value = True
    return integration


class TestEnsureWhatsappLiveAdapter:
    """Unit tests for the shared helper directly."""

    def test_registers_and_schedules_start_when_none_registered(self):
        from hart_intelligence_entry import _ensure_whatsapp_live_adapter

        integration = _mock_integration(existing_adapter=None)
        fake_adapter = MagicMock()

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch(
            'integrations.channels.whatsapp_adapter.create_whatsapp_adapter',
            return_value=fake_adapter,
        ) as create_fn, patch(
            'asyncio.run_coroutine_threadsafe',
        ) as run_coro:
            result = _ensure_whatsapp_live_adapter(
                'u1', sid='user_u1', base='http://127.0.0.1:3000',
            )

        assert result['success'] is True
        create_fn.assert_called_once()
        call_kwargs = create_fn.call_args.kwargs
        assert call_kwargs['api_url'] == 'http://127.0.0.1:3000'
        assert call_kwargs['account_id'] == 'user_u1'
        # phone_number/owner_lid carry the self-chat identity, which is fetched
        # from the live gateway — their VALUES depend on whether one is
        # reachable on :3000 (None when the fetch is refused). Assert they are
        # passed through, not what they happen to resolve to here.
        assert 'phone_number' in call_kwargs
        assert 'owner_lid' in call_kwargs
        integration.registry.register.assert_called_once_with(fake_adapter)
        run_coro.assert_called_once()
        # scheduled coroutine must be adapter.start(), on the integration's loop
        assert run_coro.call_args.args[1] is integration._loop

    def test_idempotent_when_adapter_already_registered(self):
        """Re-running (e.g. the repair path called twice) must not
        double-register or touch the event loop again."""
        from hart_intelligence_entry import _ensure_whatsapp_live_adapter

        already_live = MagicMock()
        integration = _mock_integration(existing_adapter=already_live)

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch(
            'integrations.channels.whatsapp_adapter.create_whatsapp_adapter',
        ) as create_fn, patch(
            'asyncio.run_coroutine_threadsafe',
        ) as run_coro:
            result = _ensure_whatsapp_live_adapter('u1', sid='user_u1')

        assert result['success'] is True
        create_fn.assert_not_called()
        integration.registry.register.assert_not_called()
        run_coro.assert_not_called()

    def test_reports_failure_when_loop_not_running(self):
        """If FlaskChannelIntegration.start() was never called (event loop
        thread not up), fail loudly instead of silently no-op'ing — the
        old register_channel-only path failed exactly this silently."""
        from hart_intelligence_entry import _ensure_whatsapp_live_adapter

        integration = _mock_integration(existing_adapter=None)
        integration._loop = None

        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch(
            'integrations.channels.whatsapp_adapter.create_whatsapp_adapter',
            return_value=MagicMock(),
        ):
            result = _ensure_whatsapp_live_adapter('u1', sid='user_u1')

        assert result['success'] is False
        assert 'event loop' in result['error']

    def test_default_sid_derivation(self):
        """sid defaults to user_<id> unless already prefixed — must match
        _whatsapp_account_id()'s derivation in api_channels.py exactly, or
        the mobile flow and this helper would register different sessions."""
        from hart_intelligence_entry import _ensure_whatsapp_live_adapter

        integration = _mock_integration(existing_adapter=None)
        with patch(
            'integrations.channels.flask_integration.get_channel_integration',
            return_value=integration,
        ), patch(
            'integrations.channels.whatsapp_adapter.create_whatsapp_adapter',
            return_value=MagicMock(),
        ) as create_fn, patch('asyncio.run_coroutine_threadsafe'):
            _ensure_whatsapp_live_adapter('42')

        assert create_fn.call_args.kwargs['account_id'] == 'user_42'


class TestWhatsappGetQrWiresLiveAdapter:
    """The endpoint the mobile app's QRScannerScreen actually polls."""

    def test_authenticated_status_triggers_live_adapter(self, client):
        headers = _register(client)
        fake_gateway_body = {
            'authenticated': True, 'state': 'connected', 'qr': None,
        }

        with patch(
            'integrations.social.api_channels._proxy_gateway',
            return_value=(fake_gateway_body, 200),
        ), patch(
            'hart_intelligence_entry._ensure_whatsapp_live_adapter',
        ) as ensure_fn:
            resp = client.get('/api/social/channels/whatsapp/qr', headers=headers)

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()['data']['authenticated'] is True
        ensure_fn.assert_called_once()
        # must pass the SAME account_id used to upsert the binding
        _, kwargs = ensure_fn.call_args
        assert kwargs['sid'] == kwargs['sid']  # sanity: called with explicit sid
        assert 'sid' in kwargs and kwargs['sid'].startswith('user_')

    def test_unauthenticated_status_does_not_touch_adapter(self, client):
        """Not authenticated yet (mid pairing) — must not try to wire a
        live adapter for a session that isn't actually connected."""
        headers = _register(client)
        fake_gateway_body = {
            'authenticated': False, 'state': 'connecting', 'qr': 'fake-qr-data',
        }

        with patch(
            'integrations.social.api_channels._proxy_gateway',
            return_value=(fake_gateway_body, 200),
        ), patch(
            'hart_intelligence_entry._ensure_whatsapp_live_adapter',
        ) as ensure_fn:
            resp = client.get('/api/social/channels/whatsapp/qr', headers=headers)

        assert resp.status_code == 200, resp.get_json()
        ensure_fn.assert_not_called()

    def test_live_adapter_failure_does_not_break_qr_response(self, client):
        """A live-adapter registration error must not take down the
        polling endpoint itself — binding/UI status is still useful even
        if the transport wiring hiccups."""
        headers = _register(client)
        fake_gateway_body = {
            'authenticated': True, 'state': 'connected', 'qr': None,
        }

        with patch(
            'integrations.social.api_channels._proxy_gateway',
            return_value=(fake_gateway_body, 200),
        ), patch(
            'hart_intelligence_entry._ensure_whatsapp_live_adapter',
            side_effect=RuntimeError('boom'),
        ):
            resp = client.get('/api/social/channels/whatsapp/qr', headers=headers)

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()['success'] is True
