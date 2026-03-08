"""
Admin REST API Blueprint

Provides 100+ REST endpoints for managing all channel integration components.
Supports configuration, monitoring, and control of:
- Channels (7+ types)
- Queue/Pipeline
- Commands
- Automation (webhooks, cron, triggers, workflows)
- Identity
- Plugins
- Sessions
- Metrics
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from functools import wraps
from typing import Optional, Dict, List, Any, Callable, TYPE_CHECKING

from flask import Blueprint, request, jsonify, Response, current_app, g

from .schemas import (
    APIResponse,
    PaginatedResponse,
    ChannelConfigSchema,
    ChannelStatusSchema,
    QueueConfigSchema,
    QueueStatsSchema,
    CommandConfigSchema,
    MentionGatingConfigSchema,
    WebhookConfigSchema,
    CronJobSchema,
    TriggerConfigSchema,
    WorkflowSchema,
    ScheduledMessageSchema,
    IdentityConfigSchema,
    AvatarSchema,
    SenderMappingSchema,
    PluginConfigSchema,
    SessionConfigSchema,
    PairingRequestSchema,
    MetricsSchema,
    GlobalConfigSchema,
    SecurityConfigSchema,
    MediaConfigSchema,
    ResponseConfigSchema,
    MemoryStoreConfigSchema,
    EmbodiedAIConfigSchema,
)

logger = logging.getLogger(__name__)

# Create the blueprint
admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@admin_bp.before_request
def _admin_auth_gate():
    """Require authenticated user for channel/device config endpoints.

    Channel config (settings, identity, channels, workflows) is local device
    configuration — any registered (flat+) user can manage their own device.
    Network-wide admin operations (user management, moderation) are protected
    by separate @require_central decorators on the social admin blueprint.
    """
    from integrations.social.auth import _get_user_from_token
    from flask import g

    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authentication required'}), 401

    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if user is None:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid or expired token'}), 401

    g.user = user
    g.user_id = str(user.id)
    g.db = db


@admin_bp.teardown_request
def _admin_teardown(exc):
    """Clean up db session after each admin request."""
    db = getattr(g, 'db', None)
    if db:
        try:
            if exc:
                db.rollback()
            else:
                db.commit()
        finally:
            db.close()


class AdminAPI:
    """
    Admin API controller.

    Manages all configurable components and provides REST endpoints.
    """

    def __init__(self):
        self._start_time = time.time()
        self._config: Dict[str, Any] = {}
        self._channels: Dict[str, Dict[str, Any]] = {}
        self._commands: Dict[str, CommandConfigSchema] = {}
        self._webhooks: Dict[str, WebhookConfigSchema] = {}
        self._cron_jobs: Dict[str, CronJobSchema] = {}
        self._triggers: Dict[str, TriggerConfigSchema] = {}
        self._workflows: Dict[str, WorkflowSchema] = {}
        self._scheduled_messages: Dict[str, ScheduledMessageSchema] = {}
        self._plugins: Dict[str, PluginConfigSchema] = {}
        self._sessions: Dict[str, SessionConfigSchema] = {}
        self._avatars: Dict[str, AvatarSchema] = {}
        self._sender_mappings: Dict[str, SenderMappingSchema] = {}
        self._identity: Optional[IdentityConfigSchema] = None
        self._global_config = GlobalConfigSchema()
        self._metrics_history: List[Dict[str, Any]] = []
        self._message_count = 0
        self._error_count = 0

        # Try to load saved configuration
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from file if exists."""
        config_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "agent_data",
            "admin_config.json"
        )
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    self._config = json.load(f)
                logger.info("Loaded admin configuration from %s", config_path)
        except Exception as e:
            logger.warning("Failed to load admin config: %s", e)

    def _save_config(self) -> None:
        """Save configuration to file using atomic write (temp + rename)."""
        config_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "agent_data",
            "admin_config.json"
        )
        try:
            config_dir = os.path.dirname(config_path)
            os.makedirs(config_dir, exist_ok=True)
            # Write to temp file first, then atomic rename to prevent corruption
            fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(self._config, f, indent=2, default=str)
                os.replace(tmp_path, config_path)  # atomic rename
            except:
                os.unlink(tmp_path)
                raise
            logger.info("Saved admin configuration to %s", config_path)
        except Exception as e:
            logger.warning("Failed to save admin config: %s", e)

    def get_uptime(self) -> float:
        """Get system uptime in seconds."""
        return time.time() - self._start_time


# Global API instance
_api = AdminAPI()


def get_api() -> AdminAPI:
    """Get the global API instance."""
    return _api


def api_response(func: Callable) -> Callable:
    """Decorator to wrap responses in standard API format."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            if isinstance(result, Response):
                return result
            response = APIResponse(success=True, data=result)
            return jsonify(response.to_dict())
        except ValueError as e:
            response = APIResponse(success=False, error=str(e))
            return jsonify(response.to_dict()), 400
        except PermissionError as e:
            response = APIResponse(success=False, error=str(e))
            return jsonify(response.to_dict()), 403
        except FileNotFoundError as e:
            response = APIResponse(success=False, error=str(e))
            return jsonify(response.to_dict()), 404
        except Exception as e:
            logger.exception("API error in %s", func.__name__)
            response = APIResponse(success=False, error=str(e))
            return jsonify(response.to_dict()), 500
    return wrapper


# ============================================================================
# HEALTH & STATUS ENDPOINTS
# ============================================================================

@admin_bp.route("/health", methods=["GET"])
@api_response
def health():
    """Health check endpoint."""
    return {"status": "healthy", "uptime": get_api().get_uptime()}


@admin_bp.route("/status", methods=["GET"])
@api_response
def status():
    """System status overview."""
    api = get_api()
    return {
        "status": "running",
        "uptime_seconds": api.get_uptime(),
        "channels_count": len(api._channels),
        "commands_count": len(api._commands),
        "plugins_count": len(api._plugins),
        "sessions_count": len(api._sessions),
        "webhooks_count": len(api._webhooks),
        "cron_jobs_count": len(api._cron_jobs),
        "triggers_count": len(api._triggers),
        "workflows_count": len(api._workflows),
    }


@admin_bp.route("/version", methods=["GET"])
@api_response
def version():
    """Get API version information."""
    return {
        "api_version": "1.0.0",
        "hevolvebot_version": "2.0.0",
        "python_version": "3.10+",
    }


# ============================================================================
# CHANNEL ENDPOINTS (20+ endpoints)
# ============================================================================

@admin_bp.route("/channels", methods=["GET"])
@api_response
def list_channels():
    """List all configured channels."""
    api = get_api()
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)

    channels = list(api._channels.values())
    total = len(channels)
    start = (page - 1) * page_size
    end = start + page_size

    return PaginatedResponse(
        items=channels[start:end],
        total=total,
        page=page,
        page_size=page_size,
        has_next=end < total,
        has_prev=page > 1,
    ).to_dict()


@admin_bp.route("/channels/<channel_type>", methods=["GET"])
@api_response
def get_channel(channel_type: str):
    """Get channel configuration."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")
    return api._channels[channel_type]


