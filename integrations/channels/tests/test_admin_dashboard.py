"""
Tests for Admin Dashboard

Tests dashboard statistics, session management, channel status tracking,
error logging, and Docker persistence features.
"""

import asyncio
import json
import os
import pytest
import tempfile
import time
from datetime import datetime, timedelta

# Import the dashboard module
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from integrations.channels.admin.dashboard import (
    AdminDashboard,
    DashboardConfig,
    DashboardStats,
    SessionInfo,
    ChannelStatusInfo,
    QueueStats,
    ErrorEntry,
    ErrorSeverity,
    ChannelStatus,
    get_dashboard,
)


class TestDashboardStats:
    """Tests for DashboardStats dataclass."""

    def test_create_stats(self):
        """Test creating dashboard stats."""
        stats = DashboardStats(
            timestamp="2025-01-01T00:00:00",
            uptime_seconds=3600.0,
            total_messages_processed=1000,
            active_sessions=5,
        )
        assert stats.uptime_seconds == 3600.0
        assert stats.total_messages_processed == 1000
        assert stats.active_sessions == 5

    def test_to_dict(self):
        """Test serialization to dict."""
        stats = DashboardStats(
            timestamp="2025-01-01T00:00:00",
            messages_today=100,
            error_count_today=2,
        )
        data = stats.to_dict()
        assert data["timestamp"] == "2025-01-01T00:00:00"
        assert data["messages_today"] == 100
        assert data["error_count_today"] == 2

    def test_default_values(self):
        """Test default values are set correctly."""
        stats = DashboardStats()
        assert stats.total_messages_processed == 0
        assert stats.messages_per_minute == 0.0
        assert stats.error_rate_percent == 0.0


class TestSessionInfo:
    """Tests for SessionInfo dataclass."""

    def test_create_session_info(self):
        """Test creating session info."""
        session = SessionInfo(
            session_id="sess-123",
            channel="telegram",
            user_id="user-456",
            chat_id="chat-789",
            started_at="2025-01-01T00:00:00",
            last_activity="2025-01-01T01:00:00",
            message_count=10,
        )
        assert session.session_id == "sess-123"
        assert session.channel == "telegram"
        assert session.message_count == 10

    def test_to_dict(self):
        """Test serialization."""
        session = SessionInfo(
            session_id="sess-123",
            channel="telegram",
            user_id="user-456",
            chat_id="chat-789",
            started_at="2025-01-01T00:00:00",
            last_activity="2025-01-01T01:00:00",
        )
        data = session.to_dict()
        assert data["session_id"] == "sess-123"
        assert data["channel"] == "telegram"


class TestChannelStatusInfo:
    """Tests for ChannelStatusInfo dataclass."""

    def test_create_channel_status(self):
        """Test creating channel status info."""
        status = ChannelStatusInfo(
            channel_type="telegram",
            name="Telegram Bot",
            status="connected",
            message_count=100,
        )
        assert status.channel_type == "telegram"
        assert status.status == "connected"
        assert status.message_count == 100

    def test_to_dict(self):
        """Test serialization."""
        status = ChannelStatusInfo(
            channel_type="discord",
            name="Discord Bot",
            status="disconnected",
            error_count=5,
        )
        data = status.to_dict()
        assert data["channel_type"] == "discord"
        assert data["error_count"] == 5


