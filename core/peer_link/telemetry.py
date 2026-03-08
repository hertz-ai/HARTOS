"""
Telemetry & Safety — Crossbar connection for central monitoring.

NOT optional. Three functions:
  1. Telemetry: node -> central (traffic stats, compute metrics, health)
  2. Control:   central -> nodes (upgrades, bans, emergency halt)
  3. Probe:     central <-> node (diagnostics RPC)

Central sees METADATA only — never message content.
  - Traffic volume per channel (msg count, byte count)
  - Peer connection topology
  - GPU/compute utilization
  - Economic flows (Spark earned/spent)
  - Security events (guardrail violations, integrity status)
  NOT message content — NEVER
  NOT user prompts/responses — NEVER
  NOT PeerLink E2E payloads — NEVER

Safety: kill switch delivery via Crossbar (instant) + gossip (backup).
  Master key signed emergency_halt -> HiveCircuitBreaker.trip()
  Node disconnected >24h -> self-restrict (safety measure).
"""
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.peer_link')

# Disconnection thresholds (seconds)
_DEGRADED_THRESHOLD = 3600     # 1 hour -> degraded mode
_RESTRICTED_THRESHOLD = 86400  # 24 hours -> restricted mode


class TelemetryCollector:
    """Collects traffic and compute metrics for central reporting.

    Aggregates per-channel message counts and byte totals.
    Reset after each telemetry publish cycle.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._traffic: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {'sent': 0, 'recv': 0, 'bytes_sent': 0, 'bytes_recv': 0})
        self._security_events: List[dict] = []

    def record_sent(self, channel: str, byte_count: int):
        with self._lock:
            self._traffic[channel]['sent'] += 1
            self._traffic[channel]['bytes_sent'] += byte_count

    def record_received(self, channel: str, byte_count: int):
        with self._lock:
            self._traffic[channel]['recv'] += 1
            self._traffic[channel]['bytes_recv'] += byte_count

    def record_security_event(self, event_type: str, details: str = ''):
        with self._lock:
            self._security_events.append({
                'type': event_type,
                'details': details[:200],
                'timestamp': time.time(),
            })
            # Keep only last 100 events
            if len(self._security_events) > 100:
                self._security_events = self._security_events[-100:]

    def get_summary(self) -> dict:
        """Get traffic summary and reset counters."""
        with self._lock:
            traffic = dict(self._traffic)
            self._traffic = defaultdict(
                lambda: {'sent': 0, 'recv': 0, 'bytes_sent': 0, 'bytes_recv': 0})
            events = list(self._security_events)
            self._security_events.clear()

        return {
            'traffic': {k: dict(v) for k, v in traffic.items()},
            'security_events': events,
        }


class CentralConnection:
    """Always-on connection to central Crossbar for telemetry and safety.

    Publishes telemetry every 60 seconds.
    Subscribes to control broadcast for emergency halt, peer bans, etc.
    Responds to diagnostic probes from central.
    Tracks disconnection duration for self-restriction.
    """

    def __init__(self):
        self._crossbar_url = os.environ.get(
            'CBURL', 'ws://aws_rasa.hertzai.com:8088/ws')
        self._realm = os.environ.get('CBREALM', 'realm1')
        self._connected = False
        self._disconnected_since: Optional[float] = None
        self._lock = threading.Lock()
        self._telemetry = TelemetryCollector()
        self._control_handlers: List[Callable] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._telemetry_interval = int(os.environ.get(
            'HEVOLVE_TELEMETRY_INTERVAL', '60'))
        self._node_id = ''

    @property
    def telemetry(self) -> TelemetryCollector:
        return self._telemetry

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self):
        """Start telemetry publishing and control subscription."""
        if self._running:
            return

        # Get node identity
        try:
            from security.node_integrity import get_node_identity
            self._node_id = get_node_identity().get('node_id', 'unknown')
        except Exception:
            self._node_id = 'unknown'

        self._running = True
        self._thread = threading.Thread(
            target=self._telemetry_loop, daemon=True,
            name='peerlink-telemetry')
        self._thread.start()
        logger.info("CentralConnection started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def on_control(self, handler: Callable) -> None:
        """Register handler for control messages from central."""
        self._control_handlers.append(handler)

    def is_degraded(self) -> bool:
        """Node is in degraded mode (>1h disconnected from central)."""
        if self._connected or self._disconnected_since is None:
            return False
        return (time.time() - self._disconnected_since) > _DEGRADED_THRESHOLD

    def is_restricted(self) -> bool:
        """Node is in restricted mode (>24h disconnected from central)."""
        if self._connected or self._disconnected_since is None:
            return False
        return (time.time() - self._disconnected_since) > _RESTRICTED_THRESHOLD

    def get_disconnection_hours(self) -> float:
        if self._connected or self._disconnected_since is None:
            return 0
        return (time.time() - self._disconnected_since) / 3600

    # --- Internal ------------------------------------------------

    def _telemetry_loop(self):
        """Background: publish telemetry, handle control messages."""
        while self._running:
            try:
                self._try_connect()
                if self._connected:
                    self._publish_telemetry()
                    self._check_control_messages()
            except Exception as e:
                logger.debug(f"Telemetry loop error: {e}")
                self._mark_disconnected()

            # Sleep in small increments
            for _ in range(self._telemetry_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _try_connect(self):
        """Check if any outbound transport is available (WAMP or HTTP)."""
        if self._connected:
            return

        # Check WAMP session
        try:
            from crossbar_server import wamp_session
            if wamp_session is not None:
                self._connected = True
                self._disconnected_since = None
                return
        except ImportError:
            pass

        # Check if MessageBus has HTTP transport injected
        try:
            from core.peer_link.message_bus import get_message_bus
            bus = get_message_bus()
            if bus._http_transport is not None:
                self._connected = True
                self._disconnected_since = None
                return
        except Exception:
            pass

        self._mark_disconnected()

    def _mark_disconnected(self):
        if self._connected:
            self._connected = False
            self._disconnected_since = time.time()

    def _publish_telemetry(self):
        """Publish node telemetry to central (metadata only, never content)."""
        summary = self._telemetry.get_summary()

        telemetry = {
            'node_id': self._node_id,
            'timestamp': time.time(),
            'traffic': summary.get('traffic', {}),
            'security_events': summary.get('security_events', []),
        }

        # Add compute metrics
        try:
            from integrations.service_tools.vram_manager import detect_gpu
            gpu = detect_gpu()
            telemetry['compute'] = {
                'gpu_available': gpu.get('available', False),
                'gpu_name': gpu.get('device_name', ''),
                'vram_free_mb': gpu.get('vram_free_mb', 0),
            }
        except Exception:
            telemetry['compute'] = {}

        # Add peer link stats
        try:
            from core.peer_link.link_manager import get_link_manager
            mgr = get_link_manager()
            status = mgr.get_status()
            telemetry['peer_links'] = {
                'active': status.get('active_links', 0),
                'encrypted': status.get('encrypted_links', 0),
                'total': status.get('total_links', 0),
            }
        except Exception:
            telemetry['peer_links'] = {}

        # Add health
        telemetry['health'] = {
            'cpu_count': os.cpu_count() or 1,
        }
        try:
            import psutil
            telemetry['health']['cpu_percent'] = psutil.cpu_percent()
            telemetry['health']['memory_percent'] = (
                psutil.virtual_memory().percent)
        except ImportError:
            pass

        # Publish via MessageBus (handles WAMP → HTTP fallback internally)
        # Telemetry is central-only: skip PeerLink (no peer needs our metrics)
        try:
            from core.peer_link.message_bus import get_message_bus
            bus = get_message_bus()
            telemetry['node_id'] = self._node_id
            bus.publish('telemetry.node', telemetry,
                        skip_peerlink=True)
        except Exception:
            self._mark_disconnected()

    def _check_control_messages(self):
        """Check for control messages from central (emergency halt, bans).

        In WAMP mode, control messages arrive via subscription
        (handled in crossbar_server.py @component.on_join).
        Here we handle the HTTP polling fallback.
        """
        pass

    def handle_control_message(self, message: dict):
        """Process a control message from central.

        Called by crossbar_server.py when control broadcast received,
        or by gossip when control message gossip-forwarded.
        """
        msg_type = message.get('type', '')

        # EMERGENCY HALT — requires master key signature
        if msg_type == 'emergency_halt':
            self._handle_emergency_halt(message)
            return

        # PEER BAN
        if msg_type == 'peer_ban':
            self._handle_peer_ban(message)
            return

        # Forward to registered handlers
        for handler in self._control_handlers:
            try:
                handler(message)
            except Exception as e:
                logger.debug(f"Control handler error: {e}")

    def _handle_emergency_halt(self, message: dict):
        """Verify master key signature and trip circuit breaker."""
        signature = message.get('master_signature', '')
        if not signature:
            logger.warning("Emergency halt without signature — IGNORING")
            return

        # Verify against master public key
        try:
            from security.master_key import MASTER_PUBLIC_KEY_HEX
            from security.node_integrity import verify_json_signature

            msg_copy = dict(message)
            sig = msg_copy.pop('master_signature', '')

            if verify_json_signature(MASTER_PUBLIC_KEY_HEX, msg_copy, sig):
                logger.critical(
                    "EMERGENCY HALT: Valid master key signature — "
                    "tripping circuit breaker")
                try:
                    from security.hive_guardrails import HiveCircuitBreaker
                    HiveCircuitBreaker.trip(
                        reason=message.get('reason', 'emergency_halt'))
                except Exception as e:
                    logger.critical(f"Circuit breaker trip failed: {e}")

                # Gossip-forward to peers (backup delivery)
                try:
                    from integrations.social.peer_discovery import gossip
                    gossip.broadcast({
                        'type': 'emergency_halt_relay',
                        'original': message,
                    })
                except Exception:
                    pass
            else:
                logger.warning(
                    "Emergency halt INVALID signature — IGNORING")
        except ImportError:
            logger.error(
                "Cannot verify emergency halt — security modules missing")

    def _handle_peer_ban(self, message: dict):
        """Ban a peer network-wide."""
        banned_node = message.get('node_id', '')
        if not banned_node:
            return

        logger.info(f"Peer ban received: {banned_node[:8]}")

        # Close PeerLink if connected
        try:
            from core.peer_link.link_manager import get_link_manager
            get_link_manager().close_link(banned_node)
        except Exception:
            pass

        # Update DB
        try:
            from integrations.social.models import get_db, PeerNode
            db = get_db()
            try:
                peer = db.query(PeerNode).filter_by(
                    node_id=banned_node).first()
                if peer:
                    peer.integrity_status = 'banned'
                    db.commit()
            finally:
                db.close()
        except Exception:
            pass

        self._telemetry.record_security_event('peer_ban', banned_node)


# --- Singleton ------------------------------------------------

_central: Optional[CentralConnection] = None
_central_lock = threading.Lock()


def get_central_connection() -> CentralConnection:
    global _central
    if _central is None:
        with _central_lock:
            if _central is None:
                _central = CentralConnection()
    return _central