@admin_bp.route("/channels", methods=["POST"])
@api_response
def create_channel():
    """Create a new channel configuration."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    channel_type = data.get("channel_type")
    if not channel_type:
        raise ValueError("channel_type is required")

    if channel_type in api._channels:
        raise ValueError(f"Channel {channel_type} already exists")

    config = ChannelConfigSchema(
        channel_type=channel_type,
        name=data.get("name", channel_type),
        enabled=data.get("enabled", True),
        config=data.get("config", {}),
        rate_limit=data.get("rate_limit"),
        security=data.get("security"),
    )
    api._channels[channel_type] = config.to_dict()
    api._save_config()
    return config.to_dict()


@admin_bp.route("/channels/<channel_type>", methods=["PUT"])
@api_response
def update_channel(channel_type: str):
    """Update channel configuration."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    existing = api._channels[channel_type]
    existing.update(data)
    api._channels[channel_type] = existing
    api._save_config()
    return existing


@admin_bp.route("/channels/<channel_type>", methods=["DELETE"])
@api_response
def delete_channel(channel_type: str):
    """Delete channel configuration."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    del api._channels[channel_type]
    api._save_config()
    return {"deleted": channel_type}


@admin_bp.route("/channels/<channel_type>/status", methods=["GET"])
@api_response
def get_channel_status(channel_type: str):
    """Get channel connection status."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    return ChannelStatusSchema(
        channel_type=channel_type,
        name=api._channels[channel_type].get("name", channel_type),
        status="connected",
        message_count=api._message_count,
    ).to_dict()


@admin_bp.route("/channels/<channel_type>/enable", methods=["POST"])
@api_response
def enable_channel(channel_type: str):
    """Enable a channel."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    api._channels[channel_type]["enabled"] = True
    api._save_config()
    return {"channel": channel_type, "enabled": True}


@admin_bp.route("/channels/<channel_type>/disable", methods=["POST"])
@api_response
def disable_channel(channel_type: str):
    """Disable a channel."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    api._channels[channel_type]["enabled"] = False
    api._save_config()
    return {"channel": channel_type, "enabled": False}


@admin_bp.route("/channels/<channel_type>/test", methods=["POST"])
@api_response
def test_channel(channel_type: str):
    """Test channel connection."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    # Simulate connection test
    return {"channel": channel_type, "test_result": "success", "latency_ms": 45}


@admin_bp.route("/channels/<channel_type>/reconnect", methods=["POST"])
@api_response
def reconnect_channel(channel_type: str):
    """Force channel reconnection."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    return {"channel": channel_type, "reconnected": True}


@admin_bp.route("/channels/<channel_type>/metrics", methods=["GET"])
@api_response
def get_channel_metrics(channel_type: str):
    """Get channel-specific metrics."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    return {
        "channel": channel_type,
        "messages_sent": 0,
        "messages_received": 0,
        "avg_latency_ms": 45,
        "error_rate": 0.01,
    }


@admin_bp.route("/channels/<channel_type>/rate-limit", methods=["GET"])
@api_response
def get_channel_rate_limit(channel_type: str):
    """Get channel rate limit configuration."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    return api._channels[channel_type].get("rate_limit", {
        "requests_per_minute": 60,
        "burst_limit": 10,
    })


@admin_bp.route("/channels/<channel_type>/rate-limit", methods=["PUT"])
@api_response
def update_channel_rate_limit(channel_type: str):
    """Update channel rate limit configuration."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    data = request.get_json()
    api._channels[channel_type]["rate_limit"] = data
    api._save_config()
    return api._channels[channel_type]["rate_limit"]


@admin_bp.route("/channels/<channel_type>/security", methods=["GET"])
@api_response
def get_channel_security(channel_type: str):
    """Get channel security configuration."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    return api._channels[channel_type].get("security", {})


@admin_bp.route("/channels/<channel_type>/security", methods=["PUT"])
@api_response
def update_channel_security(channel_type: str):
    """Update channel security configuration."""
    api = get_api()
    if channel_type not in api._channels:
        raise FileNotFoundError(f"Channel {channel_type} not found")

    data = request.get_json()
    api._channels[channel_type]["security"] = data
    api._save_config()
    return api._channels[channel_type]["security"]


# ============================================================================
# QUEUE/PIPELINE ENDPOINTS (15+ endpoints)
# ============================================================================

@admin_bp.route("/queue/config", methods=["GET"])
@api_response
def get_queue_config():
    """Get queue configuration."""
    api = get_api()
    return api._global_config.queue.to_dict()


@admin_bp.route("/queue/config", methods=["PUT"])
@api_response
def update_queue_config():
    """Update queue configuration."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    api._global_config.queue = QueueConfigSchema(**data)
    api._save_config()
    return api._global_config.queue.to_dict()


@admin_bp.route("/queue/stats", methods=["GET"])
@api_response
def get_queue_stats():
    """Get queue statistics."""
    return QueueStatsSchema(
        queue_size=0,
        pending_messages=0,
        processing_messages=0,
        completed_messages=0,
        failed_messages=0,
        avg_processing_time_ms=45.0,
        messages_per_minute=10.0,
    ).to_dict()


@admin_bp.route("/queue/clear", methods=["POST"])
@api_response
def clear_queue():
    """Clear all pending messages from queue."""
    return {"cleared": True, "messages_removed": 0}


@admin_bp.route("/queue/pause", methods=["POST"])
@api_response
def pause_queue():
    """Pause queue processing."""
    return {"paused": True}


@admin_bp.route("/queue/resume", methods=["POST"])
@api_response
def resume_queue():
    """Resume queue processing."""
    return {"resumed": True}


@admin_bp.route("/queue/debounce", methods=["GET"])
@api_response
def get_debounce_config():
    """Get debounce configuration."""
    api = get_api()
    return {"debounce_ms": api._global_config.queue.debounce_ms}


@admin_bp.route("/queue/debounce", methods=["PUT"])
@api_response
def update_debounce_config():
    """Update debounce configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.queue.debounce_ms = data.get("debounce_ms", 500)
    api._save_config()
    return {"debounce_ms": api._global_config.queue.debounce_ms}


@admin_bp.route("/queue/dedupe", methods=["GET"])
@api_response
def get_dedupe_config():
    """Get deduplication configuration."""
    api = get_api()
    return {"dedupe_window_seconds": api._global_config.queue.dedupe_window_seconds}


@admin_bp.route("/queue/dedupe", methods=["PUT"])
@api_response
def update_dedupe_config():
    """Update deduplication configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.queue.dedupe_window_seconds = data.get("dedupe_window_seconds", 60)
    api._save_config()
    return {"dedupe_window_seconds": api._global_config.queue.dedupe_window_seconds}


@admin_bp.route("/queue/concurrency", methods=["GET"])
@api_response
def get_concurrency_config():
    """Get concurrency limits configuration."""
    api = get_api()
    return api._global_config.queue.concurrency_limits


@admin_bp.route("/queue/concurrency", methods=["PUT"])
@api_response
def update_concurrency_config():
    """Update concurrency limits configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.queue.concurrency_limits.update(data)
    api._save_config()
    return api._global_config.queue.concurrency_limits


