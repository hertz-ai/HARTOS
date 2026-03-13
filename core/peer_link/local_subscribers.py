"""
Local Crossbar topic subscribers — replaces cloud chatbot_pipeline subscribers.

The cloud chatbot_pipeline (Twisted WAMP ApplicationSession) subscribes to
~10 Crossbar topics for logging, confirmation tracking, error handling, etc.
This module provides local equivalents that work fully offline.

Cloud subscribers replicated:
  confirmation.py → DeliveryTracker     (delivery tracking + local notification)
  longrunning.py  → LongRunningTracker  (task status logging)
  intermediate.py → (inline — just log)
  exception.py    → (inline — just log)
  timeout.py      → (inline — just log)
  actions.py      → (inline — action tracking via DB)
  probe.py        → (inline — health probe response)

Bootstrapped once via bootstrap_local_subscribers().
"""

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional

logger = logging.getLogger('hevolve.local_subscribers')

# ─── Delivery Tracker (replaces cloud confirmation.py) ─────────────────

_DELIVERY_TTL = 60  # seconds before a message is considered unconfirmed
_MAX_PENDING = 500


class DeliveryTracker:
    """Track message delivery and fire local notifications for unconfirmed messages.

    Cloud confirmation.py tracks pending messages and sends FCM push notifications
    if not confirmed within 30s. Locally, we:
    1. Track pending messages with timestamps
    2. After TTL, emit a local notification event (no FCM — that's cloud-only)
    3. Multi-device peers handle their own delivery via PeerLink acks
    """

    def __init__(self):
        self._pending: OrderedDict = OrderedDict()  # msg_id → {topic, timestamp, data}
        self._lock = threading.Lock()
        self._cleanup_thread = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True,
            name='delivery_tracker',
        )
        self._cleanup_thread.start()

    def stop(self):
        self._running = False

    def track(self, topic: str, data: dict):
        """Track a published message for delivery confirmation.

        Keys by request_id (cloud primary key), falls back to msg_id.
        Stores topic_name as secondary info (cloud uses 2-level dict,
        we use flat dict with topic_name inside the value).
        """
        # Match cloud protocol: request_id is primary, msg_id is secondary
        tracking_key = data.get('request_id') or data.get('msg_id', '')
        if not tracking_key:
            return

        with self._lock:
            self._pending[tracking_key] = {
                'topic': topic,
                'topic_name': data.get('topic_name', topic),
                'timestamp': time.time(),
                'user_id': data.get('user_id', ''),
            }
            # Cap size
            while len(self._pending) > _MAX_PENDING:
                self._pending.popitem(last=False)

    def confirm(self, tracking_key: str):
        """Mark a message as delivered (confirmed by frontend)."""
        with self._lock:
            self._pending.pop(tracking_key, None)

    def on_confirmation_message(self, topic: str, data: dict):
        """Handle confirmation topic messages.

        Cloud protocol (confirmation.py):
        - 'confirmation' key PRESENT and False → new unconfirmed message, track it
        - 'confirmation' key ABSENT → confirmed by frontend, pop it

        We handle both cloud and local format:
        - confirmation=False (explicit) → track
        - confirmation=True (explicit) → confirm
        - confirmation absent → confirm (cloud format)
        """
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return

        tracking_key = data.get('request_id') or data.get('msg_id', '')

        if 'confirmation' in data:
            if not data['confirmation']:
                # confirmation=False → new unconfirmed message, track it
                self.track(data.get('topic_name', ''), data)
            elif tracking_key:
                # confirmation=True → confirmed, pop it
                self.confirm(tracking_key)
        elif tracking_key:
            # Key absent → cloud-format confirmation, pop it
            self.confirm(tracking_key)

    def _cleanup_loop(self):
        """Background loop: check for unconfirmed messages and emit notifications."""
        while self._running:
            time.sleep(15)  # Check every 15s
            now = time.time()
            expired = []

            with self._lock:
                for msg_id, info in list(self._pending.items()):
                    if now - info['timestamp'] > _DELIVERY_TTL:
                        expired.append((msg_id, info))

                for msg_id, _ in expired:
                    self._pending.pop(msg_id, None)

            for msg_id, info in expired:
                logger.debug(
                    f"Unconfirmed delivery: {info['topic']} "
                    f"(user={info['user_id']}, age={_DELIVERY_TTL}s)"
                )
                # Emit local notification event for unconfirmed messages
                try:
                    from core.platform.events import emit_event
                    emit_event('notification.unconfirmed', {
                        'msg_id': msg_id,
                        'topic': info['topic'],
                        'user_id': info['user_id'],
                    })
                except Exception:
                    pass

    def get_stats(self) -> dict:
        with self._lock:
            return {'pending': len(self._pending)}


