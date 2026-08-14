"""Boot-time rehydration of channel adapters from persisted bindings.

UserChannelBinding exists to "persist user-to-channel links across restarts",
but nothing read those rows back at boot: adapters were only ever constructed
by a live registration call.  So every HARTOS restart left Discord / Telegram /
Slack / Signal disconnected until the binding was re-POSTed by hand, while
WhatsApp survived because it alone had a dedicated rehydration path
(_ensure_whatsapp_live_adapter).

FlaskChannelIntegration.restore_persisted_channels() closes that gap for the
rest, and start() calls it before the loop thread spins up so that
registry.start_all() connects the restored adapters with no extra lifecycle
machinery.

The load-bearing subtlety pinned here: a binding stores its credential under
the BINDING API's naming ('bot_token'), which is not the factory's parameter
name (create_discord_adapter takes 'token').  Passing metadata straight through
as **kwargs would land bot_token in the factory's catch-all, leave token None,
fall back to the DISCORD_BOT_TOKEN env var and register nothing — a silent
no-op that still logs success.  The credential must go through
register_channel's generic `token`, which maps it via _CHANNEL_SPECS /
_credential_kwarg.
"""

import sys
import types
from datetime import datetime
from unittest.mock import Mock, patch

import pytest


def _integration():
    """Build a FlaskChannelIntegration without touching Flask or the network."""
    try:
        from integrations.channels.flask_integration import (
            FlaskChannelIntegration,
        )
    except Exception as e:  # pragma: no cover - import-environment guard
        pytest.skip(f"flask_integration unavailable: {e}")
    fi = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    fi.registry = Mock()
    fi.registry.get.return_value = None
    return fi


def _binding(channel_type, meta=None, bid=1, updated=None, active=True):
    row = types.SimpleNamespace()
    row.channel_type = channel_type
    row.metadata_json = meta
    row.id = bid
    row.updated_at = updated
    row.is_active = active
    return row


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **kwargs):
        return self

    def all(self):
        return list(self._rows)


def _patch_db(rows):
    """Patch the social models import inside restore_persisted_channels."""
    db = Mock()
    db.query.return_value = _FakeQuery(rows)
    fake_models = types.ModuleType('integrations.social.models')
    fake_models.get_db = lambda: db
    fake_models.UserChannelBinding = object
    return patch.dict(sys.modules, {'integrations.social.models': fake_models})


class TestCredentialResolution:
    """_binding_credentials maps stored metadata onto register_channel's
    generic `token` + declared extras."""

    def test_discord_bot_token_becomes_the_generic_token(self):
        fi = _integration()
        token, extras = fi._binding_credentials('discord', {'bot_token': 'abc'})
        assert token == 'abc', (
            "a discord binding stores 'bot_token'; it must be surfaced as the "
            "generic credential, not left for the env fallback"
        )
        assert extras == {}

    def test_signal_prefers_its_declared_token_param(self):
        fi = _integration()
        # phone_number is signal's token_param AND api_url is a generic
        # credential key — the spec's token_param must win.
        token, extras = fi._binding_credentials(
            'signal', {'api_url': 'http://x', 'phone_number': '+15551234'},
        )
        assert token == '+15551234'
        assert extras == {'api_url': 'http://x'}, (
            "a declared `extra` carried by the binding must be passed through "
            "so the stored value beats the env fallback"
        )

    def test_slack_app_token_passed_as_extra(self):
        fi = _integration()
        token, extras = fi._binding_credentials(
            'slack', {'bot_token': 'xoxb-1', 'app_token': 'xapp-1'},
        )
        assert token == 'xoxb-1'
        assert extras == {'app_token': 'xapp-1'}

    def test_missing_and_blank_credentials_yield_none(self):
        fi = _integration()
        assert fi._binding_credentials('discord', {})[0] is None
        assert fi._binding_credentials('discord', {'bot_token': '   '})[0] is None
        assert fi._binding_credentials('discord', {'bot_token': None})[0] is None

    def test_non_string_credential_is_ignored(self):
        fi = _integration()
        # A malformed row must not hand an int to a factory expecting a token.
        assert fi._binding_credentials('discord', {'bot_token': 12345})[0] is None


