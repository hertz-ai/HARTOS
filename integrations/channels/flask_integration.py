"""
Flask Integration for Channel Adapters

Integrates the channel registry with the existing Flask API.
Routes incoming channel messages to the agent system.
"""

import asyncio
import logging
import os
import json
import threading
from typing import Optional, Dict, Any
from functools import wraps

import requests
from core.http_pool import pooled_post

from .base import Message, ChannelConfig
from .registry import ChannelRegistry, ChannelRegistryConfig, get_registry

logger = logging.getLogger(__name__)


class FlaskChannelIntegration:
    """
    Integrates channel adapters with the Flask-based agent API.

    This bridges the async channel adapters with the sync Flask app.
    """

    def __init__(
        self,
        agent_api_url: str = None,
        default_user_id: int = None,
        default_prompt_id: int = None,
        create_mode: bool = False,
        device_id: str = None,
    ):
        from core.constants import DEFAULT_USER_ID, DEFAULT_PROMPT_ID
        if default_user_id is None:
            default_user_id = DEFAULT_USER_ID
        if default_prompt_id is None:
            default_prompt_id = DEFAULT_PROMPT_ID
        if agent_api_url is None:
            from core.port_registry import get_port
            agent_api_url = f"http://localhost:{get_port('backend')}/chat"
        self.agent_api_url = agent_api_url
        self.default_user_id = default_user_id
        self.default_prompt_id = default_prompt_id
        self.create_mode = create_mode
        self._device_id = device_id

        self.registry = get_registry()
        self.registry.set_agent_handler(self._handle_message)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # Persistent session manager (LRU cache + JSON persistence + 24h cleanup)
        from .session_manager import get_session_manager
        self._session_manager = get_session_manager()

        # Response router for fan-out, conversation logging, WAMP
        from .response.router import get_response_router
        self._response_router = get_response_router(registry=self.registry)

    def _handle_message(self, message: Message) -> str:
        """
        Handle incoming message from any channel.

        Routes to Flask API and returns response. Resolves the
        Hevolve user_id via UserChannelBinding first — the user
        registered this channel (e.g. WhatsApp +1234) to their
        Hevolve account via Connect_Channel, and the binding row
        is the single source of truth for (channel, sender_id) →
        user_id. Falls back to the session cache and finally the
        configured default.
        """
        try:
            # Get or create persistent session (replaces plain dict)
            session = self._session_manager.get_session(
                message.channel, message.sender_id
            )

            # ── Resolve user_id ───────────────────────────────────
            # 1. UserChannelBinding (durable DB row written by
            #    Connect_Channel tool + response router)
            # 2. Session cache (in-memory per (channel, sender_id))
            # 3. Configured default
            # Without step 1, a WhatsApp user who bound their
            # account via Connect_Channel would still hit the chat
            # as user_id=10077 (default) and lose access to their
            # per-user memory / bindings / tool permissions.
            user_id = self._resolve_user_id_for_sender(
                channel=message.channel,
                sender_id=message.sender_id,
                fallback=(session.user_id if session and session.user_id
                          else self.default_user_id),
            )
            # prompt_id priority: session (user override) > per-channel config > global default
            prompt_id = (
                (session.prompt_id if session and session.prompt_id else None)
                or self._get_channel_prompt_id(message.channel)
                or self.default_prompt_id
            )

            # Track message in session history
            if session:
                session.add_message('user', message.content)

            # Skip if group and bot not mentioned (configurable)
            adapter = self.registry.get(message.channel)
            if adapter and message.is_group and not message.is_bot_mentioned:
                if adapter.config.require_mention_in_groups:
                    logger.debug(f"Ignoring group message without mention")
                    return None

            # Prepare request to agent API
            payload = {
                "user_id": user_id,
                "prompt_id": prompt_id,
                "prompt": message.content,
                "create_agent": self.create_mode,
                "device_id": self._device_id,
                "channel_context": {
                    "channel": message.channel,
                    "sender_id": message.sender_id,
                    "sender_name": message.sender_name,
                    "chat_id": message.chat_id,
                    "is_group": message.is_group,
                    "message_id": message.id,
                }
            }

            logger.info(f"Routing message from {message.channel}:{message.sender_id} to agent")

            # Call agent API
            response = pooled_post(
                self.agent_api_url,
                json=payload,
                timeout=120,  # 2 minute timeout for agent processing
            )

            if response.status_code == 200:
                result = response.json()
                agent_reply = result.get("response", "I processed your request.")

                # Track response in session history
                if session:
                    session.add_message('assistant', agent_reply)

                # Auto-upsert channel binding + log user message
                self._response_router.upsert_binding(
                    user_id, message.channel, message.sender_id, message.chat_id)
                self._response_router.log_user_message(
                    user_id, message.channel, message.content)

                # Route response: WAMP desktop + fan-out to bound channels + log
                self._response_router.route_response(
                    user_id=user_id,
                    response_text=agent_reply,
                    channel_context=payload.get('channel_context'),
                    fan_out=True,
                )

                return agent_reply
            else:
                logger.error(f"Agent API error: {response.status_code} - {response.text}")
                return "Sorry, I encountered an error processing your request."

        except requests.Timeout:
            logger.error("Agent API timeout")
            return "Sorry, the request timed out. Please try again."
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            return "Sorry, an unexpected error occurred."

    def _resolve_user_id_for_sender(
        self, channel: str, sender_id: str, fallback,
    ):
        """Resolve (channel_type, channel_sender_id) → Hevolve user_id
        via the UserChannelBinding table.

        Returns the bound user_id when the user has registered this
        channel via the Connect_Channel tool, otherwise the provided
        fallback (session cache or default). The lookup must never
        raise — binding DB failures log at debug and fall through so
        message handling is never blocked by a transient DB issue.
        """
        if not channel or not sender_id:
            return fallback
        try:
            from integrations.social.models import get_db, UserChannelBinding
        except ImportError:
            return fallback
        try:
            db = get_db()
            try:
                row = db.query(UserChannelBinding).filter_by(
                    channel_type=str(channel).lower(),
                    channel_sender_id=str(sender_id),
                    is_active=True,
                ).first()
                if row and row.user_id:
                    logger.debug(
                        f"Channel binding resolved: {channel}:{sender_id} "
                        f"→ user_id={row.user_id}"
                    )
                    return row.user_id
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(
                f"UserChannelBinding lookup failed "
                f"({channel}:{sender_id}): {e}"
            )
        return fallback

    def _get_channel_prompt_id(self, channel_type: str) -> Optional[int]:
        """Read per-channel prompt_id from admin config (if set)."""
        try:
            from .admin.api import get_api
            api = get_api()
            config = api._channels.get(channel_type, {})
            pid = config.get('prompt_id')
            return int(pid) if pid else None
        except Exception:
            return None

    # ── Adapter factory import paths ─────────────────────────────
    # Maps channel_type → (module_path, factory_function_name).
    # Core adapters live in integrations.channels, extensions in
    # integrations.channels.extensions, hardware in .hardware.
    _ADAPTER_FACTORIES: Dict[str, tuple] = {
        'telegram':       ('.telegram_adapter',       'create_telegram_adapter'),
        'discord':        ('.discord_adapter',        'create_discord_adapter'),
        'whatsapp':       ('.whatsapp_adapter',       'create_whatsapp_adapter'),
        'slack':          ('.slack_adapter',           'create_slack_adapter'),
        'signal':         ('.signal_adapter',          'create_signal_adapter'),
        'imessage':       ('.imessage_adapter',        'create_imessage_adapter'),
        'google_chat':    ('.google_chat_adapter',     'create_google_chat_adapter'),
        'web':            ('.web_adapter',             'create_web_adapter'),
        # Extensions
        'teams':          ('.extensions.teams_adapter',          'create_teams_adapter'),
        'matrix':         ('.extensions.matrix_adapter',         'create_matrix_adapter'),
        'mattermost':     ('.extensions.mattermost_adapter',     'create_mattermost_adapter'),
        'nextcloud':      ('.extensions.nextcloud_adapter',      'create_nextcloud_adapter'),
        'rocketchat':     ('.extensions.rocketchat_adapter',     'create_rocketchat_adapter'),
        'messenger':      ('.extensions.messenger_adapter',      'create_messenger_adapter'),
        'instagram':      ('.extensions.instagram_adapter',      'create_instagram_adapter'),
        'twitter':        ('.extensions.twitter_adapter',        'create_twitter_adapter'),
        'line':           ('.extensions.line_adapter',            'create_line_adapter'),
        'viber':          ('.extensions.viber_adapter',           'create_viber_adapter'),
        'wechat':         ('.extensions.wechat_adapter',         'create_wechat_adapter'),
        'zalo':           ('.extensions.zalo_adapter',            'create_zalo_adapter'),
        'twitch':         ('.extensions.twitch_adapter',          'create_twitch_adapter'),
        'nostr':          ('.extensions.nostr_adapter',           'create_nostr_adapter'),
        'tlon':           ('.extensions.tlon_adapter',            'create_tlon_adapter'),
        'openprose':      ('.extensions.openprose_adapter',       'create_openprose_adapter'),
        'telegram_user':  ('.extensions.telegram_user_adapter',   'create_telegram_user_adapter'),
        'discord_user':   ('.extensions.discord_user_adapter',    'create_discord_user_adapter'),
        'zalo_user':      ('.extensions.zalo_user_adapter',       'create_zalo_user_adapter'),
        'bluebubbles':    ('.extensions.bluebubbles_adapter',     'create_bluebubbles_adapter'),
        'email':          ('.extensions.email_adapter',            'create_email_adapter'),
        'voice':          ('.extensions.voice_adapter',            'create_voice_adapter'),
    }

    # Env var fallbacks for token/credential per channel type
    _ENV_FALLBACKS: Dict[str, str] = {
        'telegram':  'TELEGRAM_BOT_TOKEN',
        'discord':   'DISCORD_BOT_TOKEN',
        'whatsapp':  'WHATSAPP_API_URL',
        'slack':     'SLACK_BOT_TOKEN',
        'signal':    'SIGNAL_CLI_URL',
        'teams':     'TEAMS_BOT_TOKEN',
    }

    def register_channel(self, channel_type: str, token: str = None, **kwargs) -> bool:
        """Register any channel adapter by type.

        Generic factory — replaces per-channel register_* methods.
        Falls back to env var if no token provided.  Returns True on success.
        """
        factory_info = self._ADAPTER_FACTORIES.get(channel_type)
        if not factory_info:
            logger.warning(f"Unknown channel type: {channel_type}")
            return False

        module_path, factory_name = factory_info
        token = token or os.getenv(self._ENV_FALLBACKS.get(channel_type, ''))
        if not token and channel_type not in ('web', 'imessage', 'openprose'):
            # web/imessage/openprose don't need external tokens
            logger.warning(f"{channel_type} token not provided, skipping")
            return False

        try:
            import importlib
            mod = importlib.import_module(module_path, package='integrations.channels')
            factory_fn = getattr(mod, factory_name)
            if token:
                adapter = factory_fn(token=token, **kwargs)
            else:
                adapter = factory_fn(**kwargs)
            self.registry.register(adapter)
            logger.info(f"{channel_type} adapter registered")
            return True
        except Exception as e:
            logger.warning(f"{channel_type} adapter registration failed: {e}")
            return False

    # Keep legacy methods as thin delegates for backward compat
    def register_telegram(self, token: str = None, **kwargs) -> None:
        self.register_channel('telegram', token=token, **kwargs)

    def register_discord(self, token: str = None, **kwargs) -> None:
        self.register_channel('discord', token=token, **kwargs)

    def register_whatsapp(self, api_url: str = None, **kwargs) -> None:
        self.register_channel('whatsapp', token=api_url, **kwargs)

    def set_user_session(
        self,
        channel: str,
        sender_id: str,
        user_id: int,
        prompt_id: int,
    ) -> None:
        """Set user session mapping for a channel sender."""
        session = self._session_manager.get_session(channel, sender_id, user_id=user_id, prompt_id=prompt_id)

    def _run_async_loop(self) -> None:
        """Run asyncio event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self.registry.start_all())
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self.registry.stop_all())
            self._loop.close()

    def start(self) -> None:
        """Start all channel adapters in background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Channels already running")
            return

        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()
        logger.info("Channel adapters started in background")

    def stop(self) -> None:
        """Stop all channel adapters."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=5)

        logger.info("Channel adapters stopped")

    def get_status(self) -> Dict[str, str]:
        """Get status of all channels."""
        return {
            name: status.value
            for name, status in self.registry.get_status().items()
        }


# Global integration instance
_integration: Optional[FlaskChannelIntegration] = None


def get_channel_integration() -> FlaskChannelIntegration:
    """Get or create the global channel integration."""
    global _integration
    if _integration is None:
        _integration = FlaskChannelIntegration()
    return _integration


def init_channels(app=None, config: Dict[str, Any] = None) -> FlaskChannelIntegration:
    """
    Initialize channel integrations.

    Call this from your Flask app startup:

        from integrations.channels.flask_integration import init_channels

        app = Flask(__name__)
        channels = init_channels(app)
        channels.register_telegram()
        channels.start()

    Args:
        app: Flask app instance (optional)
        config: Configuration dict (optional)

    Returns:
        FlaskChannelIntegration instance
    """
    config = config or {}
    from core.constants import DEFAULT_USER_ID, DEFAULT_PROMPT_ID

    integration = FlaskChannelIntegration(
        agent_api_url=config.get("agent_api_url", "http://localhost:6777/chat"),
        default_user_id=config.get("default_user_id", DEFAULT_USER_ID),
        default_prompt_id=config.get("default_prompt_id", DEFAULT_PROMPT_ID),
        create_mode=config.get("create_mode", False),
        device_id=config.get("device_id"),
    )

    global _integration
    _integration = integration

    # Add Flask routes if app provided
    if app:
        @app.route("/channels/status", methods=["GET"])
        def channel_status():
            return integration.get_status()

        @app.route("/channels/send", methods=["POST"])
        def channel_send():
            from flask import request, jsonify

            data = request.json
            channel = data.get("channel")
            chat_id = data.get("chat_id")
            text = data.get("text")

            if not all([channel, chat_id, text]):
                return jsonify({"error": "Missing required fields"}), 400

            # Run async send in the event loop
            if integration._loop:
                future = asyncio.run_coroutine_threadsafe(
                    integration.registry.send_to_channel(channel, chat_id, text),
                    integration._loop,
                )
                result = future.result(timeout=30)
                return jsonify({
                    "success": result.success,
                    "message_id": result.message_id,
                    "error": result.error,
                })
            else:
                return jsonify({"error": "Channels not running"}), 503

    return integration
