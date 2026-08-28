"""
Tests for Signal Channel Adapter

Tests the Signal adapter functionality including:
- Message conversion
- Send/receive operations
- Group support
- Reactions
- Error handling
- Reconnection logic
"""

import pytest
import asyncio
import aiohttp
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime
import json

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.base import (
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
)


class TestSignalAdapter:
    """Tests for SignalAdapter."""

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp module."""
        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={})
        mock_response.text = AsyncMock(return_value="")

        mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.put = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.close = AsyncMock()

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            with patch('aiohttp.ClientSession', return_value=mock_session):
                yield mock_session

    @pytest.fixture
    def signal_config(self):
        """Create Signal adapter config."""
        return ChannelConfig(
            token="+1234567890",
            extra={"api_url": "http://localhost:8080"}
        )

    def test_adapter_creation(self, mock_aiohttp, signal_config):
        """Test SignalAdapter instantiation."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        assert adapter.name == "signal"
        assert adapter.status == ChannelStatus.DISCONNECTED
        assert adapter._phone_number == "+1234567890"
        assert adapter._api_url == "http://localhost:8080"

    def test_message_handler_registration(self, mock_aiohttp, signal_config):
        """Test message handler registration."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1

    @pytest.mark.asyncio
    async def test_connect_success(self, mock_aiohttp, signal_config):
        """Test successful connection."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        # Mock API responses
        about_response = AsyncMock()
        about_response.status = 200

        accounts_response = AsyncMock()
        accounts_response.status = 200
        accounts_response.json = AsyncMock(return_value=[{"number": "+1234567890"}])

        mock_aiohttp.get = MagicMock(side_effect=[
            AsyncMock(__aenter__=AsyncMock(return_value=about_response)),
            AsyncMock(__aenter__=AsyncMock(return_value=accounts_response)),
        ])

        with patch('aiohttp.ClientSession', return_value=mock_aiohttp):
            result = await adapter.connect()

        # Connection starts polling in background, so it should return True
        # Note: We can't fully test async polling here
        assert adapter._phone_number == "+1234567890"

    @pytest.mark.asyncio
    async def test_connect_success_accounts_as_plain_strings(self, mock_aiohttp, signal_config):
        """2026-08-28: current bbernhard/signal-cli-rest-api releases return
        /v1/accounts as a plain list of phone-number strings (e.g.
        ["+1234567890"]), not the list-of-dict shape acc.get("number")
        assumed. Found live against a real linked account -- the resulting
        AttributeError propagated out of this purely-advisory check and
        made connect() report the whole connection as ERROR even though
        /v1/about already succeeded and the account was genuinely linked.
        """
        from integrations.channels.base import ChannelStatus
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        about_response = AsyncMock()
        about_response.status = 200

        accounts_response = AsyncMock()
        accounts_response.status = 200
        accounts_response.json = AsyncMock(return_value=["+1234567890"])

        mock_aiohttp.get = MagicMock(side_effect=[
            AsyncMock(__aenter__=AsyncMock(return_value=about_response)),
            AsyncMock(__aenter__=AsyncMock(return_value=accounts_response)),
        ])

        with patch('aiohttp.ClientSession', return_value=mock_aiohttp):
            result = await adapter.connect()

        assert result is True
        assert adapter.status == ChannelStatus.CONNECTED

    def test_message_conversion(self, mock_aiohttp, signal_config):
        """Test Signal message to unified Message conversion."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        # Mock Signal message data
        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Hello from Signal!",
                    "groupInfo": None,
                    "attachments": [],
                    "mentions": [],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.channel == "signal"
        assert message.sender_id == "+1987654321"
        assert message.sender_name == "John Doe"
        assert message.text == "Hello from Signal!"
        assert message.is_group is False

    def test_group_message_conversion(self, mock_aiohttp, signal_config):
        """Test group message conversion."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Hello group!",
                    "groupInfo": {
                        "groupId": "abc123groupid",
                    },
                    "attachments": [],
                    "mentions": [],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.is_group is True
        assert message.chat_id == "abc123groupid"

    def test_attachment_handling(self, mock_aiohttp, signal_config):
        """Test attachment in message."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Check this out",
                    "groupInfo": None,
                    "attachments": [
                        {
                            "id": "att123",
                            "filename": "photo.jpg",
                            "contentType": "image/jpeg",
                            "size": 12345,
                        }
                    ],
                    "mentions": [],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.has_media
        assert len(message.media) == 1
        assert message.media[0].type == MessageType.IMAGE
        assert message.media[0].file_name == "photo.jpg"

    def test_mention_detection(self, mock_aiohttp, signal_config):
        """Test bot mention detection."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Hey @bot!",
                    "groupInfo": None,
                    "attachments": [],
                    "mentions": [
                        {"number": "+1234567890"}  # Bot's number
                    ],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.is_bot_mentioned is True

    def test_sync_message_sent_message_converts_as_self_chat(self, mock_aiohttp, signal_config):
        """2026-08-28: a Note-to-Self (or any own-account message synced to
        this linked device) arrives as syncMessage.sentMessage, not
        dataMessage. Before this fix _convert_message returned None for it
        unconditionally -- found live testing against a real linked Signal
        account with no second account available to send a genuine
        third-party dataMessage. envelope.source is already the account's
        own number in this shape, which is what self_chat.py's
        SelfChatHandler.is_self_message() needs to route it as a self-chat,
        mirroring WhatsApp's existing self-chat flow."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1234567890",  # the account's OWN number
                "sourceName": "+1234567890",
                "timestamp": 1699000000000,
                "syncMessage": {
                    "sentMessage": {
                        "destination": "+1234567890",
                        "message": "note to self",
                        "groupInfo": None,
                        "attachments": [],
                        "mentions": [],
                    }
                },
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.sender_id == "+1234567890"
        assert message.text == "note to self"
        assert message.is_group is False

    def test_data_message_still_takes_priority_over_sync_message(self, mock_aiohttp, signal_config):
        """A real dataMessage (an actual third-party sender) must never be
        shadowed by the syncMessage fallback -- dataMessage is checked
        first and wins whenever both keys happen to be present."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {"message": "real message", "groupInfo": None,
                                 "attachments": [], "mentions": []},
                "syncMessage": {"sentMessage": {"message": "should be ignored"}},
            }
        }

        message = adapter._convert_message(msg_data)

        assert message.sender_id == "+1987654321"
        assert message.text == "real message"

    def test_own_sent_message_echo_is_not_reprocessed(self, mock_aiohttp, signal_config):
        """2026-08-28: Signal syncs every message this adapter sends back to
        every linked device, including this one -- without tracking our own
        sends, that echo re-entered as a new inbound self-chat message and
        every reply was visibly delivered twice. A timestamp this adapter
        just sent must be recognized and dropped, not converted."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)
        adapter._remember_own_sent_timestamp("1699000000000")

        echo = {
            "envelope": {
                "source": "+1234567890",
                "timestamp": 1699000000000,
                "syncMessage": {"sentMessage": {
                    "destination": "+1234567890",
                    "timestamp": 1699000000000,
                    "message": "Let me check that for you...",
                }},
            }
        }

        assert adapter._convert_message(echo) is None

    def test_own_sent_timestamp_consumed_once(self, mock_aiohttp, signal_config):
        """A real, later Note-to-Self reusing the same timestamp value would
        be pathological, but the tracking must still be one-shot: don't
        leave a stale entry silently swallowing a future message."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)
        adapter._remember_own_sent_timestamp("1699000000000")

        assert adapter._is_own_sent_echo({"timestamp": 1699000000000}) is True
        assert adapter._is_own_sent_echo({"timestamp": 1699000000000}) is False

    def test_genuine_note_to_self_still_converts(self, mock_aiohttp, signal_config):
        """The echo guard must not swallow a REAL Note-to-Self just because
        no send happened yet -- only a timestamp this adapter itself sent
        is suppressed."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)
        # No _remember_own_sent_timestamp call -- this timestamp was never
        # sent by us, so it must convert normally.
        msg_data = {
            "envelope": {
                "source": "+1234567890",
                "timestamp": 1699000000001,
                "syncMessage": {"sentMessage": {
                    "destination": "+1234567890",
                    "timestamp": 1699000000001,
                    "message": "genuine note",
                }},
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.text == "genuine note"

    def test_media_type_detection(self, mock_aiohttp, signal_config):
        """Test media type detection from content type."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        assert adapter._get_media_type("image/jpeg") == MessageType.IMAGE
        assert adapter._get_media_type("image/png") == MessageType.IMAGE
        assert adapter._get_media_type("video/mp4") == MessageType.VIDEO
        assert adapter._get_media_type("audio/ogg") == MessageType.AUDIO
        assert adapter._get_media_type("application/pdf") == MessageType.DOCUMENT
        assert adapter._get_media_type("unknown/type") == MessageType.DOCUMENT