class TestAdminDashboard:
    """Tests for the main AdminDashboard class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def dashboard(self, temp_dir):
        """Create a dashboard with temp storage."""
        config = DashboardConfig(persistence_path=temp_dir)
        return AdminDashboard(config)

    def test_init(self, dashboard):
        """Test dashboard initialization."""
        assert dashboard is not None
        assert len(dashboard._sessions) == 0
        assert len(dashboard._channels) == 0

    def test_get_stats(self, dashboard):
        """Test getting dashboard stats."""
        stats = dashboard.get_stats()
        assert isinstance(stats, DashboardStats)
        assert stats.uptime_seconds >= 0
        assert stats.timestamp != ""

    def test_record_message(self, dashboard):
        """Test recording messages."""
        dashboard.record_message("telegram")
        dashboard.record_message("telegram")
        dashboard.record_message("discord")

        stats = dashboard.get_stats()
        assert stats.total_messages_processed == 3
        assert stats.messages_today == 3

    def test_record_response_time(self, dashboard):
        """Test recording response times."""
        dashboard.record_response_time(10.0)
        dashboard.record_response_time(20.0)
        dashboard.record_response_time(30.0)

        stats = dashboard.get_stats()
        assert stats.avg_response_time_ms == 20.0  # (10+20+30)/3

    def test_record_error(self, dashboard):
        """Test recording errors."""
        dashboard.record_error(
            error_type="timeout",
            message="Connection timed out",
            channel="telegram",
            severity=ErrorSeverity.ERROR,
        )

        stats = dashboard.get_stats()
        assert stats.error_count_today == 1

        errors = dashboard.get_error_log(limit=10)
        assert len(errors) == 1
        assert errors[0].error_type == "timeout"
        assert errors[0].channel == "telegram"

    def test_register_session(self, dashboard):
        """Test registering sessions."""
        session = dashboard.register_session(
            session_id="sess-123",
            channel="telegram",
            user_id="user-456",
            chat_id="chat-789",
        )

        assert session.session_id == "sess-123"
        assert len(dashboard._sessions) == 1

        sessions = dashboard.get_active_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-123"

    def test_update_session(self, dashboard):
        """Test updating session activity."""
        dashboard.register_session(
            session_id="sess-123",
            channel="telegram",
            user_id="user-456",
            chat_id="chat-789",
        )

        dashboard.update_session("sess-123", message_count_delta=5)

        sessions = dashboard.get_active_sessions()
        assert sessions[0].message_count == 5

    def test_unregister_session(self, dashboard):
        """Test removing sessions."""
        dashboard.register_session(
            session_id="sess-123",
            channel="telegram",
            user_id="user-456",
            chat_id="chat-789",
        )

        assert len(dashboard._sessions) == 1

        dashboard.unregister_session("sess-123")
        assert len(dashboard._sessions) == 0

    def test_register_channel(self, dashboard):
        """Test registering channels."""
        channel = dashboard.register_channel(
            channel_type="telegram",
            name="Telegram Bot",
            status=ChannelStatus.CONNECTED,
        )

        assert channel.channel_type == "telegram"
        assert channel.status == "connected"

        channels = dashboard.get_channel_status()
        assert "telegram" in channels
        assert channels["telegram"].status == "connected"

    def test_update_channel_status(self, dashboard):
        """Test updating channel status."""
        dashboard.register_channel(
            channel_type="telegram",
            name="Telegram Bot",
            status=ChannelStatus.DISCONNECTED,
        )

        dashboard.update_channel_status(
            "telegram",
            ChannelStatus.CONNECTED,
            latency_ms=45.0,
        )

        channels = dashboard.get_channel_status()
        assert channels["telegram"].status == "connected"
        assert channels["telegram"].avg_latency_ms == 45.0

    def test_get_queue_stats(self, dashboard):
        """Test getting queue stats."""
        stats = dashboard.get_queue_stats()
        assert isinstance(stats, QueueStats)
        assert stats.total_queues == 0

    def test_get_error_log_limit(self, dashboard):
        """Test error log with limit."""
        for i in range(10):
            dashboard.record_error(
                error_type=f"error_{i}",
                message=f"Error message {i}",
            )

        errors = dashboard.get_error_log(limit=5)
        assert len(errors) == 5
        # Most recent first
        assert errors[0].error_type == "error_9"

    def test_message_channel_tracking(self, dashboard):
        """Test that messages update channel counters."""
        dashboard.register_channel(
            channel_type="telegram",
            name="Telegram Bot",
            status=ChannelStatus.CONNECTED,
        )

        dashboard.record_message("telegram")
        dashboard.record_message("telegram")

        channels = dashboard.get_channel_status()
        assert channels["telegram"].message_count == 2
        assert channels["telegram"].last_activity is not None

    def test_error_channel_tracking(self, dashboard):
        """Test that errors update channel counters."""
        dashboard.register_channel(
            channel_type="telegram",
            name="Telegram Bot",
            status=ChannelStatus.CONNECTED,
        )

        dashboard.record_error(
            error_type="timeout",
            message="Timeout",
            channel="telegram",
        )

        channels = dashboard.get_channel_status()
        assert channels["telegram"].error_count == 1


class TestDashboardPersistence:
    """Tests for dashboard state persistence."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def test_state_persists(self, temp_dir):
        """Test that state persists across restarts."""
        config = DashboardConfig(persistence_path=temp_dir)

        # Create dashboard and record data
        dashboard1 = AdminDashboard(config)
        dashboard1.record_message("telegram")
        dashboard1.record_message("telegram")
        dashboard1.record_error("timeout", "Timeout error")

        total_messages = dashboard1._total_messages
        total_errors = dashboard1._total_errors

        # Create new dashboard instance (simulating restart)
        dashboard2 = AdminDashboard(config)
        assert dashboard2._total_messages == total_messages
        assert dashboard2._total_errors == total_errors

    def test_errors_persist(self, temp_dir):
        """Test that error log persists across restarts."""
        config = DashboardConfig(persistence_path=temp_dir)

        # Create dashboard and record errors
        dashboard1 = AdminDashboard(config)
        dashboard1.record_error("error_1", "First error")
        dashboard1.record_error("error_2", "Second error")

        # Create new dashboard instance
        dashboard2 = AdminDashboard(config)
        errors = dashboard2.get_error_log()
        assert len(errors) == 2


class TestErrorSeverity:
    """Tests for error severity levels."""

    def test_severity_levels(self):
        """Test all severity levels are defined."""
        assert ErrorSeverity.DEBUG.value == "debug"
        assert ErrorSeverity.INFO.value == "info"
        assert ErrorSeverity.WARNING.value == "warning"
        assert ErrorSeverity.ERROR.value == "error"
        assert ErrorSeverity.CRITICAL.value == "critical"


class TestChannelStatusEnum:
    """Tests for channel status enum."""

    def test_status_values(self):
        """Test all status values are defined."""
        assert ChannelStatus.CONNECTED.value == "connected"
        assert ChannelStatus.DISCONNECTED.value == "disconnected"
        assert ChannelStatus.CONNECTING.value == "connecting"
        assert ChannelStatus.ERROR.value == "error"
        assert ChannelStatus.PAUSED.value == "paused"
        assert ChannelStatus.RATE_LIMITED.value == "rate_limited"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
