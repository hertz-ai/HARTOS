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
        default_user_id: int = 10077,
        default_prompt_id: int = 8888,
        create_mode: bool = False,
        device_id: str = None,
    ):
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

        Routes to Flask API and returns response.
        """
        try:
            # Get or create persistent session (replaces plain dict)
            session = self._session_manager.get_session(
                message.channel, message.sender_id
            )
            user_id = session.user_id if session and session.user_id else self.default_user_id
            prompt_id = session.prompt_id if session and session.prompt_id else self.default_prompt_id

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

    def register_telegram(self, token: str = None, **kwargs) -> None:
        """Register Telegram adapter."""
        from .telegram_adapter import create_telegram_adapter

        token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            logger.warning("Telegram token not provided, skipping registration")
            return

        adapter = create_telegram_adapter(token, **kwargs)
        self.registry.register(adapter)
        logger.info("Telegram adapter registered")

    def register_discord(self, token: str = None, **kwargs) -> None:
        """Register Discord adapter."""
        try:
            from .discord_adapter import create_discord_adapter

            token = token or os.getenv("DISCORD_BOT_TOKEN")
            if not token:
                logger.warning("Discord token not provided, skipping registration")
                return

            adapter = create_discord_adapter(token, **kwargs)
            self.registry.register(adapter)
            logger.info("Discord adapter registered")
        except ImportError:
            logger.warning("Discord adapter not available")

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

    integration = FlaskChannelIntegration(
        agent_api_url=config.get("agent_api_url", "http://localhost:6777/chat"),
        default_user_id=config.get("default_user_id", 10077),
        default_prompt_id=config.get("default_prompt_id", 8888),
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