class TestRestorePersistedChannels:

    def test_restores_discord_through_the_token_mapping_layer(self):
        fi = _integration()
        rows = [_binding('discord', {'bot_token': 'secret-1'})]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        assert out['restored'] == ['discord']
        reg.assert_called_once_with('discord', token='secret-1')
        # Guard the exact defect: never pass the stored key through verbatim.
        assert 'bot_token' not in reg.call_args.kwargs, (
            "bot_token= would be swallowed by create_discord_adapter's "
            "**kwargs, leaving token=None and silently registering nothing"
        )

    def test_newest_binding_wins_when_several_share_a_channel(self):
        """registry.register keys on adapter.name, so only one adapter per
        channel_type can exist — restoring all five discord rows would open
        five gateway connections that overwrite each other."""
        fi = _integration()
        rows = [
            _binding('discord', {'bot_token': 'old'}, bid=8,
                     updated=datetime(2026, 1, 1)),
            _binding('discord', {'bot_token': 'newest'}, bid=11,
                     updated=datetime(2026, 8, 1)),
            _binding('discord', {'bot_token': 'middle'}, bid=9,
                     updated=datetime(2026, 5, 1)),
        ]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            fi.restore_persisted_channels()

        assert reg.call_count == 1
        assert reg.call_args.kwargs['token'] == 'newest'

    def test_null_updated_at_falls_back_to_id(self):
        """Legacy rows have updated_at NULL; sorting must not raise and the
        higher id must win."""
        fi = _integration()
        rows = [
            _binding('telegram', {'bot_token': 'lower'}, bid=2, updated=None),
            _binding('telegram', {'bot_token': 'higher'}, bid=7, updated=None),
        ]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            fi.restore_persisted_channels()

        assert reg.call_count == 1
        assert reg.call_args.kwargs['token'] == 'higher'

    def test_newest_row_without_a_credential_falls_through_to_an_older_one(self):
        """Taken from the real binding table: the NEWEST discord row (id=12,
        an orphaned out-of-band row) carries no bot_token, while the working
        binding is older (id=11).  Stopping at the newest row skipped discord
        entirely and restored nothing — the exact failure this guards."""
        fi = _integration()
        rows = [
            _binding('discord', {}, bid=12, updated=datetime(2026, 8, 12)),
            _binding('discord', {'bot_token': 'real'}, bid=11,
                     updated=datetime(2026, 8, 6)),
        ]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        assert out['restored'] == ['discord']
        assert reg.call_args.kwargs['token'] == 'real'

    def test_failed_newer_binding_falls_back_to_an_older_one(self):
        """A stale token on a newer row must not mask a working older one."""
        fi = _integration()
        rows = [
            _binding('discord', {'bot_token': 'revoked'}, bid=14,
                     updated=datetime(2026, 8, 10)),
            _binding('discord', {'bot_token': 'good'}, bid=11,
                     updated=datetime(2026, 8, 6)),
        ]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', side_effect=[False, True],
        ) as reg:
            out = fi.restore_persisted_channels()

        assert out['restored'] == ['discord']
        assert reg.call_count == 2
        assert reg.call_args.kwargs['token'] == 'good'

    def test_channel_with_no_usable_binding_reports_no_credential(self):
        fi = _integration()
        rows = [
            _binding('discord', {}, bid=12),
            _binding('discord', None, bid=6),
        ]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        reg.assert_not_called()
        assert out['skipped']['discord'] == 'no stored credential'

    def test_whatsapp_is_left_to_its_dedicated_path(self):
        fi = _integration()
        rows = [_binding('whatsapp', {'api_url': 'http://127.0.0.1:3000'})]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        reg.assert_not_called()
        assert out['skipped']['whatsapp'] == 'dedicated restore path'

    def test_already_registered_channel_is_not_replaced(self):
        fi = _integration()
        fi.registry.get.return_value = Mock()  # env/code already registered it
        rows = [_binding('discord', {'bot_token': 'x'})]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        reg.assert_not_called()
        assert out['skipped']['discord'] == 'already registered'

    def test_binding_without_credential_is_skipped(self):
        fi = _integration()
        rows = [_binding('discord', {})]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        reg.assert_not_called()
        assert out['skipped']['discord'] == 'no stored credential'

    def test_no_token_channel_restores_without_a_credential(self):
        fi = _integration()
        rows = [_binding('imessage', None)]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        assert out['restored'] == ['imessage']
        assert reg.call_args.kwargs['token'] is None

    def test_unknown_channel_type_is_skipped(self):
        fi = _integration()
        rows = [_binding('carrier_pigeon', {'token': 'x'})]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        reg.assert_not_called()
        assert out['skipped']['carrier_pigeon'] == 'no adapter factory'

    def test_one_bad_row_does_not_stop_the_others(self):
        """A binding that fails to register must not abort the restore — the
        server has to finish booting."""
        fi = _integration()
        rows = [
            _binding('discord', {'bot_token': 'a'}, bid=2),
            _binding('telegram', {'bot_token': 'b'}, bid=1),
        ]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', side_effect=[False, True],
        ):
            out = fi.restore_persisted_channels()

        assert out['restored'] == ['telegram']
        assert out['skipped']['discord'] == 'registration failed'

    def test_db_failure_is_swallowed(self):
        fi = _integration()
        broken = types.ModuleType('integrations.social.models')

        def _boom():
            raise RuntimeError("db down")

        broken.get_db = _boom
        broken.UserChannelBinding = object
        with patch.dict(
            sys.modules, {'integrations.social.models': broken},
        ):
            out = fi.restore_persisted_channels()

        assert out == {'restored': [], 'skipped': {}}

    def test_env_flag_disables_restore(self, monkeypatch):
        fi = _integration()
        monkeypatch.setenv('HEVOLVE_CHANNEL_RESTORE', '0')
        rows = [_binding('discord', {'bot_token': 'x'})]
        with _patch_db(rows), patch.object(
            fi, 'register_channel', return_value=True,
        ) as reg:
            out = fi.restore_persisted_channels()

        reg.assert_not_called()
        assert out['restored'] == []


class TestStartWiring:
    """The restore is worthless if start() doesn't call it before the loop
    thread reaches registry.start_all()."""

    def test_start_restores_before_spawning_the_loop_thread(self):
        import inspect

        from integrations.channels.flask_integration import (
            FlaskChannelIntegration,
        )
        src = inspect.getsource(FlaskChannelIntegration.start)
        assert 'restore_persisted_channels' in src, (
            "start() must rehydrate persisted bindings, or adapters stay "
            "dead across restarts"
        )
        restore_at = src.index('restore_persisted_channels')
        thread_at = src.index('threading.Thread')
        assert restore_at < thread_at, (
            "registry.start_all() runs as the loop thread's first act, so "
            "adapters registered after the thread starts may never connect"
        )
