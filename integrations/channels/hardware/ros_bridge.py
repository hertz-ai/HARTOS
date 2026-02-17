"""
ROS 2 Bridge Channel Adapter — Subscribe/publish to ROS 2 topics.

Bridges ROS 2 robots to Hyve agents. Subscribes to String and Image topics,
publishes agent responses back to ROS 2 topics.

Only loaded when HEVOLVE_ROS_BRIDGE_ENABLED=true (rclpy pulls ~500MB deps).

Usage:
    HEVOLVE_ROS_BRIDGE_ENABLED=true python embedded_main.py

    from integrations.channels.hardware.ros_bridge import ROSBridgeAdapter
    adapter = ROSBridgeAdapter(
        subscribe_topics=['/hyve/input', '/camera/image_raw'],
        publish_topic='/hyve/output',
    )
    adapter.on_message(handler)
    await adapter.start()
"""
import asyncio
import base64
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


class ROSBridgeAdapter(ChannelAdapter):
    """Channel adapter for ROS 2 topic pub/sub.

    String topics → Message events (text).
    Image topics → Message events (text = base64 frame, raw = metadata).
    send_message → publishes String to publish_topic.
    """

    def __init__(
        self,
        subscribe_topics: List[str] = None,
        publish_topic: str = '',
        node_name: str = '',
        frame_store=None,
        config: ChannelConfig = None,
    ):
        super().__init__(config or ChannelConfig())
        self._subscribe_topics = subscribe_topics or _parse_topic_list(
            os.environ.get('HEVOLVE_ROS_TOPICS', '/hyve/input'))
        self._publish_topic = publish_topic or os.environ.get(
            'HEVOLVE_ROS_PUBLISH_TOPIC', '/hyve/output')
        self._node_name = node_name or os.environ.get(
            'HEVOLVE_ROS_NODE_NAME', 'hyve_bridge')
        self._frame_store = frame_store  # Optional: inject frames into FrameStore
        self._node = None
        self._publisher = None
        self._subscriptions = []
        self._spin_thread = None

    @property
    def name(self) -> str:
        return 'ros'

    async def connect(self) -> bool:
        """Initialize ROS 2 node, create subscriptions and publisher."""
        try:
            import rclpy
            from rclpy.node import Node
        except ImportError:
            logger.error("ROS bridge: rclpy not installed")
            return False

        try:
            # Initialize ROS 2 if not already
            if not rclpy.ok():
                rclpy.init()

            self._node = rclpy.create_node(self._node_name)

            # Create publisher for agent responses
            from std_msgs.msg import String
            self._publisher = self._node.create_publisher(
                String, self._publish_topic, 10)

            # Subscribe to configured topics
            for topic in self._subscribe_topics:
                if _is_image_topic(topic):
                    self._subscribe_image(topic)
                else:
                    self._subscribe_string(topic)

            # Spin in background thread
            self._spin_thread = threading.Thread(
                target=self._spin_loop, daemon=True)
            self._spin_thread.start()

            self.status = ChannelStatus.CONNECTED
            logger.info(
                f"ROS bridge: node='{self._node_name}', "
                f"sub={self._subscribe_topics}, pub={self._publish_topic}"
            )
            return True
        except Exception as e:
            logger.error(f"ROS bridge: init failed: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Shutdown ROS 2 node."""
        self._running = False
        if self._node:
            try:
                self._node.destroy_node()
            except Exception:
                pass
        try:
            import rclpy
            if rclpy.ok():
                rclpy.shutdown()
        except Exception:
            pass

    async def send_message(
        self, chat_id: str, text: str,
        reply_to: Optional[str] = None,
        media: Optional[List] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Publish a String message to ROS 2 topic.

        chat_id: topic to publish to (defaults to self._publish_topic)
        text: message payload
        """
        if not self._node or not self._publisher:
            return SendResult(success=False, error="ROS node not initialized")

        try:
            from std_msgs.msg import String
            msg = String()
            msg.data = text

            # If chat_id matches publish topic, use existing publisher
            topic = chat_id or self._publish_topic
            if topic == self._publish_topic:
                self._publisher.publish(msg)
            else:
                # Create a one-shot publisher for different topic
                pub = self._node.create_publisher(String, topic, 10)
                pub.publish(msg)
                # Cleanup after brief delay
                self._node.create_timer(1.0, lambda: self._node.destroy_publisher(pub))

            return SendResult(success=True, message_id=f"ros_{uuid.uuid4().hex[:8]}")
        except Exception as e:
            logger.error(f"ROS publish error: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(self, chat_id: str, message_id: str,
                           text: str, buttons=None) -> SendResult:
        """ROS topics are streams — edit means re-publish."""
        return await self.send_message(chat_id, text)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return False  # Not applicable

    async def send_typing(self, chat_id: str) -> None:
        pass  # Not applicable

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        return {
            'node_name': self._node_name,
            'subscribe_topics': self._subscribe_topics,
            'publish_topic': self._publish_topic,
        }

    # ─── Internal ───

    def _subscribe_string(self, topic: str):
        """Subscribe to a String topic."""
        try:
            from std_msgs.msg import String

            def callback(msg):
                self._dispatch_ros_message(topic, msg.data)

            sub = self._node.create_subscription(String, topic, callback, 10)
            self._subscriptions.append(sub)
            logger.debug(f"ROS bridge: subscribed to String topic {topic}")
        except Exception as e:
            logger.error(f"ROS bridge: failed to subscribe to {topic}: {e}")

    def _subscribe_image(self, topic: str):
        """Subscribe to an Image topic (sensor_msgs/Image)."""
        try:
            from sensor_msgs.msg import Image

            def callback(msg):
                self._handle_image_message(topic, msg)

            sub = self._node.create_subscription(Image, topic, callback, 10)
            self._subscriptions.append(sub)
            logger.debug(f"ROS bridge: subscribed to Image topic {topic}")
        except ImportError:
            logger.warning(
                f"ROS bridge: sensor_msgs not available, "
                f"skipping image topic {topic}")
        except Exception as e:
            logger.error(f"ROS bridge: failed to subscribe to {topic}: {e}")

    def _handle_image_message(self, topic: str, img_msg):
        """Process ROS Image message — inject into FrameStore or dispatch."""
        frame_data = bytes(img_msg.data)

        # Inject into FrameStore if available
        if self._frame_store:
            try:
                self._frame_store.put_frame(
                    user_id=f'ros:{topic}',
                    frame_data=frame_data,
                )
            except Exception as e:
                logger.debug(f"ROS bridge: FrameStore inject failed: {e}")

        # Also dispatch as Message with metadata
        self._dispatch_ros_message(
            topic,
            text=f'image:{img_msg.width}x{img_msg.height}:{img_msg.encoding}',
            raw={
                'width': img_msg.width,
                'height': img_msg.height,
                'encoding': img_msg.encoding,
                'step': img_msg.step,
                'frame_size': len(frame_data),
            },
        )

    def _dispatch_ros_message(self, topic: str, text: str,
                              raw: Optional[Dict] = None):
        """Create Message from ROS data and dispatch to handlers."""
        msg = Message(
            id=str(uuid.uuid4())[:8],
            channel='ros',
            sender_id=f'ros:{topic}',
            sender_name=f'ROS {topic}',
            chat_id=topic,
            text=text,
            raw=raw or {'topic': topic},
        )

        for handler in self._message_handlers:
            try:
                handler(msg)
            except Exception as e:
                logger.error(f"ROS handler error: {e}")

    def _spin_loop(self):
        """Background thread: spin ROS 2 node to process callbacks."""
        try:
            import rclpy
            while self._running and rclpy.ok() and self._node:
                rclpy.spin_once(self._node, timeout_sec=0.1)
        except Exception as e:
            if self._running:
                logger.error(f"ROS spin loop error: {e}")


def _parse_topic_list(env_value: str) -> List[str]:
    """Parse comma-separated ROS topic names from env var."""
    if not env_value:
        return ['/hyve/input']
    return [t.strip() for t in env_value.split(',') if t.strip()]


def _is_image_topic(topic: str) -> bool:
    """Heuristic: detect if a topic carries Image messages."""
    image_keywords = ['image', 'camera', 'rgb', 'depth', 'frame']
    topic_lower = topic.lower()
    return any(kw in topic_lower for kw in image_keywords)