class TestSignalPolling:
    """2026-08-28: found live against a real signal-cli-rest-api instance --
    every endpoint on the current release mislabels its response body as
    text/plain (with or without a charset) even though the body is valid
    JSON. aiohttp's response.json() is strict about content-type by
    default and raises ContentTypeError on a mismatch, which silently
    broke the /v1/receive poll loop every cycle (30s) -- the adapter
    still reported CONNECTED, so this never surfaced as a connect
    failure, only as messages never arriving. Fixed by passing
    content_type=None everywhere the adapter calls response.json().
    """

    @pytest.mark.asyncio
    async def test_poll_tolerates_mislabeled_text_plain_content_type(self):
        from integrations.channels.base import ChannelConfig
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8090"})
        adapter = SignalAdapter(config)

        class _RealisticResponse:
            """Mimics aiohttp's actual json() contract: raises unless the
            caller explicitly opts out of the content-type check, exactly
            what a real ContentTypeError against a text/plain body does."""
            status = 200

            async def json(self, content_type='application/json'):
                if content_type is not None:
                    raise aiohttp.ContentTypeError(
                        request_info=None, history=(),
                        message="Attempt to decode JSON with unexpected mimetype: text/plain; charset=utf-8")
                return [{
                    "envelope": {
                        "source": "+1987654321", "sourceName": "Tester",
                        "timestamp": 1699000000000,
                        "dataMessage": {"message": "hi", "groupInfo": None,
                                        "attachments": [], "mentions": []},
                    }
                }]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        received = []
        adapter.on_message(lambda m: received.append(m) or None)
        adapter._session = MagicMock()
        adapter._session.get = MagicMock(return_value=_RealisticResponse())
        adapter._running = True

        # Real (unmocked) scheduling: the mocked get()/json() resolve near-
        # instantly, so one iteration's dispatch completes well before the
        # loop's own real asyncio.sleep(1) between polls -- no need to fake
        # the clock, just give the task a short real window then stop it.
        poll_task = asyncio.create_task(adapter._poll_messages())
        try:
            await asyncio.wait_for(poll_task, timeout=0.3)
        except asyncio.TimeoutError:
            pass
        finally:
            adapter._running = False
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass

        assert len(received) == 1
        assert received[0].content == "hi"


