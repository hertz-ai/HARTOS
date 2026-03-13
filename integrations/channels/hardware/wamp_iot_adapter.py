"""
WAMP/Crossbar Channel Adapter — Pub/sub for IoT devices via existing Crossbar router.

Bridges IoT sensors and actuators to HART agents through the existing Crossbar
WAMP infrastructure (ws://host:8088/ws). Uses the same router that powers
agent multichat and channel bridging.

Subscribes to configurable WAMP topics, publishes agent responses.
ESP32/microcontrollers connect via WebSocket WAMP or through a serial→WAMP gateway.

Usage:
    from integrations.channels.hardware.mqtt_adapter import WAMPIoTAdapter
    adapter = WAMPIoTAdapter(topics=['com.hertzai.hevolve.iot.sensors.#'])
    adapter.on_message(handler)
    await adapter.start()

Environment:
    CBURL               Crossbar WebSocket URL (default: ws://localhost:8088/ws)
    CBREALM             WAMP realm (default: realm1)
    HEVOLVE_IOT_TOPICS  Comma-separated WAMP topics to subscribe
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Any

from integrations.channels.base import (
    ChannelAdapter, ChannelConfig, ChannelStatus,
    Message, SendResult,
)

logger = logging.getLogger(__name__)

# Also export as MQTTAdapter for backward compat with __init__.py registration
# (the auto_register function references MQTTAdapter)


class WAMPIoTAdapter(ChannelAdapter):
    """Channel adapter for IoT devices via Crossbar WAMP pub/sub.

    Subscribe topics generate Message events.
    send_message publishes to a WAMP topic (chat_id = topic URI).
    """

    def __init__(
        self,
        crossbar_url: str = '',
        realm: str = '',
        topics: List[str] = None,
        config: ChannelConfig = None,
    ):
        super().__init__(config or ChannelConfig())
        self._crossbar_url = crossbar_url or os.environ.get(
            'CBURL', 'ws://localhost:8088/ws')
        self._realm = realm or os.environ.get('CBREALM', 'realm1')
        self._topics = topics or _parse_topic_list(
            os.environ.get('HEVOLVE_IOT_TOPICS',
                           'com.hertzai.hevolve.iot.sensors'))
        self._component = None
        self._session = None
        self._loop = None
        self._thread = None

    @property
    def name(self) -> str:
        return 'wamp_iot'

    async def connect(self) -> bool:
        """Connect to Crossbar WAMP router and subscribe to IoT topics."""
        try:
            from autobahn.asyncio.component import Component
        except ImportError:
            logger.error("WAMP IoT adapter: autobahn not installed")
            return False

        if not self._crossbar_url:
            logger.error("WAMP IoT adapter: no Crossbar URL configured")
            return False

        try:
            self._component = Component(
                transports=self._crossbar_url,
                realm=self._realm,
            )

            adapter_ref = self  # Capture for closures

            @self._component.on_join
            async def on_join(session, details):
                adapter_ref._session = session
                logger.info("WAMP IoT adapter: joined session")
                # Subscribe to IoT topics
                for topic in adapter_ref._topics:
                    try:
                        await session.subscribe(
                            adapter_ref._on_wamp_event, topic)
                        logger.debug(f"WAMP IoT subscribed: {topic}")
                    except Exception as e:
                        logger.error(f"WAMP subscribe failed for {topic}: {e}")

            @self._component.on_leave
            async def on_leave(session, details):
                adapter_ref._session = None
                if adapter_ref._running:
                    logger.warning("WAMP IoT adapter: session lost, will reconnect")

            # Run component in background thread with its own event loop
            self._thread = threading.Thread(
                target=self._run_component_loop, daemon=True)
            self._thread.start()

            # Wait for session (up to 5 seconds)
            for _ in range(50):
                if self._session is not None:
                    self.status = ChannelStatus.CONNECTED
                    logger.info(
                        f"WAMP IoT adapter: connected to {self._crossbar_url}, "
                        f"topics={self._topics}")
                    return True
                time.sleep(0.1)

            logger.warning("WAMP IoT adapter: connection timeout")
            return False
        except Exception as e:
            logger.error(f"WAMP IoT adapter: connect failed: {e}")
            self.status = ChannelStatus.ERROR
            return False

    def _run_component_loop(self):
        """Run WAMP component in a dedicated event loop thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            from autobahn.asyncio.component import run
            run([self._component], log_level='warn')
        except Exception as e:
            if self._running:
                logger.error(f"WAMP IoT component loop error: {e}")

    async def disconnect(self) -> None:
        """Disconnect from Crossbar."""
        self._running = False
        self._session = None
        if self._component:
            try:
                # Component will stop when the loop ends
                pass
            except Exception:
                pass

    async def send_message(
        self, chat_id: str, text: str,
        reply_to: Optional[str] = None,
        media: Optional[List] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Publish a message to a WAMP topic.

        chat_id: WAMP topic URI (e.g. "com.hertzai.hevolve.iot.actuators.led1")
        text: payload (plain text or JSON string)
        """
        if not self._session:
            return SendResult(success=False, error="WAMP session not active")

        try:
            # Try to parse as JSON for structured messages
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                payload = {'text': text}

            self._session.publish(chat_id, payload)
            return SendResult(
                success=True,
                message_id=f"wamp_{uuid.uuid4().hex[:8]}",
            )
        except Exception as e:
            logger.error(f"WAMP publish error: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(self, chat_id: str, message_id: str,
                           text: str, buttons=None) -> SendResult:
        """WAMP is pub/sub — edit means re-publish."""
        return await self.send_message(chat_id, text)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return False  # Not applicable to WAMP pub/sub

    async def send_typing(self, chat_id: str) -> None:
        pass  # Not applicable

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        return {
            'crossbar_url': self._crossbar_url,
            'realm': self._realm,
            'topic': chat_id,
            'connected': self._session is not None,
        }

    # ─── WAMP Event Handler ───

    def _on_wamp_event(self, *args, **kwargs):
        """Called when an event arrives on a subscribed WAMP topic."""
        try:
            # WAMP events can be positional args or kwargs
            if args and isinstance(args[0], dict):
                data = args[0]
            elif kwargs:
                data = kwargs
            else:
                data = {'raw_args': list(args)}

            # Extract text and sender from payload
            if isinstance(data, dict):
                text = data.get('text', json.dumps(data))
                sender = data.get('sender', data.get('node_id', 'wamp_device'))
                topic = data.get('topic', 'unknown')
            else:
                text = str(data)
                sender = 'wamp_device'
                topic = 'unknown'

            msg = Message(
                id=str(uuid.uuid4())[:8],
                channel='wamp_iot',
                sender_id=sender,
                sender_name=f'WAMP {sender}',
                chat_id=topic,
                text=text,
                raw=data if isinstance(data, dict) else {'value': data},
            )

            for handler in self._message_handlers:
                try:
                    handler(msg)
                except Exception as e:
                    logger.error(f"WAMP IoT handler error: {e}")

        except Exception as e:
            logger.error(f"WAMP event processing error: {e}")


# Backward compat alias — __init__.py references MQTTAdapter
MQTTAdapter = WAMPIoTAdapter


def _parse_topic_list(env_value: str) -> List[str]:
    """Parse comma-separated WAMP topic URIs from env var."""
    if not env_value:
        return ['com.hertzai.hevolve.iot.sensors']
    return [t.strip() for t in env_value.split(',') if t.strip()]