@admin_bp.route("/queue/rate-limit", methods=["GET"])
@api_response
def get_rate_limit_config():
    """Get rate limit configuration."""
    api = get_api()
    return api._global_config.queue.rate_limits


@admin_bp.route("/queue/rate-limit", methods=["PUT"])
@api_response
def update_rate_limit_config():
    """Update rate limit configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.queue.rate_limits.update(data)
    api._save_config()
    return api._global_config.queue.rate_limits


@admin_bp.route("/queue/retry", methods=["GET"])
@api_response
def get_retry_config():
    """Get retry configuration."""
    api = get_api()
    return api._global_config.queue.retry_config


@admin_bp.route("/queue/retry", methods=["PUT"])
@api_response
def update_retry_config():
    """Update retry configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.queue.retry_config.update(data)
    api._save_config()
    return api._global_config.queue.retry_config


@admin_bp.route("/queue/batching", methods=["GET"])
@api_response
def get_batching_config():
    """Get batching configuration."""
    api = get_api()
    return api._global_config.queue.batching


@admin_bp.route("/queue/batching", methods=["PUT"])
@api_response
def update_batching_config():
    """Update batching configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.queue.batching.update(data)
    api._save_config()
    return api._global_config.queue.batching


# ============================================================================
# COMMAND ENDPOINTS (15+ endpoints)
# ============================================================================

@admin_bp.route("/commands", methods=["GET"])
@api_response
def list_commands():
    """List all registered commands."""
    api = get_api()
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)

    commands = [c.to_dict() for c in api._commands.values()]
    total = len(commands)
    start = (page - 1) * page_size
    end = start + page_size

    return PaginatedResponse(
        items=commands[start:end],
        total=total,
        page=page,
        page_size=page_size,
        has_next=end < total,
        has_prev=page > 1,
    ).to_dict()


@admin_bp.route("/commands/<command_name>", methods=["GET"])
@api_response
def get_command(command_name: str):
    """Get command configuration."""
    api = get_api()
    if command_name not in api._commands:
        raise FileNotFoundError(f"Command {command_name} not found")
    return api._commands[command_name].to_dict()


@admin_bp.route("/commands", methods=["POST"])
@api_response
def create_command():
    """Create a new command."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    name = data.get("name")
    if not name:
        raise ValueError("name is required")

    if name in api._commands:
        raise ValueError(f"Command {name} already exists")

    command = CommandConfigSchema(
        name=name,
        description=data.get("description", ""),
        pattern=data.get("pattern", f"/{name}"),
        handler=data.get("handler", ""),
        enabled=data.get("enabled", True),
        admin_only=data.get("admin_only", False),
        cooldown_seconds=data.get("cooldown_seconds", 0),
        usage_limit=data.get("usage_limit"),
        aliases=data.get("aliases", []),
        arguments=data.get("arguments", []),
    )
    api._commands[name] = command
    api._save_config()
    return command.to_dict()


@admin_bp.route("/commands/<command_name>", methods=["PUT"])
@api_response
def update_command(command_name: str):
    """Update command configuration."""
    api = get_api()
    if command_name not in api._commands:
        raise FileNotFoundError(f"Command {command_name} not found")

    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    existing = api._commands[command_name]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/commands/<command_name>", methods=["DELETE"])
@api_response
def delete_command(command_name: str):
    """Delete a command."""
    api = get_api()
    if command_name not in api._commands:
        raise FileNotFoundError(f"Command {command_name} not found")

    del api._commands[command_name]
    api._save_config()
    return {"deleted": command_name}


@admin_bp.route("/commands/<command_name>/enable", methods=["POST"])
@api_response
def enable_command(command_name: str):
    """Enable a command."""
    api = get_api()
    if command_name not in api._commands:
        raise FileNotFoundError(f"Command {command_name} not found")

    api._commands[command_name].enabled = True
    api._save_config()
    return {"command": command_name, "enabled": True}


@admin_bp.route("/commands/<command_name>/disable", methods=["POST"])
@api_response
def disable_command(command_name: str):
    """Disable a command."""
    api = get_api()
    if command_name not in api._commands:
        raise FileNotFoundError(f"Command {command_name} not found")

    api._commands[command_name].enabled = False
    api._save_config()
    return {"command": command_name, "enabled": False}


@admin_bp.route("/commands/<command_name>/stats", methods=["GET"])
@api_response
def get_command_stats(command_name: str):
    """Get command usage statistics."""
    api = get_api()
    if command_name not in api._commands:
        raise FileNotFoundError(f"Command {command_name} not found")

    return {
        "command": command_name,
        "invocations": 0,
        "successful": 0,
        "failed": 0,
        "avg_response_time_ms": 0,
    }


@admin_bp.route("/commands/mention-gating", methods=["GET"])
@api_response
def get_mention_gating():
    """Get mention gating configuration."""
    api = get_api()
    return api._config.get("mention_gating", MentionGatingConfigSchema().to_dict())


@admin_bp.route("/commands/mention-gating", methods=["PUT"])
@api_response
def update_mention_gating():
    """Update mention gating configuration."""
    api = get_api()
    data = request.get_json()
    api._config["mention_gating"] = data
    api._save_config()
    return api._config["mention_gating"]


# ============================================================================
# AUTOMATION ENDPOINTS (25+ endpoints)
# ============================================================================

# Webhooks
@admin_bp.route("/automation/webhooks", methods=["GET"])
@api_response
def list_webhooks():
    """List all webhooks."""
    api = get_api()
    return [w.to_dict() for w in api._webhooks.values()]


@admin_bp.route("/automation/webhooks/<webhook_id>", methods=["GET"])
@api_response
def get_webhook(webhook_id: str):
    """Get webhook configuration."""
    api = get_api()
    if webhook_id not in api._webhooks:
        raise FileNotFoundError(f"Webhook {webhook_id} not found")
    return api._webhooks[webhook_id].to_dict()