class TestSignalSending:
    """Tests for Signal message sending."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200
        response.json = AsyncMock(return_value={"timestamp": 1699000000000})

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        session.close = AsyncMock()

        return session

    @pytest.mark.asyncio
    async def test_send_direct_message(self, mock_session):
        """Test sending direct message."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="+1987654321",
                text="Hello!",
            )

            assert result.success
            assert result.message_id == "1699000000000"
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_group_message(self, mock_session):
        """Test sending group message."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="group.abc123",
                text="Hello group!",
            )

            assert result.success

    @pytest.mark.asyncio
    async def test_send_with_quote(self, mock_session):
        """Test sending message with quote/reply."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="+1987654321",
                text="Reply to this",
                reply_to="1699000000000",
            )

            assert result.success
            # 2026-08-28: a bare quote_timestamp (no quote_author/quote_message)
            # is rejected by the real API with "Quote author parameter is
            # missing", failing the WHOLE send -- found live, every in-thread
            # reply was silently dropped. Guard against that regressing.
            sent_payload = mock_session.post.call_args.kwargs.get("json", {})
            assert "quote_timestamp" not in sent_payload

    @pytest.mark.asyncio
    async def test_send_not_connected(self):
        """Test sending when not connected."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            # Don't set _session

            result = await adapter.send_message(
                chat_id="+1987654321",
                text="Hello!",
            )

            assert not result.success
            assert "Not connected" in result.error


class TestSignalReactions:
    """Tests for Signal reactions."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_send_reaction(self, mock_session):
        """Test sending reaction."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_reaction(
                chat_id="+1987654321",
                message_id="1699000000000",
                emoji="",
            )

            assert result is True
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_reaction(self, mock_session):
        """Test removing reaction."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_reaction(
                chat_id="+1987654321",
                message_id="1699000000000",
                emoji="",
                remove=True,
            )

            assert result is True


