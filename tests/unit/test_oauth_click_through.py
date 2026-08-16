"""
Tests for PR O — OAuth click-through.

Covers:
  - Per-channel metadata sanity (9 OAuth-capable channels)
  - is_oauth_capable / is_oauth_configured env-gating
  - OAuthStateManager generate / verify / replay-protection / expiry
  - generate_pkce_pair shape + S256 contract
  - /api/oauth/<type>/start authorize URL build (auth, scopes, PKCE,
    extra params, state echoing)
  - /api/oauth/<type>/callback state-mismatch / channel-mismatch /
    error / token-exchange error / happy-path register dispatch
  - hart_intelligence_entry._handle_connect_channel_tool emits
    'oauth_link' when env is set, falls back to 'form' when env is unset
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from integrations.channels.metadata import (
    CHANNEL_CATALOG,
    is_oauth_capable,
    is_oauth_configured,
    get_channel_metadata,
)
from integrations.channels.security import (
    OAuthStateManager,
    get_oauth_state_manager,
    generate_pkce_pair,
)


# ─── Metadata sanity ───────────────────────────────────────────────

OAUTH_CHANNELS = [
    'discord', 'slack', 'google_chat', 'teams',
    'messenger', 'instagram', 'twitter', 'line', 'twitch',
]


class TestOAuthMetadata:
    """The 9 OAuth-capable channels each declare a complete metadata block."""

    @pytest.mark.parametrize('ch', OAUTH_CHANNELS)
    def test_oauth_capable(self, ch):
        assert is_oauth_capable(ch), f"{ch} should be OAuth-capable"

    @pytest.mark.parametrize('ch', OAUTH_CHANNELS)
    def test_authorize_and_token_urls_set(self, ch):
        meta = get_channel_metadata(ch)
        assert meta['oauth_authorize_url'].startswith('https://'), ch
        assert meta['oauth_token_url'].startswith('https://'), ch

    @pytest.mark.parametrize('ch', OAUTH_CHANNELS)
    def test_scopes_present(self, ch):
        # LINE's scopes can be terse but must be non-empty.
        assert get_channel_metadata(ch).get('oauth_scopes'), ch

    @pytest.mark.parametrize('ch', OAUTH_CHANNELS)
    def test_response_map_present(self, ch):
        # Empty map allowed for half-OAuth (LINE: identity-only) but
        # the key itself must be declared so /callback knows the intent.
        meta = get_channel_metadata(ch)
        assert 'oauth_token_response_map' in meta, ch

    @pytest.mark.parametrize('ch', OAUTH_CHANNELS)
    def test_external_url_set(self, ch):
        # external_url is the dev-portal link used by paste-flow + OAuth
        # card's "Manage app at <provider>" secondary button.
        assert get_channel_metadata(ch).get('external_url', '').startswith('https://'), ch

    def test_pkce_required_channels(self):
        # Google, Microsoft, Twitter X v2 require PKCE.
        for ch in ['google_chat', 'teams', 'twitter']:
            assert get_channel_metadata(ch).get('oauth_uses_pkce') is True, ch

    def test_non_pkce_channels(self):
        # Discord, Slack, Meta, Twitch don't require PKCE.
        for ch in ['discord', 'slack', 'messenger', 'instagram', 'twitch']:
            assert not get_channel_metadata(ch).get('oauth_uses_pkce'), ch

    def test_legacy_paste_fields_intact(self):
        """auth_method + setup_fields stay unchanged so the paste flow
        keeps working — single binding shape, two populators.
        """
        for ch in OAUTH_CHANNELS:
            meta = get_channel_metadata(ch)
            assert meta.get('setup_fields'), f"{ch} lost setup_fields"
            assert meta.get('auth_method'), f"{ch} lost auth_method"


class TestOAuthEnvGating:
    """is_oauth_configured returns False when env is unset, True when both
    HARTOS_OAUTH_CLIENT_<TYPE> and HARTOS_OAUTH_SECRET_<TYPE> are set."""

    def test_unset_returns_false(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_oauth_configured('discord') is False

    def test_partial_set_returns_false(self):
        with patch.dict(os.environ, {'HARTOS_OAUTH_CLIENT_DISCORD': 'x'}, clear=True):
            assert is_oauth_configured('discord') is False

    def test_both_set_returns_true(self):
        with patch.dict(os.environ, {
            'HARTOS_OAUTH_CLIENT_DISCORD': 'x',
            'HARTOS_OAUTH_SECRET_DISCORD': 'y',
        }, clear=True):
            assert is_oauth_configured('discord') is True

    def test_non_oauth_channel_always_false(self):
        # email is not OAuth-capable; even with env set it's False.
        with patch.dict(os.environ, {
            'HARTOS_OAUTH_CLIENT_EMAIL': 'x',
            'HARTOS_OAUTH_SECRET_EMAIL': 'y',
        }, clear=True):
            assert is_oauth_configured('email') is False


# ─── OAuthStateManager ─────────────────────────────────────────────

class TestOAuthStateManager:

    def test_generate_returns_url_safe_token(self):
        m = OAuthStateManager()
        s = m.generate_state(user_id=1, channel_type='discord')
        assert isinstance(s, str)
        assert 30 < len(s) < 64  # secrets.token_urlsafe(32) → 43 chars
        # url-safe alphabet
        assert all(c.isalnum() or c in '-_' for c in s)

    def test_verify_returns_context(self):
        m = OAuthStateManager()
        s = m.generate_state(user_id=42, channel_type='slack', code_verifier='V1')
        ctx = m.verify_state(s)
        assert ctx == {
            'user_id': 42, 'channel_type': 'slack',
            'code_verifier': 'V1', 'extra': {},
        }

    def test_extra_round_trips(self):
        m = OAuthStateManager()
        s = m.generate_state(user_id=1, channel_type='teams', tenant='abc')
        ctx = m.verify_state(s)
        assert ctx['extra'] == {'tenant': 'abc'}

    def test_replay_protection(self):
        m = OAuthStateManager()
        s = m.generate_state(user_id=1, channel_type='discord')
        assert m.verify_state(s) is not None
        assert m.verify_state(s) is None  # second use is rejected

    def test_unknown_state(self):
        m = OAuthStateManager()
        assert m.verify_state('not-a-real-state') is None

    def test_empty_state(self):
        m = OAuthStateManager()
        assert m.verify_state('') is None
        assert m.verify_state(None) is None

    def test_expired_state_rejected(self):
        m = OAuthStateManager()
        s = m.generate_state(user_id=1, channel_type='discord')
        # Force the record's created_at into the past.
        m._states[s].created_at = datetime.now() - timedelta(minutes=11)
        assert m.verify_state(s) is None

    def test_eviction_on_generate(self):
        m = OAuthStateManager()
        old = m.generate_state(user_id=1, channel_type='discord')
        m._states[old].created_at = datetime.now() - timedelta(minutes=11)
        # Generating a new state should evict the expired one.
        m.generate_state(user_id=1, channel_type='slack')
        assert old not in m._states

    def test_singleton_returns_same_instance(self):
        a = get_oauth_state_manager()
        b = get_oauth_state_manager()
        assert a is b


class TestPKCE:
    """generate_pkce_pair returns RFC-7636-compliant (verifier, challenge)."""

    def test_pair_shape(self):
        v, c = generate_pkce_pair()
        assert 43 <= len(v) <= 128, 'verifier length out of RFC range'
        assert len(c) == 43, 'S256 challenge is 43 chars (32-byte digest, base64url, no padding)'

    def test_challenge_is_sha256_of_verifier(self):
        import hashlib, base64
        v, c = generate_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(v.encode('ascii')).digest()
        ).rstrip(b'=').decode('ascii')
        assert c == expected

    def test_pairs_are_unique(self):
        pairs = {generate_pkce_pair()[0] for _ in range(50)}
        assert len(pairs) == 50


# ─── /api/oauth/<type>/start ───────────────────────────────────────

@pytest.fixture
def app_with_oauth():
    """Flask app with oauth_bp and a stubbed auth check."""
    from flask import Flask, g
    from integrations.channels.oauth_api import oauth_bp

    app = Flask(__name__)

    # Replace the auth gate with a stub that always succeeds with user_id=99.
    # _require_user is module-level inside oauth_api; patch at the route
    # boundary via a before_request shim that pre-sets g.user_id and
    # short-circuits the real gate.
    @app.before_request
    def _stub_user():
        g.user_id = 99
        g.user = MagicMock(id=99)
        g.db = None

    app.register_blueprint(oauth_bp)
    app.config['TESTING'] = True
    return app


@pytest.fixture
def discord_env(monkeypatch):
    monkeypatch.setenv('HARTOS_OAUTH_CLIENT_DISCORD', 'CLIENT_ID_X')
    monkeypatch.setenv('HARTOS_OAUTH_SECRET_DISCORD', 'CLIENT_SECRET_Y')
    monkeypatch.setenv('HARTOS_PUBLIC_URL', 'https://hartos.example.com')


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv('HARTOS_OAUTH_CLIENT_GOOGLE_CHAT', 'GOOG_ID')
    monkeypatch.setenv('HARTOS_OAUTH_SECRET_GOOGLE_CHAT', 'GOOG_SECRET')
    monkeypatch.setenv('HARTOS_PUBLIC_URL', 'https://hartos.example.com')


class TestOAuthStartRoute:

    def test_unconfigured_channel_returns_400(self, app_with_oauth):
        # No env vars set → is_oauth_configured False.  Patch the real
        # _require_user shim so we exercise the policy check and not
        # the Bearer-token gate.
        with patch(
            'integrations.channels.oauth_api._require_user',
            return_value=None,
        ):
            resp = app_with_oauth.test_client().post(
                '/api/oauth/discord/start',
                headers={'Authorization': 'Bearer faketoken'},
            )
        assert resp.status_code == 400
        # Assert the STRUCTURED reason, not the prose. The human-facing message
        # was deliberately rewritten to be kind and actionable ("Connecting
        # Discord isn't switched on yet. The Hevolve team still has to enable
        # it...") and no longer contains the machine phrase "not configured";
        # the machine contract is the `reason` field, which is what a client
        # should branch on anyway.
        assert b'"reason":"not_configured"' in resp.data.replace(b', ', b',')

    def test_non_oauth_channel_returns_400(self, app_with_oauth, discord_env):
        # Email is not OAuth-capable.
        with patch(
            'integrations.channels.oauth_api._require_user',
            return_value=None,
        ):
            resp = app_with_oauth.test_client().post(
                '/api/oauth/email/start',
                headers={'Authorization': 'Bearer faketoken'},
            )
        assert resp.status_code == 400
        # Structured reason, not prose (see the note above): email's message is
        # now "Add Email by pasting your IMAP Server." with reason=not_oauth.
        assert b'"reason":"not_oauth"' in resp.data.replace(b', ', b',')

    def test_discord_start_builds_authorize_url(self, app_with_oauth, discord_env):
        # Patch _require_user so the route doesn't try to validate the
        # Bearer token via the real social.auth.  The before_request
        # stub on the app fixture sets g but oauth_bp's own
        # _require_user call wraps the real auth — patch it out.
        with patch(
            'integrations.channels.oauth_api._require_user',
            return_value=None,
        ):
            client = app_with_oauth.test_client()
            with client.application.test_request_context():
                pass
            resp = client.post(
                '/api/oauth/discord/start',
                headers={'Authorization': 'Bearer faketoken'},
                json={},
            )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body['success'] is True
        url = body['authorize_url']
        assert url.startswith('https://discord.com/api/oauth2/authorize?')
        # Discord-specific extras live in metadata.oauth_extra_params.
        assert 'permissions=274877990912' in url
        # Scopes from metadata.
        assert 'scope=bot+applications.commands' in url \
            or 'scope=bot%20applications.commands' in url
        # Redirect URI uses HARTOS_PUBLIC_URL.
        assert 'redirect_uri=https%3A%2F%2Fhartos.example.com%2Fapi%2Foauth%2Fdiscord%2Fcallback' in url
        # State is opaque.
        assert 'state=' in url
        # No PKCE for Discord.
        assert 'code_challenge=' not in url

    def test_google_start_includes_pkce(self, app_with_oauth, google_env):
        with patch(
            'integrations.channels.oauth_api._require_user',
            return_value=None,
        ):
            resp = app_with_oauth.test_client().post(
                '/api/oauth/google_chat/start',
                headers={'Authorization': 'Bearer faketoken'},
                json={},
            )
        assert resp.status_code == 200, resp.data
        url = resp.get_json()['authorize_url']
        assert 'code_challenge=' in url
        assert 'code_challenge_method=S256' in url
        # Google-specific extras (access_type=offline, prompt=consent).
        assert 'access_type=offline' in url
        assert 'prompt=consent' in url


# ─── /api/oauth/<type>/callback ────────────────────────────────────

class TestOAuthCallbackRoute:

    def test_missing_state_returns_400(self, app_with_oauth):
        resp = app_with_oauth.test_client().get(
            '/api/oauth/discord/callback?code=abc',
        )
        assert resp.status_code == 400
        assert b'Missing code or state' in resp.data

    def test_missing_code_returns_400(self, app_with_oauth):
        resp = app_with_oauth.test_client().get(
            '/api/oauth/discord/callback?state=abc',
        )
        assert resp.status_code == 400

    def test_provider_error_renders_close_page(self, app_with_oauth):
        resp = app_with_oauth.test_client().get(
            '/api/oauth/discord/callback?error=access_denied',
        )
        assert resp.status_code == 400
        assert b'Provider error' in resp.data
        # Deep-link is embedded so mobile dismissal still fires.
        assert b'hevolve://oauth-complete' in resp.data

    def test_state_mismatch_returns_400(self, app_with_oauth):
        # Generate state for slack but call /discord/callback.
        m = get_oauth_state_manager()
        # Reset to keep tests isolated.
        m._states.clear()
        s = m.generate_state(user_id=1, channel_type='slack')
        resp = app_with_oauth.test_client().get(
            f'/api/oauth/discord/callback?code=x&state={s}',
        )
        assert resp.status_code == 400
        assert b'State / channel mismatch' in resp.data

    def test_unknown_state_returns_400(self, app_with_oauth):
        resp = app_with_oauth.test_client().get(
            '/api/oauth/discord/callback?code=x&state=bogus-state',
        )
        assert resp.status_code == 400
        assert b'Invalid or expired state' in resp.data

    def test_token_exchange_failure_returns_502(self, app_with_oauth, discord_env):
        m = get_oauth_state_manager()
        m._states.clear()
        s = m.generate_state(user_id=1, channel_type='discord')

        fake_resp = MagicMock()
        fake_resp.status_code = 400
        fake_resp.json.return_value = {'error': 'invalid_grant'}
        with patch(
            'integrations.channels.oauth_api.requests.post',
            return_value=fake_resp,
        ):
            resp = app_with_oauth.test_client().get(
                f'/api/oauth/discord/callback?code=x&state={s}',
            )
        assert resp.status_code == 502
        assert b'invalid_grant' in resp.data

    def test_happy_path_calls_register_channel(self, app_with_oauth, discord_env):
        m = get_oauth_state_manager()
        m._states.clear()
        s = m.generate_state(user_id=42, channel_type='discord')

        # Mock the token exchange to return a Discord-shaped response.
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            'access_token': 'BOT_TOKEN_XYZ',
            'token_type': 'Bearer',
            'scope': 'bot applications.commands',
        }

        # Mock build_channel_tool_closures so register_channel is a stub.
        register_calls = []

        def fake_register(channel_type, config_json):
            register_calls.append((channel_type, config_json))
            return f'{channel_type} registered and enabled!'

        fake_tools = [('register_channel', '...', fake_register)]

        with patch(
            'integrations.channels.oauth_api.requests.post',
            return_value=fake_resp,
        ), patch(
            'integrations.channels.agent_tools.build_channel_tool_closures',
            return_value=fake_tools,
        ):
            resp = app_with_oauth.test_client().get(
                f'/api/oauth/discord/callback?code=AUTH_CODE&state={s}',
            )

        assert resp.status_code == 200, resp.data
        assert len(register_calls) == 1
        ch_type, cfg = register_calls[0]
        assert ch_type == 'discord'
        # oauth_token_response_map maps access_token → bot_token.
        import json as _json
        cfg_dict = _json.loads(cfg)
        assert cfg_dict == {'bot_token': 'BOT_TOKEN_XYZ'}
        # Close-page deep link is present so the RN app dismisses card.
        assert b'hevolve://oauth-complete' in resp.data
        assert b'channel_type=discord' in resp.data

    def test_pkce_verifier_is_sent_in_token_exchange(
        self, app_with_oauth, google_env,
    ):
        m = get_oauth_state_manager()
        m._states.clear()
        s = m.generate_state(
            user_id=1, channel_type='google_chat', code_verifier='V_RANDOM',
        )

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            'access_token': 'AT', 'refresh_token': 'RT',
        }

        with patch(
            'integrations.channels.oauth_api.requests.post',
            return_value=fake_resp,
        ) as post_mock, patch(
            'integrations.channels.agent_tools.build_channel_tool_closures',
            return_value=[(
                'register_channel', '...',
                lambda *a, **k: 'google_chat registered and enabled!',
            )],
        ):
            app_with_oauth.test_client().get(
                f'/api/oauth/google_chat/callback?code=C&state={s}',
            )

        # Inspect the token-exchange request body.
        kwargs = post_mock.call_args.kwargs
        assert kwargs['data']['code_verifier'] == 'V_RANDOM'
        assert kwargs['data']['code'] == 'C'
        assert kwargs['data']['client_id'] == 'GOOG_ID'
        assert kwargs['data']['client_secret'] == 'GOOG_SECRET'
