"""
SensorStore — Thread-safe multi-modal sensor data store.

Follows the FrameStore pattern (RLock, per-key bounded deques, TTL).
Stores SensorReadings from all modalities: IMU, GPS, LiDAR, encoders,
force/torque, proximity, temperature, etc.

HevolveAI's world model operates in one latent space — this store is the
buffer between hardware adapters and the WorldModelBridge that feeds
sensor data into that unified latent space.

Usage:
    from integrations.robotics.sensor_store import SensorStore
    store = SensorStore()
    store.put_reading(SensorReading(sensor_id='imu_0', sensor_type='imu',
                                     data={'accel_x': 0.1, 'accel_y': -9.8}))
    latest = store.get_latest('imu_0')
"""
import logging
import threading
import time
from collections import deque
from typing import Dict, List, Optional

from .sensor_model import SensorReading, DEFAULT_TTL

logger = logging.getLogger('hevolve_robotics')

# Singleton
_store = None
_store_lock = threading.Lock()


def get_sensor_store() -> 'SensorStore':
    """Get or create the singleton SensorStore."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SensorStore()
    return _store


class SensorStore:
    """Thread-safe multi-modal sensor data store.

    Per-sensor bounded deque with configurable max entries and TTL.
    Automatic cleanup of expired entries on read.
    """

    def __init__(
        self,
        max_entries_per_sensor: int = 100,
        ttl_overrides: Optional[Dict[str, float]] = None,
    ):
        self._lock = threading.RLock()
        self._max_entries = max_entries_per_sensor
        self._ttl = dict(DEFAULT_TTL)
        if ttl_overrides:
            self._ttl.update(ttl_overrides)
        self._default_ttl = 5.0  # Default for unknown types

        # Per-sensor storage: sensor_id → deque of SensorReading
        self._sensors: Dict[str, deque] = {}
        # Per-sensor counters
        self._counts: Dict[str, int] = {}

    def put_reading(self, reading: SensorReading):
        """Store a sensor reading with auto-cleanup of expired entries."""
        with self._lock:
            sid = reading.sensor_id
            if sid not in self._sensors:
                self._sensors[sid] = deque(maxlen=self._max_entries)
                self._counts[sid] = 0
            self._sensors[sid].append(reading)
            self._counts[sid] = self._counts.get(sid, 0) + 1

    def get_latest(self, sensor_id: str) -> Optional[SensorReading]:
        """Get the latest reading for a sensor if within TTL."""
        with self._lock:
            buf = self._sensors.get(sensor_id)
            if not buf:
                return None
            reading = buf[-1]
            ttl = self._ttl.get(reading.sensor_type, self._default_ttl)
            if time.time() - reading.timestamp > ttl:
                return None  # Expired
            return reading

    def get_window(self, sensor_id: str, duration_sec: float) -> List[SensorReading]:
        """Get all readings within a time window (newest first)."""
        cutoff = time.time() - duration_sec
        with self._lock:
            buf = self._sensors.get(sensor_id)
            if not buf:
                return []
            return [r for r in reversed(buf) if r.timestamp >= cutoff]

    def get_all_latest(self) -> Dict[str, SensorReading]:
        """Snapshot of latest reading per sensor (respecting TTL)."""
        result = {}
        now = time.time()
        with self._lock:
            for sid, buf in self._sensors.items():
                if buf:
                    reading = buf[-1]
                    ttl = self._ttl.get(reading.sensor_type, self._default_ttl)
                    if now - reading.timestamp <= ttl:
                        result[sid] = reading
        return result

    def active_sensors(self) -> List[str]:
        """List sensor IDs with recent (non-expired) readings."""
        return list(self.get_all_latest().keys())

    def has_sensor(self, sensor_id: str) -> bool:
        """Check if a sensor has any readings (expired or not)."""
        with self._lock:
            buf = self._sensors.get(sensor_id)
            return bool(buf)

    def get_ttl(self, sensor_type: str) -> float:
        """Get the TTL for a sensor type."""
        return self._ttl.get(sensor_type, self._default_ttl)

    def set_ttl(self, sensor_type: str, ttl: float):
        """Override TTL for a sensor type."""
        with self._lock:
            self._ttl[sensor_type] = ttl

    def stats(self) -> Dict:
        """Per-sensor statistics: count, rate, staleness, active."""
        now = time.time()
        result = {}
        with self._lock:
            for sid, buf in self._sensors.items():
                if not buf:
                    continue
                latest = buf[-1]
                ttl = self._ttl.get(latest.sensor_type, self._default_ttl)
                staleness = now - latest.timestamp
                # Estimate rate from last N readings
                rate = 0.0
                if len(buf) >= 2:
                    span = buf[-1].timestamp - buf[0].timestamp
                    if span > 0:
                        rate = (len(buf) - 1) / span

                result[sid] = {
                    'sensor_type': latest.sensor_type,
                    'total_count': self._counts.get(sid, 0),
                    'buffered': len(buf),
                    'staleness_sec': round(staleness, 3),
                    'rate_hz': round(rate, 1),
                    'active': staleness <= ttl,
                    'source': latest.source,
                }
        return result

    def clear(self, sensor_id: Optional[str] = None):
        """Clear readings for a specific sensor or all sensors."""
        with self._lock:
            if sensor_id:
                self._sensors.pop(sensor_id, None)
                self._counts.pop(sensor_id, None)
            else:
                self._sensors.clear()
                self._counts.clear()
