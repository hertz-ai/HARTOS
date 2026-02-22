"""
Sensor Data Model — Unified representation for all sensor types.

Every sensor reading is a SensorReading dataclass with timestamps,
frame IDs, covariance, and type-specific data.  This is the format
that flows through SensorStore → WorldModelBridge → HevolveAI.

HevolveAI's world model operates in one latent space: text, sensors,
motors are all representations of the same world.  This model ensures
sensor data is structured consistently for that unified space.

Supported sensor types:
    imu, gps, encoder, force_torque, proximity, temperature,
    camera, depth, lidar, contact, battery
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Predefined sensor data schemas ──────────────────────────────

SENSOR_SCHEMAS = {
    'imu': {
        'required': [],
        'optional': [
            'accel_x', 'accel_y', 'accel_z',
            'gyro_x', 'gyro_y', 'gyro_z',
            'mag_x', 'mag_y', 'mag_z',
        ],
    },
    'gps': {
        'required': ['latitude', 'longitude'],
        'optional': ['altitude', 'fix_quality', 'hdop', 'num_satellites'],
    },
    'encoder': {
        'required': ['position_ticks'],
        'optional': ['velocity_ticks_per_sec', 'direction'],
    },
    'force_torque': {
        'required': [],
        'optional': ['fx', 'fy', 'fz', 'tx', 'ty', 'tz'],
    },
    'proximity': {
        'required': ['distance_m'],
        'optional': ['angle_rad', 'object_detected'],
    },
    'temperature': {
        'required': ['celsius'],
        'optional': ['sensor_location'],
    },
    'camera': {
        'required': [],
        'optional': ['frame_base64', 'width', 'height', 'encoding', 'description'],
    },
    'depth': {
        'required': [],
        'optional': ['depth_map_base64', 'width', 'height', 'min_depth', 'max_depth'],
    },
    'lidar': {
        'required': [],
        'optional': [
            'ranges', 'angle_min', 'angle_max', 'angle_increment',
            'range_min', 'range_max',
        ],
    },
    'contact': {
        'required': ['is_contact'],
        'optional': ['force_n', 'location'],
    },
    'battery': {
        'required': ['voltage'],
        'optional': ['current_a', 'percentage', 'temperature_c', 'charging'],
    },
}

# Default TTL per sensor type (seconds)
DEFAULT_TTL = {
    'imu': 0.5,
    'gps': 5.0,
    'encoder': 0.2,
    'force_torque': 0.5,
    'proximity': 1.0,
    'temperature': 30.0,
    'camera': 2.0,
    'depth': 2.0,
    'lidar': 1.0,
    'contact': 0.5,
    'battery': 60.0,
}


@dataclass
class SensorReading:
    """Universal sensor data format.

    This is the atom of sensor data that flows through the system:
    SensorStore → WorldModelBridge → HevolveAI latent space.
    """
    sensor_id: str              # e.g., 'imu_0', 'gps_0', 'lidar_front'
    sensor_type: str            # One of SENSOR_SCHEMAS keys
    timestamp: float = field(default_factory=time.time)
    frame_id: str = 'base_link'  # Coordinate frame reference
    data: Dict[str, Any] = field(default_factory=dict)
    covariance: Optional[List[float]] = None   # Flattened uncertainty matrix
    quality: float = 1.0        # 0.0-1.0, signal quality metric
    source: str = 'local'       # 'local', 'ros', 'serial', 'gpio', 'wamp'

    def to_dict(self) -> Dict:
        """Serialize to dict for JSON/gossip transport."""
        d = {
            'sensor_id': self.sensor_id,
            'sensor_type': self.sensor_type,
            'timestamp': self.timestamp,
            'frame_id': self.frame_id,
            'data': self.data,
            'quality': self.quality,
            'source': self.source,
        }
        if self.covariance is not None:
            d['covariance'] = self.covariance
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> 'SensorReading':
        """Deserialize from dict."""
        return cls(
            sensor_id=d['sensor_id'],
            sensor_type=d['sensor_type'],
            timestamp=d.get('timestamp', time.time()),
            frame_id=d.get('frame_id', 'base_link'),
            data=d.get('data', {}),
            covariance=d.get('covariance'),
            quality=d.get('quality', 1.0),
            source=d.get('source', 'local'),
        )


def validate_reading(reading: SensorReading) -> bool:
    """Validate a SensorReading against its type schema.

    Returns True if the reading has valid structure.
    Lenient: unknown sensor types are accepted (extensible).
    """
    if not reading.sensor_id or not reading.sensor_type:
        return False

    if not 0.0 <= reading.quality <= 1.0:
        return False

    if reading.timestamp <= 0:
        return False

    schema = SENSOR_SCHEMAS.get(reading.sensor_type)
    if schema is None:
        return True  # Unknown types are valid (extensible)

    # Check required fields
    for req in schema.get('required', []):
        if req not in reading.data:
            return False

    return True
