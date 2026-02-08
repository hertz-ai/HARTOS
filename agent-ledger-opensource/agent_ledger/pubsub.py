"""
Redis PubSub notification layer for distributed SmartLedger instances.

Enables real-time cross-host notifications when tasks are completed,
delegated, or need verification. Activates only when Redis backend is used.

Usage:
    from agent_ledger.pubsub import LedgerPubSub

    pubsub = LedgerPubSub(redis_client, agent_id="agent_A")
    pubsub.subscribe([LedgerPubSub.CHANNEL_TASK_UPDATE], my_callback)
    pubsub.publish_task_update("task_1", "IN_PROGRESS", "COMPLETED", result_hash="abc123")
"""

import json
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class LedgerPubSub:
    """Redis PUBSUB wrapper for distributed SmartLedger notifications."""

    CHANNEL_TASK_UPDATE = "agent_ledger:task_update"
    CHANNEL_DELEGATION = "agent_ledger:delegation"
    CHANNEL_AGENT_ANNOUNCE = "agent_ledger:agent_announce"
    CHANNEL_HEARTBEAT = "agent_ledger:heartbeat"
    CHANNEL_VERIFICATION = "agent_ledger:verification"

    def __init__(self, redis_client, agent_id: str):
        """
        Initialize with existing Redis connection and agent identity.

        Args:
            redis_client: redis.Redis instance (from RedisBackend.redis_client)
            agent_id: This agent's unique identifier
        """
        self._redis = redis_client
        self._agent_id = agent_id
        self._pubsub = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False

    def subscribe(self, channels: List[str], callback: Callable[[str, Dict], None]) -> None:
        """
        Subscribe to notification channels with a callback handler.

        Args:
            channels: List of channel names (use class constants)
            callback: Function(channel: str, data: dict) called on each message
        """
        self._pubsub = self._redis.pubsub()
        self._pubsub.subscribe(*channels)
        self._running = True

        def _listen():
            for message in self._pubsub.listen():
                if not self._running:
                    break
                if message["type"] == "message":
                    try:
                        channel = message["channel"]
                        if isinstance(channel, bytes):
                            channel = channel.decode("utf-8")
                        data = json.loads(message["data"])
                        # Don't echo own messages
                        if data.get("agent_id") != self._agent_id:
                            callback(channel, data)
                    except Exception as e:
                        logger.debug(f"PubSub message parse error: {e}")

        self._listener_thread = threading.Thread(target=_listen, daemon=True, name="ledger-pubsub")
        self._listener_thread.start()
        logger.info(f"PubSub subscribed to {channels} as {self._agent_id}")

    def _publish(self, channel: str, data: Dict[str, Any]) -> None:
        """Publish a message to a channel."""
        data["agent_id"] = self._agent_id
        data["timestamp"] = datetime.now().isoformat()
        try:
            self._redis.publish(channel, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"PubSub publish error on {channel}: {e}")

    def publish_task_update(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
        result_hash: Optional[str] = None,
    ) -> None:
        """Broadcast task state change to all listeners."""
        self._publish(self.CHANNEL_TASK_UPDATE, {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "result_hash": result_hash,
        })

    def publish_delegation(
        self,
        task_id: str,
        from_agent: str,
        to_agent: str,
        description: str = "",
    ) -> None:
        """Broadcast delegation event."""
        self._publish(self.CHANNEL_DELEGATION, {
            "task_id": task_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "description": description,
        })

    def publish_agent_announce(self, capabilities: List[str], host_info: Optional[Dict] = None) -> None:
        """Announce agent presence with skills and host info."""
        self._publish(self.CHANNEL_AGENT_ANNOUNCE, {
            "capabilities": capabilities,
            "host_info": host_info or {},
        })

    def publish_verification_request(self, task_id: str, result_hash: str) -> None:
        """Request other agents to verify a task result."""
        self._publish(self.CHANNEL_VERIFICATION, {
            "task_id": task_id,
            "result_hash": result_hash,
            "action": "verify_request",
        })

    def stop(self) -> None:
        """Stop the listener thread cleanly."""
        self._running = False
        if self._pubsub:
            try:
                self._pubsub.unsubscribe()
                self._pubsub.close()
            except Exception:
                pass
        logger.info(f"PubSub stopped for {self._agent_id}")