# ─── Long-Running Task Tracker (replaces cloud longrunning.py) ──────────

class LongRunningTracker:
    """Track long-running task status locally.

    Cloud longrunning.py logs task status to Teams webhook + tracks in memory.
    Locally, we just log and track in memory (no Teams webhook needed).
    """

    def __init__(self):
        self._tasks: Dict[str, dict] = {}  # request_id → latest status
        self._lock = threading.Lock()

    def on_progress(self, topic: str, data: dict):
        """Handle longrunning.log messages."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return

        request_id = data.get('request_id', '')
        task_name = data.get('task_name', '')
        status = data.get('status', '')

        if request_id:
            with self._lock:
                self._tasks[request_id] = {
                    'task_name': task_name,
                    'status': status,
                    'timestamp': time.time(),
                    'data': data,
                }

            # Log status transitions
            if status in ('ERROR', 'TIMEOUT'):
                logger.warning(f"Task {task_name} ({request_id}): {status}")
            else:
                logger.debug(f"Task {task_name} ({request_id}): {status}")

        # Clean old entries (keep last 200)
        with self._lock:
            if len(self._tasks) > 200:
                oldest = sorted(self._tasks.items(),
                                key=lambda x: x[1].get('timestamp', 0))
                for rid, _ in oldest[:len(self._tasks) - 200]:
                    del self._tasks[rid]

    def get_task_status(self, request_id: str) -> Optional[dict]:
        with self._lock:
            return self._tasks.get(request_id)

    def get_stats(self) -> dict:
        with self._lock:
            return {'tracked_tasks': len(self._tasks)}


# ─── Global instances (thread-safe, matching MessageBus pattern) ────────

_delivery_tracker: Optional[DeliveryTracker] = None
_longrunning_tracker: Optional[LongRunningTracker] = None
_bootstrapped = False
_singleton_lock = threading.Lock()


def get_delivery_tracker() -> DeliveryTracker:
    global _delivery_tracker
    if _delivery_tracker is None:
        with _singleton_lock:
            if _delivery_tracker is None:
                _delivery_tracker = DeliveryTracker()
    return _delivery_tracker


def get_longrunning_tracker() -> LongRunningTracker:
    global _longrunning_tracker
    if _longrunning_tracker is None:
        with _singleton_lock:
            if _longrunning_tracker is None:
                _longrunning_tracker = LongRunningTracker()
    return _longrunning_tracker


def bootstrap_local_subscribers() -> None:
    """Wire up local subscribers to the MessageBus.

    Call once at startup. Subscribes to all topics that the cloud
    chatbot_pipeline handles, using local handlers.
    """
    global _bootstrapped
    with _singleton_lock:
        if _bootstrapped:
            return
        _bootstrapped = True

    try:
        from core.peer_link.message_bus import get_message_bus
        bus = get_message_bus()
    except Exception as e:
        logger.warning(f"Cannot bootstrap local subscribers (no MessageBus): {e}")
        return

    # 1. Confirmation tracking (replaces cloud confirmation.py)
    tracker = get_delivery_tracker()
    tracker.start()
    bus.subscribe('task.confirmation', tracker.on_confirmation_message)

    # 2. Long-running task tracking (replaces cloud longrunning.py)
    lr = get_longrunning_tracker()
    bus.subscribe('task.progress', lr.on_progress)

    # 3. Intermediate responses — just log (cloud intermediate.py does the same)
    def _on_intermediate(topic, data):
        logger.debug(f"Intermediate response: {str(data)[:200]}")

    bus.subscribe('task.intermediate', _on_intermediate)

    # 4. Exception logging (replaces cloud exception.py)
    def _on_exception(topic, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {'raw': data}
        logger.error(f"Agent exception: {data.get('error', data.get('raw', str(data)[:200]))}")

    bus.subscribe('task.exception', _on_exception)

    # 5. Timeout tracking (replaces cloud timeout.py)
    def _on_timeout(topic, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {'raw': data}
        logger.warning(f"Task timeout: {data.get('task_name', '')} ({data.get('request_id', '')})")

    bus.subscribe('task.timeout', _on_timeout)

    # 6. Health probe response (replaces cloud probe.py)
    def _on_probe(topic, data):
        logger.debug("Health probe received")
        try:
            bus.publish('task.probe_response', {
                'status': 'ok',
                'timestamp': time.time(),
                'delivery': tracker.get_stats(),
                'longrunning': lr.get_stats(),
            }, skip_crossbar=True)
        except Exception:
            pass

    # Probe uses a different topic pattern but subscribe anyway
    bus.subscribe('task.probe', _on_probe)

    logger.info(
        "Local subscribers bootstrapped: confirmation, longrunning, "
        "intermediate, exception, timeout, probe"
    )
