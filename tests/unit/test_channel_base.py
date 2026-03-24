"""
test_channel_base.py - Tests for integrations/channels/base.py

Tests the base channel adapter — the interface all 34 channel integrations implement.
Each test verifies a specific contract or data structure guarantee:

FT: Message dataclass (content extraction, media detection), MediaAttachment,
    ChannelStatus enum, ChannelConfig, ChannelAdapter ABC contract.
NFT: Enum completeness, dataclass defaults, message serialization safety.
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.channels.base import (
    MessageType, ChannelStatus, MediaAttachment, Message,
    SendResult, ChannelConfig, ChannelAdapter,
    ChannelError, ChannelConnectionError, ChannelSendError,
)


# ============================================================
# MessageType enum — drives media handling across all channels
# ============================================================

class TestMessageType:
    """MessageType determines how the channel adapter processes attachments."""

    def test_has_text(self):
        assert MessageType.TEXT.value == 'text'

    def test_has_image(self):
        assert MessageType.IMAGE.value == 'image'

    def test_has_all_common_types(self):
        """All common messaging types must be covered — missing = unsupported media."""
        names = {m.name for m in MessageType}
        required = {'TEXT', 'IMAGE', 'VIDEO', 'AUDIO', 'DOCUMENT', 'VOICE'}
        missing = required - names
        assert not missing, f"Missing message types: {missing}"


class TestChannelStatus:
    """ChannelStatus drives the connection indicator in the admin panel."""

    def test_has_connected_and_disconnected(self):
        assert ChannelStatus.CONNECTED.value == 'connected'
        assert ChannelStatus.DISCONNECTED.value == 'disconnected'

    def test_has_error_state(self):
        assert ChannelStatus.ERROR.value == 'error'

    def test_has_rate_limited(self):
        """Rate limiting is common across platforms — needs its own state."""
        assert ChannelStatus.RATE_LIMITED.value == 'rate_limited'


# ============================================================
# Message dataclass — unified format across all 34 channels
# ============================================================

class TestMessage:
    """Message is the universal format — every channel adapter converts to/from this."""

    def test_has_media_false_when_empty(self):
        msg = Message(id='1', channel='test', sender_id='u1')
        assert msg.has_media is False

    def test_has_media_true_with_attachment(self):
        att = MediaAttachment(type=MessageType.IMAGE, url='http://example.com/img.png')
        msg = Message(id='1', channel='test', sender_id='u1', media=[att])
        assert msg.has_media is True

    def test_content_returns_text(self):
        msg = Message(id='1', channel='test', sender_id='u1', text='Hello')
        assert msg.content == 'Hello'

    def test_content_returns_caption_when_no_text(self):
        att = MediaAttachment(type=MessageType.IMAGE, caption='A photo')
        msg = Message(id='1', channel='test', sender_id='u1', media=[att])
        assert msg.content == 'A photo'

    def test_content_empty_when_no_text_no_caption(self):
        msg = Message(id='1', channel='test', sender_id='u1')
        assert msg.content == ''

    def test_defaults(self):
        msg = Message(id='1', channel='test', sender_id='u1')
        assert msg.chat_id == ''
        assert msg.is_group is False
        assert msg.is_bot_mentioned is False
        assert msg.reply_to_id is None
        assert msg.raw is None

    def test_timestamp_auto_set(self):
        before = datetime.now()
        msg = Message(id='1', channel='test', sender_id='u1')
        assert msg.timestamp >= before


# ============================================================
# MediaAttachment — file metadata for cross-platform media
# ============================================================

class TestMediaAttachment:
    """MediaAttachment carries file info — wrong metadata = broken preview."""

    def test_minimal_creation(self):
        att = MediaAttachment(type=MessageType.IMAGE)
        assert att.type == MessageType.IMAGE
        assert att.url is None
        assert att.file_size is None

    def test_full_creation(self):
        att = MediaAttachment(
            type=MessageType.DOCUMENT,
            url='https://example.com/doc.pdf',
            file_name='report.pdf',
            mime_type='application/pdf',
            file_size=1024000,
        )
        assert att.file_name == 'report.pdf'
        assert att.file_size == 1024000


# ============================================================
# ChannelConfig — adapter configuration
# ============================================================

class TestChannelConfig:
    """ChannelConfig stores credentials and settings per adapter."""

    def test_creation_with_token(self):
        config = ChannelConfig(token='test-token')
        assert config.token == 'test-token'

    def test_enabled_default_true(self):
        config = ChannelConfig()
        assert config.enabled is True

    def test_dm_policy_default(self):
        config = ChannelConfig()
        assert config.dm_policy == 'pairing'


# ============================================================
# ChannelAdapter ABC — interface contract
# ============================================================

class TestChannelAdapterContract:
    """ChannelAdapter is abstract — concrete adapters must implement all methods."""

    def test_cannot_instantiate_directly(self):
        """ABC must not be instantiated — forces subclasses to implement contract."""
        with pytest.raises(TypeError):
            ChannelAdapter(ChannelConfig(channel_name='test'))

    def test_has_required_abstract_methods(self):
        """ChannelAdapter must define the interface all 34 adapters implement."""
        import inspect
        abstract_methods = {name for name, _ in inspect.getmembers(ChannelAdapter)
                           if getattr(getattr(ChannelAdapter, name, None), '__isabstractmethod__', False)}
        required = {'connect', 'disconnect', 'send_message', 'name'}
        missing = required - abstract_methods
        assert not missing, f"Missing abstract methods: {missing}"


# ============================================================
# Error hierarchy — channel adapters raise specific exceptions
# ============================================================

class TestChannelErrors:
    """Error classes used by all channel adapters for consistent handling."""

    def test_channel_error_is_exception(self):
        assert issubclass(ChannelError, Exception)

    def test_connection_error_is_channel_error(self):
        assert issubclass(ChannelConnectionError, ChannelError)

    def test_send_error_is_channel_error(self):
        assert issubclass(ChannelSendError, ChannelError)

    def test_send_result_success(self):
        result = SendResult(success=True, message_id='123')
        assert result.success is True
        assert result.message_id == '123'

    def test_send_result_failure(self):
        result = SendResult(success=False, error='Rate limited')
        assert result.success is False
        assert result.error == 'Rate limited'
