"""Env-var channels are registered from ONE list: FlaskChannelIntegration
._ENV_FALLBACKS, by start().

hartos_bootstrap._init_channel_adapters used to keep its own five-entry
env dict that disagreed with it: SIGNAL_SERVICE_URL (a URL) was registered as
Signal's phone number, and WHATSAPP_ACCESS_TOKEN built the identity-less
WhatsApp adapter that _RESTORE_EXCLUDED rules out.  Bootstrap now registers
only the admin-config channels and `web`, then calls start().
"""
from unittest.mock import MagicMock, patch


def test_bootstrap_registers_no_env_channel_itself(monkeypatch):
    from hartos import hartos_bootstrap

    for name, value in {'TELEGRAM_BOT_TOKEN': 't', 'DISCORD_BOT_TOKEN': 'd',
                        'SLACK_BOT_TOKEN': 's',
                        'WHATSAPP_ACCESS_TOKEN': 'w',
                        'SIGNAL_SERVICE_URL': 'http://signal:8080'}.items():
        monkeypatch.setenv(name, value)

    channels = MagicMock()
    channels.registry._adapters = {}
    channels.register_channel.return_value = True
    admin_api = MagicMock()
    admin_api._channels = {}

    with patch('integrations.channels.flask_integration.init_channels',
               return_value=channels), \
         patch('integrations.channels.admin.api.get_api', return_value=admin_api):
        hartos_bootstrap._init_channel_adapters(MagicMock(), {})

    registered = [c.args[0] for c in channels.register_channel.call_args_list]
    assert registered == ['web'], registered
    channels.start.assert_called_once_with()


def test_start_registers_the_env_channels_bootstrap_used_to(monkeypatch):
    """The same three working env names, now via start() and _ENV_FALLBACKS."""
    from integrations.channels import flask_integration as fi_mod
    from integrations.channels.registry import ChannelRegistry

    fi = fi_mod.FlaskChannelIntegration.__new__(fi_mod.FlaskChannelIntegration)
    fi.registry = ChannelRegistry()
    fi._thread = None
    for name in ('TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN', 'SLACK_BOT_TOKEN'):
        monkeypatch.setenv(name, 'x')
    asked = []
    monkeypatch.setattr(fi, 'register_channel',
                        lambda ct, **kw: asked.append(ct) or True)
    monkeypatch.setattr(fi, 'restore_persisted_channels', lambda: {})
    monkeypatch.setattr(fi_mod.threading, 'Thread',
                        lambda *a, **k: type('T', (), {'start': lambda self: None})())
    fi.start()

    assert {'telegram', 'discord', 'slack'} <= set(asked)