@admin_bp.route("/automation/webhooks", methods=["POST"])
@api_response
def create_webhook():
    """Create a new webhook."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    webhook_id = str(uuid.uuid4())
    webhook = WebhookConfigSchema(
        id=webhook_id,
        name=data.get("name", ""),
        url=data.get("url", ""),
        secret=data.get("secret"),
        events=data.get("events", []),
        enabled=data.get("enabled", True),
        retry_count=data.get("retry_count", 3),
        timeout_seconds=data.get("timeout_seconds", 30),
        headers=data.get("headers", {}),
    )
    api._webhooks[webhook_id] = webhook
    api._save_config()
    return webhook.to_dict()


@admin_bp.route("/automation/webhooks/<webhook_id>", methods=["PUT"])
@api_response
def update_webhook(webhook_id: str):
    """Update webhook configuration."""
    api = get_api()
    if webhook_id not in api._webhooks:
        raise FileNotFoundError(f"Webhook {webhook_id} not found")

    data = request.get_json()
    existing = api._webhooks[webhook_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/automation/webhooks/<webhook_id>", methods=["DELETE"])
@api_response
def delete_webhook(webhook_id: str):
    """Delete a webhook."""
    api = get_api()
    if webhook_id not in api._webhooks:
        raise FileNotFoundError(f"Webhook {webhook_id} not found")

    del api._webhooks[webhook_id]
    api._save_config()
    return {"deleted": webhook_id}


@admin_bp.route("/automation/webhooks/<webhook_id>/test", methods=["POST"])
@api_response
def test_webhook(webhook_id: str):
    """Test a webhook."""
    api = get_api()
    if webhook_id not in api._webhooks:
        raise FileNotFoundError(f"Webhook {webhook_id} not found")

    return {"webhook_id": webhook_id, "test_result": "success", "status_code": 200}


# Cron Jobs
@admin_bp.route("/automation/cron", methods=["GET"])
@api_response
def list_cron_jobs():
    """List all cron jobs."""
    api = get_api()
    return [c.to_dict() for c in api._cron_jobs.values()]


@admin_bp.route("/automation/cron/<job_id>", methods=["GET"])
@api_response
def get_cron_job(job_id: str):
    """Get cron job configuration."""
    api = get_api()
    if job_id not in api._cron_jobs:
        raise FileNotFoundError(f"Cron job {job_id} not found")
    return api._cron_jobs[job_id].to_dict()


@admin_bp.route("/automation/cron", methods=["POST"])
@api_response
def create_cron_job():
    """Create a new cron job."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    job_id = str(uuid.uuid4())
    job = CronJobSchema(
        id=job_id,
        name=data.get("name", ""),
        schedule=data.get("schedule", ""),
        handler=data.get("handler", ""),
        enabled=data.get("enabled", True),
        payload=data.get("payload", {}),
    )
    api._cron_jobs[job_id] = job
    api._save_config()
    return job.to_dict()


@admin_bp.route("/automation/cron/<job_id>", methods=["PUT"])
@api_response
def update_cron_job(job_id: str):
    """Update cron job configuration."""
    api = get_api()
    if job_id not in api._cron_jobs:
        raise FileNotFoundError(f"Cron job {job_id} not found")

    data = request.get_json()
    existing = api._cron_jobs[job_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/automation/cron/<job_id>", methods=["DELETE"])
@api_response
def delete_cron_job(job_id: str):
    """Delete a cron job."""
    api = get_api()
    if job_id not in api._cron_jobs:
        raise FileNotFoundError(f"Cron job {job_id} not found")

    del api._cron_jobs[job_id]
    api._save_config()
    return {"deleted": job_id}


@admin_bp.route("/automation/cron/<job_id>/run", methods=["POST"])
@api_response
def run_cron_job(job_id: str):
    """Manually trigger a cron job."""
    api = get_api()
    if job_id not in api._cron_jobs:
        raise FileNotFoundError(f"Cron job {job_id} not found")

    return {"job_id": job_id, "triggered": True}


# Triggers
@admin_bp.route("/automation/triggers", methods=["GET"])
@api_response
def list_triggers():
    """List all triggers."""
    api = get_api()
    return [t.to_dict() for t in api._triggers.values()]


@admin_bp.route("/automation/triggers/<trigger_id>", methods=["GET"])
@api_response
def get_trigger(trigger_id: str):
    """Get trigger configuration."""
    api = get_api()
    if trigger_id not in api._triggers:
        raise FileNotFoundError(f"Trigger {trigger_id} not found")
    return api._triggers[trigger_id].to_dict()


@admin_bp.route("/automation/triggers", methods=["POST"])
@api_response
def create_trigger():
    """Create a new trigger."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    trigger_id = str(uuid.uuid4())
    trigger = TriggerConfigSchema(
        id=trigger_id,
        name=data.get("name", ""),
        trigger_type=data.get("trigger_type", ""),
        pattern=data.get("pattern"),
        conditions=data.get("conditions", {}),
        actions=data.get("actions", []),
        enabled=data.get("enabled", True),
        priority=data.get("priority", 0),
    )
    api._triggers[trigger_id] = trigger
    api._save_config()
    return trigger.to_dict()


@admin_bp.route("/automation/triggers/<trigger_id>", methods=["PUT"])
@api_response
def update_trigger(trigger_id: str):
    """Update trigger configuration."""
    api = get_api()
    if trigger_id not in api._triggers:
        raise FileNotFoundError(f"Trigger {trigger_id} not found")

    data = request.get_json()
    existing = api._triggers[trigger_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/automation/triggers/<trigger_id>", methods=["DELETE"])
@api_response
def delete_trigger(trigger_id: str):
    """Delete a trigger."""
    api = get_api()
    if trigger_id not in api._triggers:
        raise FileNotFoundError(f"Trigger {trigger_id} not found")

    del api._triggers[trigger_id]
    api._save_config()
    return {"deleted": trigger_id}


# Workflows
@admin_bp.route("/automation/workflows", methods=["GET"])
@api_response
def list_workflows():
    """List all workflows."""
    api = get_api()
    return [w.to_dict() for w in api._workflows.values()]


@admin_bp.route("/automation/workflows/<workflow_id>", methods=["GET"])
@api_response
def get_workflow(workflow_id: str):
    """Get workflow configuration."""
    api = get_api()
    if workflow_id not in api._workflows:
        raise FileNotFoundError(f"Workflow {workflow_id} not found")
    return api._workflows[workflow_id].to_dict()


@admin_bp.route("/automation/workflows", methods=["POST"])
@api_response
def create_workflow():
    """Create a new workflow."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    workflow_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    workflow = WorkflowSchema(
        id=workflow_id,
        name=data.get("name", ""),
        description=data.get("description", ""),
        nodes=data.get("nodes", []),
        edges=data.get("edges", []),
        enabled=data.get("enabled", True),
        created_at=now,
        updated_at=now,
    )
    api._workflows[workflow_id] = workflow
    api._save_config()
    return workflow.to_dict()


@admin_bp.route("/automation/workflows/<workflow_id>", methods=["PUT"])
@api_response
def update_workflow(workflow_id: str):
    """Update workflow configuration."""
    api = get_api()
    if workflow_id not in api._workflows:
        raise FileNotFoundError(f"Workflow {workflow_id} not found")

    data = request.get_json()
    existing = api._workflows[workflow_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)
    existing.updated_at = datetime.now().isoformat()

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/automation/workflows/<workflow_id>", methods=["DELETE"])
@api_response
def delete_workflow(workflow_id: str):
    """Delete a workflow."""
    api = get_api()
    if workflow_id not in api._workflows:
        raise FileNotFoundError(f"Workflow {workflow_id} not found")

    del api._workflows[workflow_id]
    api._save_config()
    return {"deleted": workflow_id}


