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

    @patch('integrations.channels.response.router.ChannelResponseRouter._notify_desktop_wamp')
    @patch('integrations.channels.response.router.ChannelResponseRouter._async_fan_out')
    @patch('integrations.channels.response.router.ChannelResponseRouter._log_conversation')
    def test_route_response_no_fanout(self, mock_log, mock_fan, mock_wamp):
        """Test route_response with fan_out=False skips fan-out."""
        self.router.route_response('user1', 'hello', None, fan_out=False)
        mock_fan.assert_not_called()
        mock_wamp.assert_called_once()
