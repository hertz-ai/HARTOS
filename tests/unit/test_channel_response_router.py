import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from integrations.channels.response.router import ChannelResponseRouter


class TestChannelResponseRouter:
    def setup_method(self):
        self.router = ChannelResponseRouter()

    @patch('integrations.channels.response.router.ChannelResponseRouter._get_db')
    def test_log_conversation(self, mock_get_db):
        """Test that conversation entries are logged to DB."""
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        self.router.log_user_message('user1', 'telegram', 'hello')
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch('integrations.channels.response.router.ChannelResponseRouter._get_db')
    def test_upsert_binding_creates_new(self, mock_get_db):
        """Test that upsert_binding creates a new binding when none exists."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_get_db.return_value = mock_db
        self.router.upsert_binding('user1', 'telegram', 'sender1', 'chat1')
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch('integrations.channels.response.router.ChannelResponseRouter._get_db')
    def test_upsert_binding_updates_existing(self, mock_get_db):
        """Test that upsert_binding updates an existing binding."""
        mock_existing = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_existing
        mock_get_db.return_value = mock_db
        self.router.upsert_binding('user1', 'telegram', 'sender1', 'chat1')
        assert mock_existing.is_active is True
        mock_db.add.assert_not_called()  # existing -- no add

    @patch('integrations.channels.response.router.ChannelResponseRouter._notify_desktop_wamp')
    @patch('integrations.channels.response.router.ChannelResponseRouter._async_fan_out')
    @patch('integrations.channels.response.router.ChannelResponseRouter._log_conversation')
    def test_route_response_calls_all(self, mock_log, mock_fan, mock_wamp):
        """Test route_response calls log + fan-out + WAMP."""
        ctx = {'channel': 'telegram', 'chat_id': '123'}
        self.router.route_response('user1', 'hello', ctx)
        mock_log.assert_called_once()
        mock_fan.assert_called_once()
        mock_wamp.assert_called_once()

    @patch('integrations.channels.response.router.ChannelResponseRouter.deliver_to_chat')
    @patch('integrations.channels.response.router.ChannelResponseRouter._notify_desktop_wamp')
    @patch('integrations.channels.response.router.ChannelResponseRouter._async_fan_out')
    @patch('integrations.channels.response.router.ChannelResponseRouter._log_conversation')
    def test_route_response_replies_to_origin_by_default(
            self, mock_log, mock_fan, mock_wamp, mock_origin):
        """Callers with no sender of their own (agentic_router) rely on it."""
        ctx = {'channel': 'slack', 'chat_id': 'C1'}
        self.router.route_response('user1', 'hello', ctx, fan_out=False)
        mock_origin.assert_called_once_with(
            channel='slack', chat_id='C1', text='hello')

    @patch('integrations.channels.response.router.ChannelResponseRouter.deliver_to_chat')
    @patch('integrations.channels.response.router.ChannelResponseRouter._notify_desktop_wamp')
    @patch('integrations.channels.response.router.ChannelResponseRouter._async_fan_out')
    @patch('integrations.channels.response.router.ChannelResponseRouter._log_conversation')
    def test_route_response_reply_to_origin_false_skips_origin(
            self, mock_log, mock_fan, mock_wamp, mock_origin):
        """A caller that delivers the reply itself opts out of the origin leg,
        but keeps logging, fan-out (origin excluded) and the WAMP notify."""
        ctx = {'channel': 'slack', 'chat_id': 'C1'}
        self.router.route_response('user1', 'hello', ctx, reply_to_origin=False)
        mock_origin.assert_not_called()
        mock_log.assert_called_once()
        assert mock_fan.call_args.kwargs['exclude_chat_id'] == 'C1'
        mock_wamp.assert_called_once()

    @patch('integrations.channels.response.router.ChannelResponseRouter._notify_desktop_wamp')
    @patch('integrations.channels.response.router.ChannelResponseRouter._async_fan_out')
    @patch('integrations.channels.response.router.ChannelResponseRouter._log_conversation')
    def test_route_response_no_fanout(self, mock_log, mock_fan, mock_wamp):
        """Test route_response with fan_out=False skips fan-out."""
        self.router.route_response('user1', 'hello', None, fan_out=False)
        mock_fan.assert_not_called()
        mock_wamp.assert_called_once()


class TestFanOutReachesOnlyConnectedChats:
    """Fan-out sends a user's notification to the chats THEY connected.

    upsert_binding records every inbound sender (auth_method unset), and an
    unbound sender resolves to the default user, the owner on a desktop.
    Fan-out from a worker thread was dead on main (no loop found), so this
    never fired; once #126 made the send loop reachable, an owner's
    outreach/journey notification went to every stranger DM and group that
    ever wrote to the bot (review of #126, measured by the reviewer:
    SENT [('discord','stranger-dm'), ('telegram','group-42')])."""

    def _binding(self, channel, chat, auth_method=None, preferred=False):
        from types import SimpleNamespace
        return SimpleNamespace(channel_type=channel, channel_chat_id=chat,
                               auth_method=auth_method, is_preferred=preferred)

    def test_auto_recorded_senders_are_skipped(self):
        import asyncio
        import threading
        from integrations.channels.base import SendResult
        router = ChannelResponseRouter()
        rows = [
            self._binding('discord', 'stranger-dm'),                  # auto
            self._binding('telegram', 'group-42'),                    # auto
            self._binding('telegram', 'owner-chat', auth_method='api_key'),
            self._binding('slack', 'owner-dm', preferred=True),
        ]
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = rows
        sent = []

        async def send(channel, chat, text):
            sent.append((channel, chat))
            return SendResult(success=True)

        registry = MagicMock()
        registry.send_to_channel.side_effect = send
        loop = asyncio.new_event_loop()
        runner = threading.Thread(target=loop.run_forever, daemon=True)
        runner.start()
        try:
            with patch.object(ChannelResponseRouter, '_get_db', return_value=db), \
                    patch.object(ChannelResponseRouter, '_get_registry',
                                 return_value=registry), \
                    patch.object(ChannelResponseRouter, '_get_send_loop',
                                 return_value=loop):
                router._async_fan_out('owner', 'A prospect replied')
                asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(2)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            runner.join(2)
            loop.close()
        assert sorted(sent) == [('slack', 'owner-dm'), ('telegram', 'owner-chat')]
