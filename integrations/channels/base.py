"""
Base Channel Adapter Interface

Defines the contract for all messaging channel adapters.
Ported from HevolveBot's ChannelMessagingAdapter pattern.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import Callable, Optional, List, Dict, Any, Union
import asyncio
import inspect
import logging
import traceback

logger = logging.getLogger(__name__)


# Methods on a ChannelAdapter subclass that should have their escaping
# exceptions auto-recorded to the metrics counter + dashboard log +
# (for critical classes) the setup_progress SSE card.  Adding a name
# here picks it up for every adapter on next class-load — no per-
# adapter wiring needed.  Methods not on this list pass through
# untouched (so internal helpers can raise without triggering the
# sink).
_AUTO_RECORD_METHODS = (
    'connect', 'disconnect',
    'send_message', 'edit_message', 'delete_message', 'send_typing',
    'get_chat_info', 'download_file',
)

# Sentinel marking a method as already-wrapped — keeps the wrap
# idempotent if __init_subclass__ runs more than once (test reloads,
# decorator stacks, etc.).
_AUTO_RECORD_WRAPPED_ATTR = '__channel_error_wrapped__'


class MessageType(Enum):
    """Type of message content."""
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    LOCATION = "location"
    CONTACT = "contact"
    STICKER = "sticker"
    VOICE = "voice"


class ChannelStatus(Enum):
    """Channel connection status."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"


@dataclass
class MediaAttachment:
    """Media attachment in a message."""
    type: MessageType
    url: Optional[str] = None
    file_path: Optional[str] = None
    file_id: Optional[str] = None  # Platform-specific file ID
    mime_type: Optional[str] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    caption: Optional[str] = None


@dataclass
class Message:
    """Unified message format across all channels."""
    id: str
    channel: str  # telegram, discord, slack, etc.
    sender_id: str
    sender_name: Optional[str] = None
    chat_id: str = ""  # Group/channel ID or same as sender for DMs
    text: Optional[str] = None
    media: List[MediaAttachment] = field(default_factory=list)
    reply_to_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    is_group: bool = False
    is_bot_mentioned: bool = False
    raw: Optional[Dict[str, Any]] = None  # Original platform message

    @property
    def has_media(self) -> bool:
        return len(self.media) > 0

    @property
    def content(self) -> str:
        """Get text content or media caption."""
        if self.text:
            return self.text
        for m in self.media:
            if m.caption:
                return m.caption
        return ""


@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class ChannelConfig:
    """Configuration for a channel adapter."""
    enabled: bool = True
    token: Optional[str] = None
    webhook_url: Optional[str] = None
    dm_policy: str = "pairing"  # pairing, open, closed
    allow_from: List[str] = field(default_factory=list)
    require_mention_in_groups: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


# ─── Inbound feed ingestion gate (Phase 1 omni-channel, 2026-05-30) ──
#
# Which channels' GROUP/public posts get mirrored into the Nunba social
# feed ("posts from other channels auto-created in Nunba").  Controlled
# by HEVOLVE_INGEST_CHANNELS (comma list of channel types, or '*' for
# all).  Default EMPTY = ingest nothing — the operator opts in per
# channel.  Combined with the hard is_group gate below, private 1:1 DMs
# are NEVER mirrored into the public feed regardless of this setting.
_ingest_channels_cache = None


def _channels_opted_into_ingest() -> set:
    global _ingest_channels_cache
    if _ingest_channels_cache is None:
        import os
        raw = os.environ.get('HEVOLVE_INGEST_CHANNELS', '')
        _ingest_channels_cache = {
            c.strip().lower() for c in raw.split(',') if c.strip()}
    return _ingest_channels_cache


