"""
Admin API Schemas

Pydantic models for request/response validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, List, Any, Union
from enum import Enum


class ChannelType(str, Enum):
    """Supported channel types."""
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    MATRIX = "matrix"
    TEAMS = "teams"
    LINE = "line"
    SIGNAL = "signal"
    IMESSAGE = "imessage"
    GOOGLE_CHAT = "google_chat"
    WEB = "web"
    MATTERMOST = "mattermost"
    NEXTCLOUD = "nextcloud"


class ChannelStatus(str, Enum):
    """Channel connection status."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    ERROR = "error"
    PAUSED = "paused"


class TaskStatus(str, Enum):
    """Automation task status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ChannelConfigSchema:
    """Channel configuration schema."""
    channel_type: str
    name: str
    enabled: bool = True
    config: Dict[str, Any] = field(default_factory=dict)
    rate_limit: Optional[Dict[str, int]] = None
    security: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChannelStatusSchema:
    """Channel status response."""
    channel_type: str
    name: str
    status: str
    connected_at: Optional[str] = None
    message_count: int = 0
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QueueConfigSchema:
    """Queue/pipeline configuration."""
    max_size: int = 10000
    debounce_ms: int = 500
    dedupe_window_seconds: int = 60
    concurrency_limits: Dict[str, int] = field(default_factory=lambda: {
        "max_per_user": 4,
        "max_per_channel": 20,
        "max_per_chat": 2,
        "max_global": 100
    })
    rate_limits: Dict[str, Any] = field(default_factory=lambda: {
        "requests_per_minute": 60,
        "requests_per_hour": 1000,
        "burst_limit": 10
    })
    retry_config: Dict[str, Any] = field(default_factory=lambda: {
        "max_retries": 3,
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 60.0,
        "exponential_base": 2.0
    })
    batching: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "max_batch_size": 10,
        "max_wait_ms": 100
    })

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QueueStatsSchema:
    """Queue statistics response."""
    queue_size: int = 0
    pending_messages: int = 0
    processing_messages: int = 0
    completed_messages: int = 0
    failed_messages: int = 0
    avg_processing_time_ms: float = 0.0
    messages_per_minute: float = 0.0
    by_channel: Dict[str, int] = field(default_factory=dict)
    by_priority: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CommandConfigSchema:
    """Command configuration."""
    name: str
    description: str
    pattern: str
    handler: str
    enabled: bool = True
    admin_only: bool = False
    cooldown_seconds: int = 0
    usage_limit: Optional[int] = None
    aliases: List[str] = field(default_factory=list)
    arguments: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MentionGatingConfigSchema:
    """Mention gating configuration."""
    enabled: bool = True
    require_mention_in_groups: bool = True
    allow_reply_chain: bool = True
    keywords: List[str] = field(default_factory=list)
    exempt_users: List[str] = field(default_factory=list)
    exempt_channels: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WebhookConfigSchema:
    """Webhook configuration."""
    id: Optional[str] = None
    name: str = ""
    url: str = ""
    secret: Optional[str] = None
    events: List[str] = field(default_factory=list)
    enabled: bool = True
    retry_count: int = 3
    timeout_seconds: int = 30
    headers: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CronJobSchema:
    """Cron job configuration."""
    id: Optional[str] = None
    name: str = ""
    schedule: str = ""  # Cron expression
    handler: str = ""
    enabled: bool = True
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    run_count: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TriggerConfigSchema:
    """Trigger configuration."""
    id: Optional[str] = None
    name: str = ""
    trigger_type: str = ""  # message, reaction, join, leave, etc.
    pattern: Optional[str] = None
    conditions: Dict[str, Any] = field(default_factory=dict)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    priority: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowSchema:
    """Workflow configuration."""
    id: Optional[str] = None
    name: str = ""
    description: str = ""
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScheduledMessageSchema:
    """Scheduled message configuration."""
    id: Optional[str] = None
    channel: str = ""
    chat_id: str = ""
    message: str = ""
    scheduled_time: str = ""  # ISO format
    recurring: bool = False
    recurrence_pattern: Optional[str] = None
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AutomationConfigSchema:
    """Full automation configuration."""
    webhooks: List[WebhookConfigSchema] = field(default_factory=list)
    cron_jobs: List[CronJobSchema] = field(default_factory=list)
    triggers: List[TriggerConfigSchema] = field(default_factory=list)
    workflows: List[WorkflowSchema] = field(default_factory=list)
    scheduled_messages: List[ScheduledMessageSchema] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "webhooks": [w.to_dict() for w in self.webhooks],
            "cron_jobs": [c.to_dict() for c in self.cron_jobs],
            "triggers": [t.to_dict() for t in self.triggers],
            "workflows": [w.to_dict() for w in self.workflows],
            "scheduled_messages": [s.to_dict() for s in self.scheduled_messages],
        }


@dataclass
class IdentityConfigSchema:
    """Agent identity configuration."""
    agent_id: str = ""
    display_name: str = ""
    avatar_url: Optional[str] = None
    bio: str = ""
    personality: Dict[str, Any] = field(default_factory=dict)
    response_style: Dict[str, Any] = field(default_factory=dict)
    per_channel_identity: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AvatarSchema:
    """Avatar configuration."""
    id: Optional[str] = None
    name: str = ""
    url: str = ""
    channel: Optional[str] = None
    is_default: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SenderMappingSchema:
    """Sender mapping configuration."""
    platform_user_id: str = ""
    internal_user_id: str = ""
    channel: str = ""
    display_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PluginConfigSchema:
    """Plugin configuration."""
    id: str = ""
    name: str = ""
    version: str = ""
    description: str = ""
    enabled: bool = True
    config: Dict[str, Any] = field(default_factory=dict)
    hooks: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SessionConfigSchema:
    """Session configuration."""
    session_id: str = ""
    user_id: str = ""
    channel: str = ""
    chat_id: str = ""
    created_at: str = ""
    last_activity: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PairingRequestSchema:
    """Session pairing request."""
    source_channel: str = ""
    source_chat_id: str = ""
    target_channel: str = ""
    target_chat_id: str = ""
    bidirectional: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MetricsSchema:
    """System metrics response."""
    timestamp: str = ""
    uptime_seconds: float = 0.0
    total_messages_processed: int = 0
    messages_per_minute: float = 0.0
    active_sessions: int = 0
    active_channels: int = 0
    queue_depth: int = 0
    memory_usage_mb: float = 0.0
    cpu_usage_percent: float = 0.0
    error_rate: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p99_ms: float = 0.0
    by_channel: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryStoreConfigSchema:
    """Memory store configuration."""
    backend: str = "file"  # file, redis, sqlite
    max_entries: int = 10000
    ttl_seconds: Optional[int] = None
    path: Optional[str] = None
    connection_string: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SecurityConfigSchema:
    """Security configuration."""
    allowed_users: List[str] = field(default_factory=list)
    blocked_users: List[str] = field(default_factory=list)
    allowed_channels: List[str] = field(default_factory=list)
    blocked_channels: List[str] = field(default_factory=list)
    rate_limit_per_user: int = 60
    require_authentication: bool = False
    encryption_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MediaConfigSchema:
    """Media handling configuration."""
    max_file_size_mb: int = 25
    allowed_types: List[str] = field(default_factory=lambda: [
        "image/jpeg", "image/png", "image/gif", "image/webp",
        "audio/mpeg", "audio/ogg", "audio/wav",
        "video/mp4", "video/webm",
        "application/pdf", "text/plain"
    ])
    vision_enabled: bool = True
    audio_transcription_enabled: bool = True
    tts_enabled: bool = False
    image_generation_enabled: bool = False
    link_preview_enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ResponseConfigSchema:
    """Response handling configuration."""
    typing_indicator_enabled: bool = True
    typing_delay_ms: int = 50
    reactions_enabled: bool = True
    templates_enabled: bool = True
    streaming_enabled: bool = False
    max_response_length: int = 4096
    split_long_messages: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GlobalConfigSchema:
    """Global system configuration."""
    queue: QueueConfigSchema = field(default_factory=QueueConfigSchema)
    security: SecurityConfigSchema = field(default_factory=SecurityConfigSchema)
    media: MediaConfigSchema = field(default_factory=MediaConfigSchema)
    response: ResponseConfigSchema = field(default_factory=ResponseConfigSchema)
    memory: MemoryStoreConfigSchema = field(default_factory=MemoryStoreConfigSchema)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue": self.queue.to_dict(),
            "security": self.security.to_dict(),
            "media": self.media.to_dict(),
            "response": self.response.to_dict(),
            "memory": self.memory.to_dict(),
        }


# Response wrappers
@dataclass
class APIResponse:
    """Standard API response wrapper."""
    success: bool = True
    data: Any = None
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "success": self.success,
            "timestamp": self.timestamp,
        }
        if self.data is not None:
            if hasattr(self.data, "to_dict"):
                result["data"] = self.data.to_dict()
            elif isinstance(self.data, list):
                result["data"] = [
                    d.to_dict() if hasattr(d, "to_dict") else d for d in self.data
                ]
            else:
                result["data"] = self.data
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class PaginatedResponse:
    """Paginated API response."""
    items: List[Any] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_next: bool = False
    has_prev: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [
                i.to_dict() if hasattr(i, "to_dict") else i for i in self.items
            ],
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "has_next": self.has_next,
            "has_prev": self.has_prev,
        }