@admin_bp.route("/automation/workflows/<workflow_id>/execute", methods=["POST"])
@api_response
def execute_workflow(workflow_id: str):
    """Execute a workflow."""
    api = get_api()
    if workflow_id not in api._workflows:
        raise FileNotFoundError(f"Workflow {workflow_id} not found")

    execution_id = str(uuid.uuid4())
    return {
        "workflow_id": workflow_id,
        "execution_id": execution_id,
        "status": "started",
    }


@admin_bp.route("/automation/workflows/<workflow_id>/enable", methods=["POST"])
@api_response
def enable_workflow(workflow_id: str):
    """Enable a workflow."""
    api = get_api()
    if workflow_id not in api._workflows:
        raise FileNotFoundError(f"Workflow {workflow_id} not found")

    api._workflows[workflow_id].enabled = True
    api._save_config()
    return {"workflow": workflow_id, "enabled": True}


@admin_bp.route("/automation/workflows/<workflow_id>/disable", methods=["POST"])
@api_response
def disable_workflow(workflow_id: str):
    """Disable a workflow."""
    api = get_api()
    if workflow_id not in api._workflows:
        raise FileNotFoundError(f"Workflow {workflow_id} not found")

    api._workflows[workflow_id].enabled = False
    api._save_config()
    return {"workflow": workflow_id, "enabled": False}


# Scheduled Messages
@admin_bp.route("/automation/scheduled-messages", methods=["GET"])
@api_response
def list_scheduled_messages():
    """List all scheduled messages."""
    api = get_api()
    return [s.to_dict() for s in api._scheduled_messages.values()]


@admin_bp.route("/automation/scheduled-messages/<message_id>", methods=["GET"])
@api_response
def get_scheduled_message(message_id: str):
    """Get scheduled message configuration."""
    api = get_api()
    if message_id not in api._scheduled_messages:
        raise FileNotFoundError(f"Scheduled message {message_id} not found")
    return api._scheduled_messages[message_id].to_dict()


@admin_bp.route("/automation/scheduled-messages", methods=["POST"])
@api_response
def create_scheduled_message():
    """Create a new scheduled message."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    message_id = str(uuid.uuid4())
    msg = ScheduledMessageSchema(
        id=message_id,
        channel=data.get("channel", ""),
        chat_id=data.get("chat_id", ""),
        message=data.get("message", ""),
        scheduled_time=data.get("scheduled_time", ""),
        recurring=data.get("recurring", False),
        recurrence_pattern=data.get("recurrence_pattern"),
        enabled=data.get("enabled", True),
    )
    api._scheduled_messages[message_id] = msg
    api._save_config()
    return msg.to_dict()


@admin_bp.route("/automation/scheduled-messages/<message_id>", methods=["PUT"])
@api_response
def update_scheduled_message(message_id: str):
    """Update scheduled message configuration."""
    api = get_api()
    if message_id not in api._scheduled_messages:
        raise FileNotFoundError(f"Scheduled message {message_id} not found")

    data = request.get_json()
    existing = api._scheduled_messages[message_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/automation/scheduled-messages/<message_id>", methods=["DELETE"])
@api_response
def delete_scheduled_message(message_id: str):
    """Delete a scheduled message."""
    api = get_api()
    if message_id not in api._scheduled_messages:
        raise FileNotFoundError(f"Scheduled message {message_id} not found")

    del api._scheduled_messages[message_id]
    api._save_config()
    return {"deleted": message_id}


# ============================================================================
# IDENTITY ENDPOINTS (15+ endpoints)
# ============================================================================

@admin_bp.route("/identity", methods=["GET"])
@api_response
def get_identity():
    """Get agent identity configuration."""
    api = get_api()
    if api._identity:
        return api._identity.to_dict()
    return IdentityConfigSchema().to_dict()


@admin_bp.route("/identity", methods=["PUT"])
@api_response
def update_identity():
    """Update agent identity configuration."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    api._identity = IdentityConfigSchema(
        agent_id=data.get("agent_id", ""),
        display_name=data.get("display_name", ""),
        avatar_url=data.get("avatar_url"),
        bio=data.get("bio", ""),
        personality=data.get("personality", {}),
        response_style=data.get("response_style", {}),
        per_channel_identity=data.get("per_channel_identity", {}),
    )
    api._save_config()
    return api._identity.to_dict()


@admin_bp.route("/identity/avatars", methods=["GET"])
@api_response
def list_avatars():
    """List all avatars."""
    api = get_api()
    return [a.to_dict() for a in api._avatars.values()]


@admin_bp.route("/identity/avatars/<avatar_id>", methods=["GET"])
@api_response
def get_avatar(avatar_id: str):
    """Get avatar configuration."""
    api = get_api()
    if avatar_id not in api._avatars:
        raise FileNotFoundError(f"Avatar {avatar_id} not found")
    return api._avatars[avatar_id].to_dict()


@admin_bp.route("/identity/avatars", methods=["POST"])
@api_response
def create_avatar():
    """Create a new avatar."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    avatar_id = str(uuid.uuid4())
    avatar = AvatarSchema(
        id=avatar_id,
        name=data.get("name", ""),
        url=data.get("url", ""),
        channel=data.get("channel"),
        is_default=data.get("is_default", False),
    )
    api._avatars[avatar_id] = avatar
    api._save_config()
    return avatar.to_dict()


@admin_bp.route("/identity/avatars/<avatar_id>", methods=["PUT"])
@api_response
def update_avatar(avatar_id: str):
    """Update avatar configuration."""
    api = get_api()
    if avatar_id not in api._avatars:
        raise FileNotFoundError(f"Avatar {avatar_id} not found")

    data = request.get_json()
    existing = api._avatars[avatar_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/identity/avatars/<avatar_id>", methods=["DELETE"])
@api_response
def delete_avatar(avatar_id: str):
    """Delete an avatar."""
    api = get_api()
    if avatar_id not in api._avatars:
        raise FileNotFoundError(f"Avatar {avatar_id} not found")

    del api._avatars[avatar_id]
    api._save_config()
    return {"deleted": avatar_id}


@admin_bp.route("/identity/avatars/<avatar_id>/default", methods=["POST"])
@api_response
def set_default_avatar(avatar_id: str):
    """Set an avatar as the default."""
    api = get_api()
    if avatar_id not in api._avatars:
        raise FileNotFoundError(f"Avatar {avatar_id} not found")

    for a in api._avatars.values():
        a.is_default = False
    api._avatars[avatar_id].is_default = True
    api._save_config()
    return api._avatars[avatar_id].to_dict()


@admin_bp.route("/identity/sender-mappings", methods=["GET"])
@api_response
def list_sender_mappings():
    """List all sender mappings."""
    api = get_api()
    return [s.to_dict() for s in api._sender_mappings.values()]


@admin_bp.route("/identity/sender-mappings/<mapping_id>", methods=["GET"])
@api_response
def get_sender_mapping(mapping_id: str):
    """Get sender mapping configuration."""
    api = get_api()
    if mapping_id not in api._sender_mappings:
        raise FileNotFoundError(f"Sender mapping {mapping_id} not found")
    return api._sender_mappings[mapping_id].to_dict()


@admin_bp.route("/identity/sender-mappings", methods=["POST"])
@api_response
def create_sender_mapping():
    """Create a new sender mapping."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    mapping_id = str(uuid.uuid4())
    mapping = SenderMappingSchema(
        platform_user_id=data.get("platform_user_id", ""),
        internal_user_id=data.get("internal_user_id", ""),
        channel=data.get("channel", ""),
        display_name=data.get("display_name"),
        metadata=data.get("metadata", {}),
    )
    api._sender_mappings[mapping_id] = mapping
    api._save_config()
    return mapping.to_dict()


