import pytest
from integrations.channels.metadata import (
    CHANNEL_CATALOG, get_channel_metadata, list_all_channels,
    get_channels_by_category, get_channels_by_auth_method
)


class TestChannelMetadata:
    def test_catalog_has_31_channels(self):
        assert len(CHANNEL_CATALOG) == 31

    def test_all_channels_have_required_fields(self):
        required = ['display_name', 'icon', 'color', 'category', 'auth_method', 'setup_fields', 'capabilities']
        for name, meta in CHANNEL_CATALOG.items():
            for field in required:
                assert field in meta, f'{name} missing {field}'

    def test_all_channels_have_capabilities(self):
        cap_fields = ['text', 'image', 'groups', 'typing', 'max_message_length']
        for name, meta in CHANNEL_CATALOG.items():
            for cap in cap_fields:
                assert cap in meta['capabilities'], f'{name} missing capability {cap}'

    def test_get_channel_metadata_known(self):
        meta = get_channel_metadata('telegram')
        assert meta is not None
        assert meta['display_name'] == 'Telegram'

    def test_get_channel_metadata_unknown(self):
        assert get_channel_metadata('nonexistent') is None

    def test_list_all_returns_dict(self):
        result = list_all_channels()
        assert isinstance(result, dict)
        assert len(result) == 31

    def test_categories_cover_all(self):
        categories = set(m['category'] for m in CHANNEL_CATALOG.values())
        assert categories == {'core', 'enterprise', 'social', 'decentralized', 'bridge', 'utility'}

    def test_auth_methods_valid(self):
        valid = {'api_key', 'oauth2', 'websocket_token', 'qr_session', 'phone_2fa', 'credentials'}
        for name, meta in CHANNEL_CATALOG.items():
            assert meta['auth_method'] in valid, f'{name} has invalid auth_method: {meta["auth_method"]}'

    def test_get_channels_by_category(self):
        core = get_channels_by_category('core')
        assert 'telegram' in core
        assert 'teams' not in core

    def test_get_channels_by_auth_method(self):
        api_key_channels = get_channels_by_auth_method('api_key')
        assert 'telegram' in api_key_channels
        assert 'matrix' not in api_key_channels
