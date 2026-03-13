"""
Safety Monitor — E-Stop + Operational Domain Enforcement

Safety first.  A robot that moves but has no emergency stop is dangerous.
A GPIO pin going LOW must halt all motor output within 50ms, not wait
for a gossip round.

Design principles:
  1. Safety is local-first.  E-stop does NOT require network, master key,
     or agent approval.
  2. Agents cannot clear E-stop.  Only human operators can.
  3. Every motor command passes through workspace limit checks.
  4. Fleet notification (gossip) is informational, not a gate.

Usage:
    from integrations.robotics.safety_monitor import SafetyMonitor
    monitor = SafetyMonitor()
    monitor.register_estop_pin(17)  # GPIO 17 as hardware E-stop
    monitor.register_workspace_limits({'x': (-1.0, 1.0), 'y': (-0.5, 0.5)})
    monitor.start()
"""
import logging
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_robotics')

# Singleton instance
_monitor = None
_monitor_lock = threading.Lock()


def get_safety_monitor() -> 'SafetyMonitor':
    """Get or create the singleton SafetyMonitor."""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = SafetyMonitor()
    return _monitor


class SafetyMonitor:
    """E-Stop + Operational Domain Monitor.

    Thread-safe.  Runs a dedicated monitor thread at 20Hz for E-stop
    polling when hardware sources are registered.
    """

    MONITOR_HZ = 20  # 50ms poll interval

    def __init__(self):
        self._lock = threading.RLock()
        self._estop_active = False
        self._estop_reason = ''
        self._estop_source = ''
        self._estop_timestamp = 0.0
        self._cleared_by = ''

        # Workspace limits: {axis: (min, max)}
        self._workspace_limits: Dict[str, Tuple[float, float]] = {}
        self._joint_limits: Dict[str, Tuple[float, float]] = {}

        # Registered E-stop sources
        self._estop_gpio_pins: List[int] = []
        self._estop_serial_ports: List[Dict] = []  # [{'port': str, 'pattern': str}]

        # Callbacks for E-stop events
        self._on_estop_callbacks: List[Callable] = []

        # Audit trail
        self._audit_trail: List[Dict] = []
        self._max_audit = 100

        # Monitor thread
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False

    def register_estop_pin(self, pin: int):
        """Register a GPIO pin as hardware E-stop source.

        When the pin reads LOW (pulled to ground), E-stop triggers.
        """
        with self._lock:
            if pin not in self._estop_gpio_pins:
                self._estop_gpio_pins.append(pin)
                logger.info(f"Safety: registered E-stop GPIO pin {pin}")

    def register_estop_serial(self, port: str, trigger_pattern: str = 'ESTOP'):
        """Register a serial port as E-stop source.

        When the trigger pattern is received on the serial port, E-stop triggers.
        """
        with self._lock:
            self._estop_serial_ports.append({
                'port': port,
                'pattern': trigger_pattern,
                'compiled': re.compile(re.escape(trigger_pattern)),
            })
            logger.info(f"Safety: registered E-stop serial {port} (pattern={trigger_pattern})")

    def register_workspace_limits(self, limits: Dict):
        """Define operational domain bounds.

        Args:
            limits: Dict with axis limits and optional joint limits.
                {
                    'x': (-1.0, 1.0),
                    'y': (-0.5, 0.5),
                    'z': (0.0, 1.2),
                    'joint_limits': {'joint_0': (-90, 90), 'joint_1': (0, 180)}
                }
        """
        with self._lock:
            joint_limits = limits.pop('joint_limits', {})
            for axis, bounds in limits.items():
                if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                    self._workspace_limits[axis] = (float(bounds[0]), float(bounds[1]))
            for joint, bounds in joint_limits.items():
                if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                    self._joint_limits[joint] = (float(bounds[0]), float(bounds[1]))
            logger.info(
                f"Safety: workspace limits set — "
                f"axes={list(self._workspace_limits.keys())}, "
                f"joints={list(self._joint_limits.keys())}"
            )

    def on_estop(self, callback: Callable):
        """Register a callback for E-stop events.

        Callback receives (reason: str, source: str).
        """
        with self._lock:
            self._on_estop_callbacks.append(callback)

    def check_position_safe(self, position: Dict) -> bool:
        """Validate a target position against workspace limits.

        Args:
            position: Dict with axis values, e.g. {'x': 0.5, 'y': 0.2, 'z': 0.8}
                      and/or joint values, e.g. {'joint_0': 45.0}

        Returns:
            True if position is within all configured limits.
        """
        if self._estop_active:
            return False

        with self._lock:
            # Check Cartesian workspace limits
            for axis, (lo, hi) in self._workspace_limits.items():
                val = position.get(axis)
                if val is not None:
                    if float(val) < lo or float(val) > hi:
                        logger.warning(
                            f"Safety: position {axis}={val} outside limits [{lo}, {hi}]"
                        )
                        return False

            # Check joint limits
            for joint, (lo, hi) in self._joint_limits.items():
                val = position.get(joint)
                if val is not None:
                    if float(val) < lo or float(val) > hi:
                        logger.warning(
                            f"Safety: joint {joint}={val} outside limits [{lo}, {hi}]"
                        )
                        return False

        return True

    def trigger_estop(self, reason: str, source: str = 'manual'):
        """Trigger emergency stop.  Immediate.  No network dependency.

        Sets HEVOLVE_HALTED env var, fires callbacks, logs to audit trail.
        Fleet notification is informational (best-effort gossip).

        Args:
            reason: Human-readable reason for the E-stop.
            source: Source identifier ('gpio_17', 'serial_/dev/ttyUSB0', 'manual', 'fleet').
        """
        with self._lock:
            if self._estop_active:
                return  # Already stopped

            self._estop_active = True
            self._estop_reason = reason
            self._estop_source = source
            self._estop_timestamp = time.time()
            self._cleared_by = ''

            # Set halt flag immediately
            os.environ['HEVOLVE_HALTED'] = 'true'
            os.environ['HEVOLVE_HALT_REASON'] = f'E-STOP: {reason}'

            audit_entry = {
                'event': 'estop_triggered',
                'reason': reason,
                'source': source,
                'timestamp': self._estop_timestamp,
            }
            self._audit_trail.append(audit_entry)
            if len(self._audit_trail) > self._max_audit:
                self._audit_trail = self._audit_trail[-self._max_audit:]

        logger.critical(f"E-STOP TRIGGERED: {reason} (source={source})")

        # Fire callbacks (outside lock)
        for cb in self._on_estop_callbacks:
            try:
                cb(reason, source)
            except Exception as e:
                logger.error(f"Safety callback error: {e}")

        # Local halt via HiveCircuitBreaker (no master key needed)
        try:
            from security.hive_guardrails import HiveCircuitBreaker
            HiveCircuitBreaker.local_halt(f'E-STOP: {reason}')
        except (ImportError, AttributeError):
            pass

        # Best-effort gossip notification (informational)
        self._gossip_estop(reason, source)

    def clear_estop(self, operator_id: str) -> bool:
        """Clear E-stop.  HUMAN ONLY.  Agents cannot clear.

        Args:
            operator_id: Human operator identifier.  Must not be empty
                         and must not start with 'agent_' or 'bot_'.

        Returns:
            True if E-stop was cleared, False if rejected.
        """
        if not operator_id:
            logger.warning("Safety: E-stop clear rejected — no operator_id")
            return False

        # Reject agent-initiated clears
        lower_id = operator_id.lower()
        if lower_id.startswith(('agent_', 'bot_', 'system_', 'auto_')):
            logger.warning(
                f"Safety: E-stop clear rejected — "
                f"operator '{operator_id}' appears to be an agent, not a human"
            )
            return False

        with self._lock:
            if not self._estop_active:
                return True  # Already clear

            self._estop_active = False
            self._cleared_by = operator_id

            os.environ.pop('HEVOLVE_HALTED', None)
            os.environ.pop('HEVOLVE_HALT_REASON', None)

            audit_entry = {
                'event': 'estop_cleared',
                'operator_id': operator_id,
                'previous_reason': self._estop_reason,
                'timestamp': time.time(),
            }
            self._audit_trail.append(audit_entry)
            if len(self._audit_trail) > self._max_audit:
                self._audit_trail = self._audit_trail[-self._max_audit:]

        logger.info(f"E-STOP CLEARED by operator: {operator_id}")
        return True

    @property
    def is_estopped(self) -> bool:
        """Check if E-stop is currently active."""
        return self._estop_active

    def get_safety_status(self) -> Dict:
        """Get current safety status for dashboard/fleet reporting."""
        with self._lock:
            return {
                'estop_active': self._estop_active,
                'estop_reason': self._estop_reason,
                'estop_source': self._estop_source,
                'estop_timestamp': self._estop_timestamp,
                'cleared_by': self._cleared_by,
                'workspace_limits': dict(self._workspace_limits),
                'joint_limits': dict(self._joint_limits),
                'estop_gpio_pins': list(self._estop_gpio_pins),
                'estop_serial_ports': [
                    {'port': s['port'], 'pattern': s['pattern']}
                    for s in self._estop_serial_ports
                ],
                'audit_trail': list(self._audit_trail[-10:]),
                'monitor_running': self._running,
            }

    def start(self):
        """Start the E-stop monitor thread (20Hz).

        Only starts if GPIO or serial E-stop sources are registered.
        """
        with self._lock:
            if self._running:
                return
            if not self._estop_gpio_pins and not self._estop_serial_ports:
                logger.info("Safety: no E-stop sources registered, monitor not started")
                return
            self._running = True

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name='safety_estop_monitor',
            daemon=True,
        )
        self._monitor_thread.start()
        logger.info(f"Safety: E-stop monitor started at {self.MONITOR_HZ}Hz")

    def stop(self):
        """Stop the E-stop monitor thread."""
        self._running = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=1.0)
        logger.info("Safety: E-stop monitor stopped")

    def _monitor_loop(self):
        """Poll GPIO and serial E-stop sources at MONITOR_HZ."""
        interval = 1.0 / self.MONITOR_HZ
        while self._running:
            try:
                self._check_gpio_estops()
                self._check_serial_estops()
            except Exception as e:
                logger.error(f"Safety monitor error: {e}")
            time.sleep(interval)

    def _check_gpio_estops(self):
        """Check registered GPIO pins for E-stop trigger (LOW = triggered)."""
        if not self._estop_gpio_pins:
            return

        try:
            import gpiod
            chip = gpiod.Chip('gpiochip0')
            for pin in self._estop_gpio_pins:
                try:
                    line = chip.get_line(pin)
                    line.request(consumer='hart_estop', type=gpiod.LINE_REQ_DIR_IN)
                    value = line.get_value()
                    line.release()
                    if value == 0:  # LOW = E-stop triggered
                        self.trigger_estop(
                            f'Hardware E-stop pin {pin} triggered (LOW)',
                            source=f'gpio_{pin}',
                        )
                        return
                except Exception:
                    pass
        except ImportError:
            # Try RPi.GPIO fallback
            try:
                import RPi.GPIO as GPIO
                for pin in self._estop_gpio_pins:
                    try:
                        GPIO.setmode(GPIO.BCM)
                        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                        if GPIO.input(pin) == GPIO.LOW:
                            self.trigger_estop(
                                f'Hardware E-stop pin {pin} triggered (LOW)',
                                source=f'gpio_{pin}',
                            )
                            return
                    except Exception:
                        pass
            except ImportError:
                pass  # No GPIO library available

    def _check_serial_estops(self):
        """Check registered serial ports for E-stop trigger pattern."""
        if not self._estop_serial_ports:
            return

        try:
            import serial
        except ImportError:
            return

        for port_config in self._estop_serial_ports:
            try:
                ser = serial.Serial(
                    port_config['port'], baudrate=9600, timeout=0.01,
                )
                data = ser.read(256)
                ser.close()
                if data:
                    text = data.decode('utf-8', errors='ignore')
                    if port_config['compiled'].search(text):
                        self.trigger_estop(
                            f"Serial E-stop pattern '{port_config['pattern']}' "
                            f"received on {port_config['port']}",
                            source=f"serial_{port_config['port']}",
                        )
                        return
            except Exception:
                pass

    def _gossip_estop(self, reason: str, source: str):
        """Best-effort gossip notification of E-stop event."""
        try:
            from integrations.social.peer_discovery import gossip
            gossip.broadcast({
                'type': 'node_estop',
                'reason': reason,
                'source': source,
                'timestamp': time.time(),
            })
        except Exception:
            pass  # Gossip failure must not block safety