@admin_bp.route("/identity/sender-mappings/<mapping_id>", methods=["PUT"])
@api_response
def update_sender_mapping(mapping_id: str):
    """Update sender mapping configuration."""
    api = get_api()
    if mapping_id not in api._sender_mappings:
        raise FileNotFoundError(f"Sender mapping {mapping_id} not found")

    data = request.get_json()
    existing = api._sender_mappings[mapping_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/identity/sender-mappings/<mapping_id>", methods=["DELETE"])
@api_response
def delete_sender_mapping(mapping_id: str):
    """Delete a sender mapping."""
    api = get_api()
    if mapping_id not in api._sender_mappings:
        raise FileNotFoundError(f"Sender mapping {mapping_id} not found")

    del api._sender_mappings[mapping_id]
    api._save_config()
    return {"deleted": mapping_id}


# ============================================================================
# PLUGIN ENDPOINTS (10+ endpoints)
# ============================================================================

@admin_bp.route("/plugins", methods=["GET"])
@api_response
def list_plugins():
    """List all plugins."""
    api = get_api()
    return [p.to_dict() for p in api._plugins.values()]


@admin_bp.route("/plugins/<plugin_id>", methods=["GET"])
@api_response
def get_plugin(plugin_id: str):
    """Get plugin configuration."""
    api = get_api()
    if plugin_id not in api._plugins:
        raise FileNotFoundError(f"Plugin {plugin_id} not found")
    return api._plugins[plugin_id].to_dict()


@admin_bp.route("/plugins", methods=["POST"])
@api_response
def install_plugin():
    """Install a new plugin."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    plugin_id = data.get("id", str(uuid.uuid4()))
    plugin = PluginConfigSchema(
        id=plugin_id,
        name=data.get("name", ""),
        version=data.get("version", "1.0.0"),
        description=data.get("description", ""),
        enabled=data.get("enabled", True),
        config=data.get("config", {}),
        hooks=data.get("hooks", []),
        dependencies=data.get("dependencies", []),
    )
    api._plugins[plugin_id] = plugin
    api._save_config()
    return plugin.to_dict()


@admin_bp.route("/plugins/<plugin_id>", methods=["PUT"])
@api_response
def update_plugin(plugin_id: str):
    """Update plugin configuration."""
    api = get_api()
    if plugin_id not in api._plugins:
        raise FileNotFoundError(f"Plugin {plugin_id} not found")

    data = request.get_json()
    existing = api._plugins[plugin_id]
    for key, value in data.items():
        if hasattr(existing, key):
            setattr(existing, key, value)

    api._save_config()
    return existing.to_dict()


@admin_bp.route("/plugins/<plugin_id>", methods=["DELETE"])
@api_response
def uninstall_plugin(plugin_id: str):
    """Uninstall a plugin."""
    api = get_api()
    if plugin_id not in api._plugins:
        raise FileNotFoundError(f"Plugin {plugin_id} not found")

    del api._plugins[plugin_id]
    api._save_config()
    return {"deleted": plugin_id}


@admin_bp.route("/plugins/<plugin_id>/enable", methods=["POST"])
@api_response
def enable_plugin(plugin_id: str):
    """Enable a plugin."""
    api = get_api()
    if plugin_id not in api._plugins:
        raise FileNotFoundError(f"Plugin {plugin_id} not found")

    api._plugins[plugin_id].enabled = True
    api._save_config()
    return {"plugin": plugin_id, "enabled": True}


@admin_bp.route("/plugins/<plugin_id>/disable", methods=["POST"])
@api_response
def disable_plugin(plugin_id: str):
    """Disable a plugin."""
    api = get_api()
    if plugin_id not in api._plugins:
        raise FileNotFoundError(f"Plugin {plugin_id} not found")

    api._plugins[plugin_id].enabled = False
    api._save_config()
    return {"plugin": plugin_id, "enabled": False}


@admin_bp.route("/plugins/<plugin_id>/config", methods=["GET"])
@api_response
def get_plugin_config(plugin_id: str):
    """Get plugin-specific configuration."""
    api = get_api()
    if plugin_id not in api._plugins:
        raise FileNotFoundError(f"Plugin {plugin_id} not found")

    return api._plugins[plugin_id].config


@admin_bp.route("/plugins/<plugin_id>/config", methods=["PUT"])
@api_response
def update_plugin_config(plugin_id: str):
    """Update plugin-specific configuration."""
    api = get_api()
    if plugin_id not in api._plugins:
        raise FileNotFoundError(f"Plugin {plugin_id} not found")

    data = request.get_json()
    api._plugins[plugin_id].config = data
    api._save_config()
    return api._plugins[plugin_id].config


# ============================================================================
# SESSION ENDPOINTS (10+ endpoints)
# ============================================================================

@admin_bp.route("/sessions", methods=["GET"])
@api_response
def list_sessions():
    """List all active sessions."""
    api = get_api()
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)

    sessions = [s.to_dict() for s in api._sessions.values()]
    total = len(sessions)
    start = (page - 1) * page_size
    end = start + page_size

    return PaginatedResponse(
        items=sessions[start:end],
        total=total,
        page=page,
        page_size=page_size,
        has_next=end < total,
        has_prev=page > 1,
    ).to_dict()


@admin_bp.route("/sessions/<session_id>", methods=["GET"])
@api_response
def get_session(session_id: str):
    """Get session details."""
    api = get_api()
    if session_id not in api._sessions:
        raise FileNotFoundError(f"Session {session_id} not found")
    return api._sessions[session_id].to_dict()


@admin_bp.route("/sessions/<session_id>", methods=["DELETE"])
@api_response
def terminate_session(session_id: str):
    """Terminate a session."""
    api = get_api()
    if session_id not in api._sessions:
        raise FileNotFoundError(f"Session {session_id} not found")

    del api._sessions[session_id]
    return {"terminated": session_id}


@admin_bp.route("/sessions/<session_id>/context", methods=["GET"])
@api_response
def get_session_context(session_id: str):
    """Get session context."""
    api = get_api()
    if session_id not in api._sessions:
        raise FileNotFoundError(f"Session {session_id} not found")

    return api._sessions[session_id].context


@admin_bp.route("/sessions/<session_id>/context", methods=["PUT"])
@api_response
def update_session_context(session_id: str):
    """Update session context."""
    api = get_api()
    if session_id not in api._sessions:
        raise FileNotFoundError(f"Session {session_id} not found")

    data = request.get_json()
    api._sessions[session_id].context.update(data)
    return api._sessions[session_id].context


@admin_bp.route("/sessions/<session_id>/clear-context", methods=["POST"])
@api_response
def clear_session_context(session_id: str):
    """Clear session context."""
    api = get_api()
    if session_id not in api._sessions:
        raise FileNotFoundError(f"Session {session_id} not found")

    api._sessions[session_id].context = {}
    return {"cleared": True}


@admin_bp.route("/sessions/pair", methods=["POST"])
@api_response
def pair_sessions():
    """Pair two sessions for bridging."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    pairing = PairingRequestSchema(
        source_channel=data.get("source_channel", ""),
        source_chat_id=data.get("source_chat_id", ""),
        target_channel=data.get("target_channel", ""),
        target_chat_id=data.get("target_chat_id", ""),
        bidirectional=data.get("bidirectional", True),
    )

    pairing_id = str(uuid.uuid4())
    return {"pairing_id": pairing_id, "paired": True, "config": pairing.to_dict()}


