"""
Admin Dashboard - Server-side Dashboard Data Provider

Provides aggregated statistics and real-time data for admin dashboards.
Designed for Docker environments with persistent stats storage.

Features:
- Dashboard statistics aggregation
- Active session monitoring
- Channel status tracking
- Queue statistics
- Error logging with persistence
- Docker volume support for data persistence
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)


class ChannelStatus(str, Enum):
    """Channel connection status."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    ERROR = "error"
    PAUSED = "paused"
    RATE_LIMITED = "rate_limited"


class ErrorSeverity(str, Enum):
    """Error severity levels."""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class DashboardStats:
    """Aggregated dashboard statistics."""
    timestamp: str = ""
    uptime_seconds: float = 0.0
    # Message stats
    total_messages_processed: int = 0
    messages_today: int = 0
    messages_this_hour: int = 0
    messages_per_minute: float = 0.0
    # Session stats
    active_sessions: int = 0
    total_sessions_today: int = 0
    # Channel stats
    active_channels: int = 0
    total_channels: int = 0
    # Queue stats
    queue_depth: int = 0
    queue_processing_rate: float = 0.0
    # Performance stats
    avg_response_time_ms: float = 0.0
    p99_response_time_ms: float = 0.0
    # Error stats
    error_count_today: int = 0
    error_rate_percent: float = 0.0
    # Resource stats
    memory_usage_mb: float = 0.0
    cpu_usage_percent: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SessionInfo:
    """Information about an active session."""
    session_id: str
    channel: str
    user_id: str
    chat_id: str
    started_at: str
    last_activity: str
    message_count: int = 0
    state: str = "active"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChannelStatusInfo:
    """Status information for a channel."""
    channel_type: str
    name: str
    status: str
    connected_at: Optional[str] = None
    last_activity: Optional[str] = None
    message_count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0
    rate_limit_remaining: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QueueStats:
    """Queue statistics."""
    total_queues: int = 0
    total_messages: int = 0
    pending_messages: int = 0
    processing_messages: int = 0
    completed_today: int = 0
    failed_today: int = 0
    avg_wait_time_ms: float = 0.0
    avg_processing_time_ms: float = 0.0
    throughput_per_second: float = 0.0
    by_channel: Dict[str, int] = field(default_factory=dict)
    by_priority: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ErrorEntry:
    """Error log entry."""
    timestamp: str
    severity: str
    channel: Optional[str]
    error_type: str
    message: str
    stack_trace: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DashboardConfig:
    """Configuration for the dashboard."""
    # Persistence path (should be volume-mounted in Docker)
    persistence_path: Optional[str] = None
    # Error log settings
    max_error_log_size: int = 10000
    error_log_retention_days: int = 7
    # Stats settings
    stats_update_interval_seconds: int = 60
    stats_history_hours: int = 24

    def get_persistence_path(self) -> str:
        """Get persistence path, defaulting to Docker-friendly location."""
        if self.persistence_path:
            return self.persistence_path
        import sys as _sys
        if os.environ.get('NUNBA_BUNDLED') or getattr(_sys, 'frozen', False):
            try:
                from core.platform_paths import get_agent_data_dir
                _default = os.path.join(get_agent_data_dir(), 'dashboard')
            except ImportError:
                _default = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data', 'dashboard')
        elif os.path.exists("/app"):
            _default = "/app/data/dashboard"
        else:
            _default = "./agent_data/dashboard"
        return os.environ.get("DASHBOARD_DATA_PATH", _default)


