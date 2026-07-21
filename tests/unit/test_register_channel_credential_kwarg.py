"""Regression test for FlaskChannelIntegration.register_channel()'s
credential-parameter mismatch bug (found 2026-07-21 while investigating
why Signal, like WhatsApp before it, never actually connected).

register_channel() always called the adapter factory with token=<cred>.
Most factories accept that (create_telegram_adapter(token=...),
create_discord_adapter(token=...)), but not all do:

  - create_whatsapp_adapter(api_url=..., phone_number=..., account_id=...)
  - create_signal_adapter(phone_number=..., api_url=...)

Passing token= to either either raised "unexpected keyword argument"
(if the factory has no **kwargs catch-all) or, worse, silently absorbed
it into **kwargs and left the real required parameter as None — so the
factory raised its OWN "required" ValueError instead, with the original
mismatch nowhere in the error message. Either way, register_channel()
returned False for every credential-based channel whose factory doesn't
happen to name its parameter "token", and no live adapter was ever
constructed.

Fixed via _credential_kwarg(): inspect the factory's actual signature
and pass the credential under whatever its first real parameter is
named, defaulting to "token" only when nothing better applies.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.channels.flask_integration import FlaskChannelIntegration


def _sig_token(token: str = None, other: str = None, **kwargs):
    pass


def _sig_api_url(api_url: str = None, phone_number: str = None, **kwargs):
    pass


def _sig_phone_first(phone_number: str = None, api_url: str = None, **kwargs):
    pass


def _sig_no_params(**kwargs):
    pass


class TestCredentialKwarg:
    def test_uses_token_when_factory_accepts_it(self):
        result = FlaskChannelIntegration._credential_kwarg(_sig_token, 'abc123')
        assert result == {'token': 'abc123'}

    def test_falls_back_to_first_param_for_whatsapp_style_factory(self):
        """create_whatsapp_adapter(api_url=..., phone_number=..., ...) —
        no 'token' param at all; api_url is first."""
        result = FlaskChannelIntegration._credential_kwarg(_sig_api_url, 'http://localhost:3000')
        assert result == {'api_url': 'http://localhost:3000'}

    def test_falls_back_to_first_param_for_signal_style_factory(self):
        """create_signal_adapter(phone_number=..., api_url=...) —
        phone_number is first."""
        result = FlaskChannelIntegration._credential_kwarg(_sig_phone_first, '+15551234567')
        assert result == {'phone_number': '+15551234567'}

    def test_defaults_to_token_when_factory_has_no_named_params(self):
        result = FlaskChannelIntegration._credential_kwarg(_sig_no_params, 'x')
        assert result == {'token': 'x'}

    def test_real_whatsapp_factory_signature(self):
        from integrations.channels.whatsapp_adapter import create_whatsapp_adapter
        result = FlaskChannelIntegration._credential_kwarg(
            create_whatsapp_adapter, 'http://localhost:3000',
        )
        assert result == {'api_url': 'http://localhost:3000'}
        # Must actually be constructible with this kwarg — the real bug
        # symptom was a downstream crash even when this looked plausible.
        adapter = create_whatsapp_adapter(**result)
        assert adapter is not None

    def test_real_signal_factory_signature(self):
        from integrations.channels.signal_adapter import create_signal_adapter
        result = FlaskChannelIntegration._credential_kwarg(
            create_signal_adapter, '+15551234567',
        )
        assert result == {'phone_number': '+15551234567'}
        adapter = create_signal_adapter(**result)
        assert adapter is not None

    def test_real_telegram_factory_signature(self):
        """Telegram's factory names its param 'token' — must be unaffected
        by this change (still the common, unremarkable case)."""
        from integrations.channels.telegram_adapter import create_telegram_adapter
        result = FlaskChannelIntegration._credential_kwarg(
            create_telegram_adapter, '123:ABC',
        )
        assert result == {'token': '123:ABC'}