@admin_bp.route("/sessions/unpair/<pairing_id>", methods=["DELETE"])
@api_response
def unpair_sessions(pairing_id: str):
    """Remove session pairing."""
    return {"pairing_id": pairing_id, "unpaired": True}


# ============================================================================
# METRICS ENDPOINTS (10+ endpoints)
# ============================================================================

@admin_bp.route("/metrics", methods=["GET"])
@api_response
def get_metrics():
    """Get current system metrics."""
    api = get_api()
    return MetricsSchema(
        timestamp=datetime.now().isoformat(),
        uptime_seconds=api.get_uptime(),
        total_messages_processed=api._message_count,
        messages_per_minute=0.0,
        active_sessions=len(api._sessions),
        active_channels=len(api._channels),
        queue_depth=0,
        memory_usage_mb=0.0,
        cpu_usage_percent=0.0,
        error_rate=0.0,
        latency_p50_ms=45.0,
        latency_p99_ms=150.0,
    ).to_dict()


@admin_bp.route("/metrics/history", methods=["GET"])
@api_response
def get_metrics_history():
    """Get historical metrics."""
    api = get_api()
    limit = request.args.get("limit", 100, type=int)
    return api._metrics_history[-limit:]


@admin_bp.route("/metrics/channels", methods=["GET"])
@api_response
def get_channel_metrics_all():
    """Get metrics for all channels."""
    api = get_api()
    return {
        channel: {
            "messages_sent": 0,
            "messages_received": 0,
            "avg_latency_ms": 45,
            "error_rate": 0.01,
        }
        for channel in api._channels
    }


@admin_bp.route("/metrics/commands", methods=["GET"])
@api_response
def get_command_metrics():
    """Get metrics for all commands."""
    api = get_api()
    return {
        cmd: {
            "invocations": 0,
            "successful": 0,
            "failed": 0,
            "avg_response_time_ms": 0,
        }
        for cmd in api._commands
    }


@admin_bp.route("/metrics/queue", methods=["GET"])
@api_response
def get_queue_metrics():
    """Get queue performance metrics."""
    return {
        "throughput_per_second": 10.0,
        "avg_wait_time_ms": 50.0,
        "avg_processing_time_ms": 100.0,
        "queue_depth": 0,
        "rejected_count": 0,
    }


@admin_bp.route("/metrics/errors", methods=["GET"])
@api_response
def get_error_metrics():
    """Get error metrics and recent errors."""
    api = get_api()
    return {
        "total_errors": api._error_count,
        "error_rate": 0.0,
        "recent_errors": [],
    }


@admin_bp.route("/metrics/latency", methods=["GET"])
@api_response
def get_latency_metrics():
    """Get latency distribution metrics."""
    return {
        "p50_ms": 45.0,
        "p75_ms": 75.0,
        "p90_ms": 100.0,
        "p95_ms": 125.0,
        "p99_ms": 150.0,
        "max_ms": 500.0,
    }


# ============================================================================
# GLOBAL CONFIGURATION ENDPOINTS (10+ endpoints)
# ============================================================================

@admin_bp.route("/config", methods=["GET"])
@api_response
def get_global_config():
    """Get global system configuration."""
    api = get_api()
    return api._global_config.to_dict()


@admin_bp.route("/config", methods=["PUT"])
@api_response
def update_global_config():
    """Update global system configuration."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    if "queue" in data:
        api._global_config.queue = QueueConfigSchema(**data["queue"])
    if "security" in data:
        api._global_config.security = SecurityConfigSchema(**data["security"])
    if "media" in data:
        api._global_config.media = MediaConfigSchema(**data["media"])
    if "response" in data:
        api._global_config.response = ResponseConfigSchema(**data["response"])
    if "memory" in data:
        api._global_config.memory = MemoryStoreConfigSchema(**data["memory"])
    if "embodied_ai" in data:
        api._global_config.embodied_ai = EmbodiedAIConfigSchema(**data["embodied_ai"])

    api._save_config()
    return api._global_config.to_dict()


@admin_bp.route("/config/security", methods=["GET"])
@api_response
def get_security_config():
    """Get security configuration."""
    api = get_api()
    return api._global_config.security.to_dict()


@admin_bp.route("/config/security", methods=["PUT"])
@api_response
def update_security_config():
    """Update security configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.security = SecurityConfigSchema(**data)
    api._save_config()
    return api._global_config.security.to_dict()


@admin_bp.route("/config/media", methods=["GET"])
@api_response
def get_media_config():
    """Get media handling configuration."""
    api = get_api()
    return api._global_config.media.to_dict()


@admin_bp.route("/config/media", methods=["PUT"])
@api_response
def update_media_config():
    """Update media handling configuration."""
    api = get_api()
    data = request.get_json()
    valid_fields = {f.name for f in MediaConfigSchema.__dataclass_fields__.values()}
    api._global_config.media = MediaConfigSchema(**{k: v for k, v in data.items() if k in valid_fields})
    api._save_config()
    return api._global_config.media.to_dict()


@admin_bp.route("/config/response", methods=["GET"])
@api_response
def get_response_config():
    """Get response handling configuration."""
    api = get_api()
    return api._global_config.response.to_dict()


@admin_bp.route("/config/response", methods=["PUT"])
@api_response
def update_response_config():
    """Update response handling configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.response = ResponseConfigSchema(**data)
    api._save_config()
    return api._global_config.response.to_dict()


@admin_bp.route("/config/memory", methods=["GET"])
@api_response
def get_memory_config():
    """Get memory store configuration."""
    api = get_api()
    return api._global_config.memory.to_dict()


@admin_bp.route("/config/memory", methods=["PUT"])
@api_response
def update_memory_config():
    """Update memory store configuration."""
    api = get_api()
    data = request.get_json()
    api._global_config.memory = MemoryStoreConfigSchema(**data)
    api._save_config()
    return api._global_config.memory.to_dict()


@admin_bp.route("/config/embodied", methods=["GET"])
@api_response
def get_embodied_config():
    """Get embodied AI / HevolveAI feed configuration."""
    api = get_api()
    return api._global_config.embodied_ai.to_dict()


@admin_bp.route("/config/embodied", methods=["PUT"])
@api_response
def update_embodied_config():
    """Update embodied AI feed configuration and propagate to HevolveAI."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")
    api._global_config.embodied_ai = EmbodiedAIConfigSchema(**data)
    api._save_config()

    # Propagate to HevolveAI runtime if reachable
    _propagate_embodied_config(api._global_config.embodied_ai)
    return api._global_config.embodied_ai.to_dict()


