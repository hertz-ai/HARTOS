"""
Control Loop Bridge — Timing bridge between agentic and native layers.

NOT a PID controller.  NOT intelligence.  Just timing.

Hevolve-Core owns the actual control loops (PID, motor control, kinematics).
This bridge ensures the LLM-langchain side sends sensor data and receives
feedback at the right cadence.  It's the clock that keeps the agentic
and embodied sides in sync.

Usage:
    from integrations.robotics.control_loop import ControlLoopBridge
    loop = ControlLoopBridge()
    loop.register_callback('imu_0', my_sensor_handler, hz=50)
    loop.start()
"""
import logging
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger('hevolve_robotics')


class ControlLoopBridge:
    """Timing bridge for sensor-action loops.

    Registers callbacks at specified Hz.  Each callback is called on its
    own timer with drift compensation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._callbacks: Dict[str, Dict] = {}  # name → {fn, hz, thread, running}
        self._stats: Dict[str, Dict] = {}      # name → {calls, missed, jitter}

    def register_callback(
        self,
        name: str,
        callback: Callable,
        hz: float = 10.0,
    ):
        """Register a timed callback.

        Args:
            name: Unique callback name (e.g., 'imu_ingestion', 'feedback_poll').
            callback: Function to call at the given frequency.
            hz: Target frequency in Hz.
        """
        with self._lock:
            if name in self._callbacks:
                self.unregister_callback(name)
            self._callbacks[name] = {
                'fn': callback,
                'hz': hz,
                'thread': None,
                'running': False,
            }
            self._stats[name] = {
                'calls': 0,
                'missed_deadlines': 0,
                'total_jitter_ms': 0.0,
                'target_hz': hz,
            }

    def unregister_callback(self, name: str):
        """Stop and remove a callback."""
        with self._lock:
            cb = self._callbacks.get(name)
            if cb:
                cb['running'] = False
                self._callbacks.pop(name, None)

    def start(self):
        """Start all registered callbacks."""
        with self._lock:
            for name, cb in self._callbacks.items():
                if not cb['running']:
                    cb['running'] = True
                    thread = threading.Thread(
                        target=self._loop, args=(name,),
                        name=f'control_loop_{name}', daemon=True,
                    )
                    cb['thread'] = thread
                    thread.start()

    def stop(self):
        """Stop all callbacks."""
        with self._lock:
            for cb in self._callbacks.values():
                cb['running'] = False

    def get_stats(self) -> Dict:
        """Get timing statistics for all callbacks."""
        with self._lock:
            result = {}
            for name, stats in self._stats.items():
                calls = stats['calls']
                result[name] = {
                    **stats,
                    'avg_jitter_ms': (
                        stats['total_jitter_ms'] / calls if calls > 0 else 0
                    ),
                }
            return result

    def _loop(self, name: str):
        """Run a callback at the target Hz with drift compensation."""
        cb = self._callbacks.get(name)
        if not cb:
            return

        interval = 1.0 / cb['hz']
        next_time = time.monotonic() + interval

        while cb.get('running', False):
            now = time.monotonic()

            # Call the callback
            try:
                cb['fn']()
            except Exception as e:
                logger.debug(f"Control loop '{name}' callback error: {e}")

            # Track timing
            elapsed = time.monotonic() - now
            jitter_ms = abs(elapsed - interval) * 1000

            with self._lock:
                stats = self._stats.get(name, {})
                stats['calls'] = stats.get('calls', 0) + 1
                stats['total_jitter_ms'] = stats.get('total_jitter_ms', 0) + jitter_ms
                if elapsed > interval * 1.5:
                    stats['missed_deadlines'] = stats.get('missed_deadlines', 0) + 1

            # Drift-compensated sleep
            next_time += interval
            sleep_time = next_time - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # Fell behind — reset target
                next_time = time.monotonic() + interval