class ChannelAdapter(ABC):
    """
    Base class for all channel adapters.

    Implements the adapter pattern for unified messaging across platforms.
    Each platform (Telegram, Discord, etc.) extends this class.
    """

    def __init__(self, config: ChannelConfig):
        self.config = config
        self.status = ChannelStatus.DISCONNECTED
        self._message_handlers: List[Callable] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Channel name identifier."""
        pass

    @abstractmethod
    async def connect(self) -> bool:
        """
        Connect to the messaging platform.
        Returns True if connection successful.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the platform."""
        pass

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Send a message to a chat.

        Args:
            chat_id: Target chat/user ID
            text: Message text
            reply_to: Message ID to reply to
            media: Media attachments
            buttons: Interactive buttons/keyboard

        Returns:
            SendResult with success status and message ID
        """
        pass

    @abstractmethod
    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing message."""
        pass

    @abstractmethod
    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> bool:
        """Delete a message."""
        pass

    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        pass

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a chat."""
        pass

    def on_message(self, handler: Callable[[Message], Any]) -> None:
        """
        Register a message handler.

        Handler will be called for each incoming message.
        """
        self._message_handlers.append(handler)

    async def _dispatch_message(self, message: Message) -> None:
        """Dispatch message to all registered handlers."""
        for handler in self._message_handlers:
            try:
                result = handler(message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Error in message handler: {e}")
        # Inbound bridge: mirror opted-in channels' GROUP posts into the
        # Nunba social feed.  Independent of the agent handlers above —
        # an ingest failure must not affect the agent reply, and vice
        # versa.
        self._maybe_ingest_to_feed(message)

    def _maybe_ingest_to_feed(self, message: Message) -> None:
        """Mirror an inbound channel post into the Nunba social feed via
        the canonical cross_channel.ingest_channel_message (which dedups
        by source_channel+source_message_id).  Triple-gated for privacy:
          1. is_group — NEVER ingest a private 1:1 DM into the public feed
          2. the channel must be opted in (HEVOLVE_INGEST_CHANNELS)
          3. there must be content
        Runs on a daemon thread because ingest does synchronous DB I/O
        that must not block this adapter's async event loop.  Best-effort.
        """
        try:
            if not getattr(message, 'is_group', False):
                return  # privacy: DMs are never mirrored to the public feed
            allow = _channels_opted_into_ingest()
            chan = (message.channel or '').lower()
            if '*' not in allow and chan not in allow:
                return
            content = message.content
            if not content:
                return
            media_urls = [getattr(m, 'url', None) for m in (message.media or [])]
            media_urls = [u for u in media_urls if u] or None
            sender = message.sender_name or message.sender_id

            def _run():
                try:
                    from integrations.social.cross_channel import (
                        ingest_channel_message)
                    ingest_channel_message(
                        message.channel, sender, content,
                        message_id=message.id, media_urls=media_urls)
                except Exception as e:
                    logger.debug("inbound feed ingest skipped: %s", e)

            import threading
            threading.Thread(
                target=_run, daemon=True, name='channel-feed-ingest').start()
        except Exception as e:
            logger.debug("inbound ingest gate error: %s", e)

    def get_status(self) -> ChannelStatus:
        """Get current connection status."""
        return self.status

    async def start(self) -> None:
        """Start the channel adapter (begin receiving messages)."""
        if self._running:
            return

        self._running = True
        connected = await self.connect()

        if connected:
            self.status = ChannelStatus.CONNECTED
            logger.info(f"{self.name} channel connected")
        else:
            self.status = ChannelStatus.ERROR
            logger.error(f"{self.name} channel failed to connect")

    async def stop(self) -> None:
        """Stop the channel adapter."""
        self._running = False
        await self.disconnect()
        self.status = ChannelStatus.DISCONNECTED
        logger.info(f"{self.name} channel disconnected")

    def is_running(self) -> bool:
        """Check if adapter is running."""
        return self._running and self.status == ChannelStatus.CONNECTED

    # ─── Canonical channel-error sink (auto-wired via __init_subclass__) ──
    #
    # Every method in ``_AUTO_RECORD_METHODS`` defined on a concrete
    # adapter gets transparently wrapped at class-load time so an
    # escaping exception:
    #   (a) increments the per-(channel, error_type) Prometheus counter
    #       in ``admin.metrics.MetricsCollector`` — ops time-series signal
    #   (b) appends a structured row to ``admin.dashboard.AdminDashboard``
    #       — admin UI's recent-errors panel
    #   (c) when severity is critical (auth / sdk_missing / persistent
    #       connect failure), publishes a ``setup_progress`` SSE event
    #       with ``status='needs_user_action'`` + ``action_hint`` so
    #       the demopage renders a one-click fix card
    #   (d) is re-raised unchanged — wrapping must not change the
    #       caller-visible contract of any method
    #
    # Existing in-adapter try/except blocks that catch + return a
    # ``SendResult(success=False, error=...)`` keep working as before;
    # they short-circuit the wrapper.  Those call sites can OPT IN to
    # finer-grained reporting by calling ``self._record_channel_error(...)``
    # explicitly from inside the except.

    @staticmethod
    def _classify_exception(exc: BaseException,
                            fallback: str = 'send_failed') -> str:
        """Map an exception instance to a low-cardinality error_type
        suitable for the (channel, error_type) → count counter.
        Subclasses may override for platform-specific classification
        (e.g. mapping ``telegram.error.RetryAfter`` → ``rate_limit``)
        but the default covers the common HARTOS-internal taxonomy."""
        if isinstance(exc, ChannelRateLimitError):
            return 'rate_limit'
        if isinstance(exc, ChannelAuthError):
            return 'auth'
        if isinstance(exc, ChannelSDKMissingError):
            return 'sdk_missing'
        if isinstance(exc, ChannelConnectionError):
            return 'connect_failed'
        if isinstance(exc, ChannelSendError):
            return 'send_failed'
        # Non-channel exception types that adapters commonly let escape.
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return 'timeout'
        if isinstance(exc, ImportError):
            return 'sdk_missing'
        if isinstance(exc, (ConnectionError, OSError)):
            return 'network'
        return fallback

    @staticmethod
    def _severity_for(error_type: str) -> str:
        """Map error_type to severity for the dashboard log + the
        setup_progress critical gate.  Centralised so a future tweak
        (e.g. promoting persistent 'network' to critical after N
        consecutive failures) lives in one place."""
        if error_type in ('auth', 'sdk_missing'):
            return 'critical'
        if error_type == 'rate_limit':
            return 'warning'
        return 'error'

    @staticmethod
    def _action_hint_for(channel: str, error_type: str) -> str:
        """User-facing action hint shipped with the setup_progress SSE
        card.  Frontend renders these as clickable affordances."""
        hints = {
            'auth':        f'reconfigure_{channel}_token',
            'sdk_missing': f'install_{channel}_sdk',
            'rate_limit':  'wait_and_retry',
            'connect_failed': f'retry_{channel}_connect',
            'send_failed': 'retry_last_message',
            'timeout':     'retry_or_check_network',
            'network':     'check_network',
        }
        return hints.get(error_type, 'investigate')

    def _record_channel_error(self,
                              error_type: str,
                              exc: Optional[BaseException] = None,
                              severity: Optional[str] = None,
                              context: Optional[Dict[str, Any]] = None) -> None:
        """Single canonical channel-error sink.  Called automatically
        by the ``__init_subclass__`` wrapper for any escaping exception
        from a method in ``_AUTO_RECORD_METHODS``.  Adapters may ALSO
        call this directly from internal try/except blocks where they
        currently catch + log + return ``SendResult(success=False)`` —
        otherwise those swallowed exceptions don't reach the counter.

        Side-effects (all best-effort, never propagate):
          1. ``MetricsCollector.record_error(channel, error_type)``
             — Prometheus / time-series counter
          2. ``AdminDashboard.record_error(error_type, message, ...)``
             — admin-UI structured log
          3. If ``severity='critical'``: ``publish_event('setup_progress',
             {'status': 'needs_user_action', 'action_hint': ...})``
             — live UI card
          4. ``record_exception(exc, module, function, context)``
             — pushes into the canonical ExceptionCollector singleton
             so the existing self-heal pipeline (ExceptionWatcher in
             agent_daemon → SelfHealingDispatcher → self_heal goal →
             idle agent fixes the root cause via repair tools) sees
             channel failures the same as any other Python exception.
             This is the wiring that turns a silent adapter failure
             into an autonomous remediation goal.

        A failure in ANY side-effect is logged and swallowed; the
        original exception (if any) is the caller's concern and is
        not touched here.
        """
        sev = severity or self._severity_for(error_type)
        msg = str(exc) if exc is not None else error_type
        channel = self.name if hasattr(self, 'name') else 'unknown'

        # (1) Prometheus counter
        try:
            from integrations.channels.admin.metrics import get_metrics_collector
            get_metrics_collector().record_error(channel, error_type)
        except Exception as e:  # noqa: BLE001
            logger.debug("channel-error metrics sink failed: %s", e)

        # (2) Admin-UI structured log
        try:
            from integrations.channels.admin.dashboard import (
                get_dashboard, ErrorSeverity,
            )
            sev_enum = getattr(ErrorSeverity, sev.upper(), ErrorSeverity.ERROR)
            stack = traceback.format_exc() if exc is not None else None
            get_dashboard().record_error(
                error_type=error_type,
                message=msg,
                channel=channel,
                severity=sev_enum,
                stack_trace=stack,
                context=context or {},
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("channel-error dashboard sink failed: %s", e)

        # (3) setup_progress card — only for critical (auth / sdk_missing
        # / explicitly-marked-critical adapter-level escalations).  Other
        # severities don't fire UI cards (operator looks at the admin
        # panel or Prometheus instead) to avoid card spam on transient
        # rate-limits + retried network blips.
        if sev == 'critical':
            try:
                from integrations.social.realtime import publish_event
                publish_event('setup_progress', {
                    'type': 'setup_progress',
                    'job_type': f'channel_{channel}',
                    'status': 'needs_user_action',
                    'channel': channel,
                    'error_type': error_type,
                    'message': msg[:280],
                    'action_hint': self._action_hint_for(channel, error_type),
                })
            except Exception as e:  # noqa: BLE001
                logger.debug("channel-error setup_progress sink failed: %s", e)

        # (4) Self-heal pipeline — single canonical entry point.
        # report_subsystem_failure is the ONE helper every subsystem
        # uses to feed ExceptionCollector with consistent module-key
        # shape ({subsystem}.{identifier}).  TTS demotion, VLM /
        # LLM model-worker failures, daemon thread death, dynamic
        # tool registration miss all funnel through the same helper —
        # no per-subsystem string-formatting of the module key, no
        # parallel push paths.  The dispatcher's pattern_key grouping
        # only works correctly when every subsystem uses the same
        # naming convention; this helper enforces it.
        if exc is not None:
            try:
                from hartos.exception_collector import report_subsystem_failure
                method_name = (context or {}).get('method', 'unknown')
                report_subsystem_failure(
                    subsystem='channels',
                    identifier=channel,
                    exc=exc,
                    function=method_name,
                    error_type=error_type,
                    severity=sev,
                    **{k: v for k, v in (context or {}).items()
                       if k not in ('method',)},
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "channel-error self-heal push failed: %s", e)

    @classmethod
    def __init_subclass__(cls, **kwargs):
        """Wrap concrete adapter methods so escaping exceptions land
        in the canonical sink without touching adapter code.

        Idempotent: a method that's already wrapped (via the
        ``_AUTO_RECORD_WRAPPED_ATTR`` sentinel) is skipped on repeat
        class-loads — keeps test reloads + decorator stacks clean.

        Only methods listed in ``_AUTO_RECORD_METHODS`` are wrapped.
        Internal helpers stay untouched so adapters can raise from
        non-public code without triggering the sink."""
        super().__init_subclass__(**kwargs)
        for method_name in _AUTO_RECORD_METHODS:
            method = cls.__dict__.get(method_name)
            if method is None:
                continue  # this adapter didn't override the method
            if getattr(method, _AUTO_RECORD_WRAPPED_ATTR, False):
                continue  # already wrapped

            if inspect.iscoroutinefunction(method):
                wrapped = _wrap_async_with_error_sink(method, method_name)
            else:
                wrapped = _wrap_sync_with_error_sink(method, method_name)
            setattr(wrapped, _AUTO_RECORD_WRAPPED_ATTR, True)
            setattr(cls, method_name, wrapped)


class ChannelError(Exception):
    """Base exception for channel errors."""
    pass


class ChannelConnectionError(ChannelError):
    """Error connecting to channel."""
    pass


class ChannelSendError(ChannelError):
    """Error sending message."""
    pass


class ChannelRateLimitError(ChannelError):
    """Rate limit exceeded."""
    def __init__(self, retry_after: Optional[int] = None):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s" if retry_after else "Rate limited")


class ChannelAuthError(ChannelError):
    """Authentication / credentials failure — missing or invalid token,
    expired session, OAuth revoked, etc.  Distinguished from generic
    ChannelError so the base-class error sink can emit a critical
    ``setup_progress`` SSE card prompting the operator to refresh
    credentials, rather than logging it as a transient send failure."""
    pass


class ChannelSDKMissingError(ChannelError):
    """Required SDK / Python package is not importable on this install.
    Surfaces as a critical ``setup_progress`` card prompting install
    (e.g. ``pip install discord.py``).  Distinct from auth so the UI
    can route to a different action (install vs reconfigure)."""
    pass


def _wrap_async_with_error_sink(method: Callable, method_name: str) -> Callable:
    """Wrap an async adapter method so escaping exceptions land in
    the canonical sink before being re-raised.  Re-raise preserves
    the existing caller contract — wrapping must be transparent."""
    @wraps(method)
    async def _wrapped(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except Exception as exc:
            try:
                error_type = self._classify_exception(
                    exc, fallback=method_name)
                self._record_channel_error(
                    error_type, exc,
                    context={'method': method_name})
            except Exception as sink_err:  # noqa: BLE001
                # Sink machinery itself broke — log + still re-raise
                # the original so adapter callers are unaffected.
                logger.debug(
                    "channel-error sink failed for %s.%s: %s",
                    type(self).__name__, method_name, sink_err)
            raise
    return _wrapped


def _wrap_sync_with_error_sink(method: Callable, method_name: str) -> Callable:
    """Sync counterpart of ``_wrap_async_with_error_sink`` — for
    adapters that override ``_AUTO_RECORD_METHODS`` entries as
    plain ``def``."""
    @wraps(method)
    def _wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            try:
                error_type = self._classify_exception(
                    exc, fallback=method_name)
                self._record_channel_error(
                    error_type, exc,
                    context={'method': method_name})
            except Exception as sink_err:  # noqa: BLE001
                logger.debug(
                    "channel-error sink failed for %s.%s: %s",
                    type(self).__name__, method_name, sink_err)
            raise
    return _wrapped