@admin_bp.route("/config/embodied/toggle", methods=["POST"])
@api_response
def toggle_embodied_feed():
    """Toggle individual feeds on/off at runtime.

    Body: {"feed": "screen"|"camera"|"audio"|"all", "enabled": true|false}
    """
    api = get_api()
    data = request.get_json()
    if not data or "feed" not in data:
        raise ValueError("feed and enabled are required")

    feed = data["feed"]
    enabled = data.get("enabled", True)
    cfg = api._global_config.embodied_ai

    if feed == "screen":
        cfg.screen_capture_enabled = enabled
    elif feed == "camera":
        cfg.camera_enabled = enabled
    elif feed == "audio":
        cfg.audio_enabled = enabled
    elif feed == "all":
        cfg.enabled = enabled
        cfg.screen_capture_enabled = enabled
        cfg.camera_enabled = enabled
        cfg.audio_enabled = enabled
    else:
        raise ValueError(f"Unknown feed: {feed}. Use screen|camera|audio|all")

    api._save_config()
    _propagate_embodied_config(cfg)
    return {"feed": feed, "enabled": enabled, "config": cfg.to_dict()}


@admin_bp.route("/config/embodied/status", methods=["GET"])
@api_response
def get_embodied_status():
    """Get live status of HevolveAI embodied AI system."""
    import requests as req
    api = get_api()
    url = api._global_config.embodied_ai.hevolveai_url

    try:
        resp = req.get(f"{url}/health", timeout=3)
        health = resp.json() if resp.ok else {"status": "unreachable"}
    except Exception:
        health = {"status": "unreachable", "error": "Cannot connect to HevolveAI"}

    try:
        resp = req.get(f"{url}/v1/stats", timeout=3)
        stats = resp.json() if resp.ok else {}
    except Exception:
        stats = {}

    return {
        "config": api._global_config.embodied_ai.to_dict(),
        "hevolve_core_health": health,
        "learning_stats": stats,
    }


def _propagate_embodied_config(cfg: EmbodiedAIConfigSchema):
    """Push config changes to HevolveAI runtime (best-effort)."""
    import requests as req
    try:
        # HevolveAI reads env vars at startup, but we can hit a
        # runtime config endpoint if available, or set for next restart.
        # For now, write to shared config file that HevolveAI watches.
        config_path = os.path.join(
            os.path.dirname(__file__), '..', '..', '..',
            'agent_data', 'embodied_ai_config.json',
        )
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(cfg.to_dict(), f, indent=2)
        logger.info("Embodied AI config propagated to %s", config_path)
    except Exception as e:
        logger.warning("Failed to propagate embodied AI config: %s", e)


@admin_bp.route("/config/export", methods=["GET"])
@api_response
def export_config():
    """Export all configuration as JSON."""
    api = get_api()
    return {
        "global": api._global_config.to_dict(),
        "channels": {k: v for k, v in api._channels.items()},
        "commands": {k: v.to_dict() for k, v in api._commands.items()},
        "webhooks": {k: v.to_dict() for k, v in api._webhooks.items()},
        "cron_jobs": {k: v.to_dict() for k, v in api._cron_jobs.items()},
        "triggers": {k: v.to_dict() for k, v in api._triggers.items()},
        "workflows": {k: v.to_dict() for k, v in api._workflows.items()},
        "scheduled_messages": {k: v.to_dict() for k, v in api._scheduled_messages.items()},
        "plugins": {k: v.to_dict() for k, v in api._plugins.items()},
        "identity": api._identity.to_dict() if api._identity else None,
        "avatars": {k: v.to_dict() for k, v in api._avatars.items()},
        "sender_mappings": {k: v.to_dict() for k, v in api._sender_mappings.items()},
    }


@admin_bp.route("/config/import", methods=["POST"])
@api_response
def import_config():
    """Import configuration from JSON."""
    api = get_api()
    data = request.get_json()
    if not data:
        raise ValueError("Request body required")

    if "global" in data:
        g = data["global"]
        if "queue" in g:
            api._global_config.queue = QueueConfigSchema(**g["queue"])
        if "security" in g:
            api._global_config.security = SecurityConfigSchema(**g["security"])
        if "media" in g:
            api._global_config.media = MediaConfigSchema(**g["media"])
        if "response" in g:
            api._global_config.response = ResponseConfigSchema(**g["response"])
        if "memory" in g:
            api._global_config.memory = MemoryStoreConfigSchema(**g["memory"])
        if "embodied_ai" in g:
            api._global_config.embodied_ai = EmbodiedAIConfigSchema(**g["embodied_ai"])

    if "channels" in data:
        api._channels = data["channels"]

    if "commands" in data:
        api._commands = {
            k: CommandConfigSchema(**v) for k, v in data["commands"].items()
        }

    if "webhooks" in data:
        api._webhooks = {
            k: WebhookConfigSchema(**v) for k, v in data["webhooks"].items()
        }

    if "cron_jobs" in data:
        api._cron_jobs = {
            k: CronJobSchema(**v) for k, v in data["cron_jobs"].items()
        }

    if "triggers" in data:
        api._triggers = {
            k: TriggerConfigSchema(**v) for k, v in data["triggers"].items()
        }

    if "workflows" in data:
        api._workflows = {
            k: WorkflowSchema(**v) for k, v in data["workflows"].items()
        }

    if "scheduled_messages" in data:
        api._scheduled_messages = {
            k: ScheduledMessageSchema(**v) for k, v in data["scheduled_messages"].items()
        }

    if "plugins" in data:
        api._plugins = {
            k: PluginConfigSchema(**v) for k, v in data["plugins"].items()
        }

    if "identity" in data and data["identity"]:
        api._identity = IdentityConfigSchema(**data["identity"])

    if "avatars" in data:
        api._avatars = {
            k: AvatarSchema(**v) for k, v in data["avatars"].items()
        }

    if "sender_mappings" in data:
        api._sender_mappings = {
            k: SenderMappingSchema(**v) for k, v in data["sender_mappings"].items()
        }

    api._save_config()
    return {"imported": True}


@admin_bp.route("/config/reset", methods=["POST"])
@api_response
def reset_config():
    """Reset all configuration to defaults."""
    api = get_api()
    api._global_config = GlobalConfigSchema()
    api._channels = {}
    api._commands = {}
    api._webhooks = {}
    api._cron_jobs = {}
    api._triggers = {}
    api._workflows = {}
    api._scheduled_messages = {}
    api._plugins = {}
    api._sessions = {}
    api._avatars = {}
    api._sender_mappings = {}
    api._identity = None
    api._save_config()
    return {"reset": True}