class AdminDashboard:
    """
    Server-side dashboard data provider.

    Aggregates statistics from various sources and provides data for
    admin dashboards. Designed for Docker environments with persistent storage.

    Usage:
        dashboard = AdminDashboard()

        # Get overall stats
        stats = dashboard.get_stats()

        # Get active sessions
        sessions = dashboard.get_active_sessions()

        # Get channel status
        channels = dashboard.get_channel_status()

        # Get recent errors
        errors = dashboard.get_error_log(limit=50)
    """

    def __init__(self, config: Optional[DashboardConfig] = None):
        self.config = config or DashboardConfig()
        self._start_time = time.time()

        # In-memory data stores
        self._sessions: Dict[str, SessionInfo] = {}
        self._channels: Dict[str, ChannelStatusInfo] = {}
        self._error_log: deque = deque(maxlen=self.config.max_error_log_size)
        self._message_counts: Dict[str, int] = {}  # By hour
        self._response_times: List[float] = []
        self._stats_history: List[Dict[str, Any]] = []

        # Counters
        self._total_messages = 0
        self._total_errors = 0
        self._messages_today = 0
        self._errors_today = 0
        self._last_day_reset = datetime.now().date()

        # Ensure persistence directory exists
        self._ensure_persistence_dir()

        # Load persisted state
        self._load_state()

    def _ensure_persistence_dir(self) -> None:
        """Ensure persistence directory exists."""
        path = self.config.get_persistence_path()
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create persistence directory {path}: {e}")

    def _get_state_file(self) -> str:
        """Get path to state file."""
        return os.path.join(self.config.get_persistence_path(), "dashboard_state.json")

    def _get_errors_file(self) -> str:
        """Get path to errors file."""
        return os.path.join(self.config.get_persistence_path(), "error_log.json")

    def _load_state(self) -> None:
        """Load persisted state."""
        state_file = self._get_state_file()
        try:
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    data = json.load(f)
                    self._total_messages = data.get("total_messages", 0)
                    self._total_errors = data.get("total_errors", 0)
                    self._message_counts = data.get("message_counts", {})
                    logger.info(f"Loaded dashboard state from {state_file}")
        except Exception as e:
            logger.warning(f"Could not load dashboard state: {e}")

        # Load error log
        errors_file = self._get_errors_file()
        try:
            if os.path.exists(errors_file):
                with open(errors_file, "r") as f:
                    errors = json.load(f)
                    for err in errors[-self.config.max_error_log_size:]:
                        self._error_log.append(ErrorEntry(**err))
                    logger.info(f"Loaded {len(self._error_log)} errors from {errors_file}")
        except Exception as e:
            logger.warning(f"Could not load error log: {e}")

    def _save_state(self) -> None:
        """Persist state to disk."""
        state_file = self._get_state_file()
        try:
            data = {
                "total_messages": self._total_messages,
                "total_errors": self._total_errors,
                "message_counts": self._message_counts,
                "saved_at": datetime.now().isoformat(),
            }
            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save dashboard state: {e}")

    def _save_errors(self) -> None:
        """Persist error log to disk."""
        errors_file = self._get_errors_file()
        try:
            errors = [err.to_dict() for err in self._error_log]
            with open(errors_file, "w") as f:
                json.dump(errors, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save error log: {e}")

    def _check_day_reset(self) -> None:
        """Reset daily counters if day changed."""
        today = datetime.now().date()
        if today > self._last_day_reset:
            self._messages_today = 0
            self._errors_today = 0
            self._last_day_reset = today

    def get_stats(self) -> DashboardStats:
        """
        Get aggregated dashboard statistics.

        Returns:
            DashboardStats with current system metrics
        """
        self._check_day_reset()
        now = datetime.now()

        # Calculate messages per minute
        hour_key = now.strftime("%Y-%m-%d-%H")
        messages_this_hour = self._message_counts.get(hour_key, 0)
        minutes_in_hour = now.minute + 1
        messages_per_minute = messages_this_hour / max(1, minutes_in_hour)

        # Calculate error rate
        error_rate = 0.0
        if self._messages_today > 0:
            error_rate = (self._errors_today / self._messages_today) * 100

        # Calculate response time percentiles
        avg_response = 0.0
        p99_response = 0.0
        if self._response_times:
            avg_response = sum(self._response_times) / len(self._response_times)
            sorted_times = sorted(self._response_times)
            p99_idx = int(len(sorted_times) * 0.99)
            p99_response = sorted_times[min(p99_idx, len(sorted_times) - 1)]

        # Get memory usage (basic)
        memory_mb = 0.0
        try:
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / (1024 * 1024)
        except ImportError:
            pass

        return DashboardStats(
            timestamp=now.isoformat(),
            uptime_seconds=time.time() - self._start_time,
            total_messages_processed=self._total_messages,
            messages_today=self._messages_today,
            messages_this_hour=messages_this_hour,
            messages_per_minute=messages_per_minute,
            active_sessions=len(self._sessions),
            total_sessions_today=len(self._sessions),
            active_channels=len([c for c in self._channels.values() if c.status == "connected"]),
            total_channels=len(self._channels),
            queue_depth=0,  # To be populated by queue integration
            queue_processing_rate=0.0,
            avg_response_time_ms=avg_response,
            p99_response_time_ms=p99_response,
            error_count_today=self._errors_today,
            error_rate_percent=error_rate,
            memory_usage_mb=memory_mb,
            cpu_usage_percent=0.0,
        )

    def get_active_sessions(self) -> List[SessionInfo]:
        """
        Get list of active sessions.

        Returns:
            List of SessionInfo for all active sessions
        """
        return list(self._sessions.values())

    def get_channel_status(self) -> Dict[str, ChannelStatusInfo]:
        """
        Get status of all channels.

        Returns:
            Dictionary mapping channel type to ChannelStatusInfo
        """
        return self._channels.copy()

    def get_queue_stats(self) -> QueueStats:
        """
        Get queue statistics.

        Returns:
            QueueStats with current queue metrics
        """
        # This would be populated by integrating with the actual queue system
        return QueueStats(
            total_queues=0,
            total_messages=0,
            pending_messages=0,
            processing_messages=0,
            completed_today=0,
            failed_today=0,
            avg_wait_time_ms=0.0,
            avg_processing_time_ms=0.0,
            throughput_per_second=0.0,
        )

    def get_error_log(self, limit: int = 100) -> List[ErrorEntry]:
        """
        Get recent error log entries.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of ErrorEntry objects, most recent first
        """
        errors = list(self._error_log)
        errors.reverse()  # Most recent first
        return errors[:limit]

    # Data recording methods (called by other components)

    def record_message(self, channel: str) -> None:
        """Record a message being processed."""
        self._check_day_reset()
        self._total_messages += 1
        self._messages_today += 1

        # Track by hour
        hour_key = datetime.now().strftime("%Y-%m-%d-%H")
        self._message_counts[hour_key] = self._message_counts.get(hour_key, 0) + 1

        # Clean old hour keys (keep last 48 hours)
        cutoff = datetime.now() - timedelta(hours=48)
        cutoff_key = cutoff.strftime("%Y-%m-%d-%H")
        self._message_counts = {
            k: v for k, v in self._message_counts.items() if k >= cutoff_key
        }

        # Update channel message count
        if channel in self._channels:
            self._channels[channel].message_count += 1
            self._channels[channel].last_activity = datetime.now().isoformat()

        self._save_state()

    def record_response_time(self, latency_ms: float) -> None:
        """Record a response time measurement."""
        self._response_times.append(latency_ms)
        # Keep only last 10000 measurements
        if len(self._response_times) > 10000:
            self._response_times = self._response_times[-10000:]

    def record_error(
        self,
        error_type: str,
        message: str,
        channel: Optional[str] = None,
        severity: ErrorSeverity = ErrorSeverity.ERROR,
        stack_trace: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record an error."""
        self._check_day_reset()
        self._total_errors += 1
        self._errors_today += 1

        if channel and channel in self._channels:
            self._channels[channel].error_count += 1

        entry = ErrorEntry(
            timestamp=datetime.now().isoformat(),
            severity=severity.value,
            channel=channel,
            error_type=error_type,
            message=message,
            stack_trace=stack_trace,
            context=context or {},
        )
        self._error_log.append(entry)
        self._save_errors()
        self._save_state()

    def register_session(
        self,
        session_id: str,
        channel: str,
        user_id: str,
        chat_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionInfo:
        """Register a new active session."""
        now = datetime.now().isoformat()
        session = SessionInfo(
            session_id=session_id,
            channel=channel,
            user_id=user_id,
            chat_id=chat_id,
            started_at=now,
            last_activity=now,
            metadata=metadata or {},
        )
        self._sessions[session_id] = session
        return session

    def update_session(self, session_id: str, message_count_delta: int = 1) -> None:
        """Update session activity."""
        if session_id in self._sessions:
            self._sessions[session_id].last_activity = datetime.now().isoformat()
            self._sessions[session_id].message_count += message_count_delta

    def unregister_session(self, session_id: str) -> None:
        """Remove a session."""
        if session_id in self._sessions:
            del self._sessions[session_id]

    def register_channel(
        self,
        channel_type: str,
        name: str,
        status: ChannelStatus = ChannelStatus.DISCONNECTED,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChannelStatusInfo:
        """Register a channel."""
        channel_info = ChannelStatusInfo(
            channel_type=channel_type,
            name=name,
            status=status.value,
            metadata=metadata or {},
        )
        self._channels[channel_type] = channel_info
        return channel_info

    def update_channel_status(
        self,
        channel_type: str,
        status: ChannelStatus,
        latency_ms: Optional[float] = None,
    ) -> None:
        """Update channel status."""
        if channel_type in self._channels:
            self._channels[channel_type].status = status.value
            if status == ChannelStatus.CONNECTED:
                self._channels[channel_type].connected_at = datetime.now().isoformat()
            if latency_ms is not None:
                self._channels[channel_type].avg_latency_ms = latency_ms


# Singleton instance
_dashboard: Optional[AdminDashboard] = None


def get_dashboard(config: Optional[DashboardConfig] = None) -> AdminDashboard:
    """Get or create the global dashboard instance."""
    global _dashboard
    if _dashboard is None:
        _dashboard = AdminDashboard(config)
    return _dashboard