class TestSignalGroups:
    """Tests for Signal group operations."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200
        response.json = AsyncMock(return_value={"id": "newgroupid123"})

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_create_group(self, mock_session):
        """Test creating group."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.create_group(
                name="Test Group",
                members=["+1987654321", "+1555555555"],
            )

            assert result == "group.newgroupid123"

    @pytest.mark.asyncio
    async def test_get_group_info(self, mock_session):
        """Test getting group info."""
        from integrations.channels.signal_adapter import SignalAdapter

        mock_session.get.return_value = AsyncMock(
            __aenter__=AsyncMock(return_value=AsyncMock(
                status=200,
                json=AsyncMock(return_value={
                    "name": "Test Group",
                    "members": ["+1234567890", "+1987654321"],
                })
            ))
        )

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.get_chat_info("group.abc123")

            assert result is not None
            assert result["type"] == "group"
            assert result["name"] == "Test Group"


class TestSignalFactory:
    """Tests for Signal adapter factory."""

    def test_factory_with_params(self):
        """Test factory function with parameters."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.signal_adapter import create_signal_adapter

            adapter = create_signal_adapter(
                phone_number="+1234567890",
                api_url="http://signal-api:8080",
            )

            assert adapter.name == "signal"
            assert adapter._phone_number == "+1234567890"
            assert adapter._api_url == "http://signal-api:8080"

    def test_factory_stamps_phone_number_into_extra_for_self_chat(self):
        """2026-08-28: self_chat.py's is_self_message() reads
        extra.get('owner_phone') or extra.get('phone_number') to recognize
        the account's own number -- without this, every self-chat message
        would convert fine but never actually route as one."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.signal_adapter import create_signal_adapter

            adapter = create_signal_adapter(phone_number="+1234567890", api_url="http://x:8080")

            assert adapter.config.extra.get("phone_number") == "+1234567890"

    def test_factory_with_env_vars(self):
        """Test factory function with environment variables."""
        with patch.dict(os.environ, {
            "SIGNAL_PHONE_NUMBER": "+1999999999",
            "SIGNAL_API_URL": "http://env-signal:8080",
        }):
            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.signal_adapter import create_signal_adapter

                adapter = create_signal_adapter()

                assert adapter._phone_number == "+1999999999"
                assert adapter._api_url == "http://env-signal:8080"

    def test_factory_missing_phone(self):
        """Test factory function without phone number."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove env vars
            os.environ.pop("SIGNAL_PHONE_NUMBER", None)

            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.signal_adapter import create_signal_adapter

                with pytest.raises(ValueError, match="phone number required"):
                    create_signal_adapter()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
