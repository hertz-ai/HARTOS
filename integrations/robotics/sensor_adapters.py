"""
Sensor Adapters — Bridge existing hardware adapters to SensorStore.

Each adapter converts hardware-specific data into SensorReading objects
and pushes them to the SensorStore.  This is the glue between:
  - GPIO events → proximity/contact readings
  - Serial messages → IMU/GPS/encoder readings
  - ROS 2 topics → any sensor type
  - WAMP IoT topics → any sensor type

Usage:
    from integrations.robotics.sensor_adapters import SerialSensorBridge
    bridge = SerialSensorBridge(
        port='/dev/ttyUSB0',
        mappings=[{
            'line_pattern': 'IMU:(.+),(.+),(.+)',
            'sensor_id': 'imu_0',
            'sensor_type': 'imu',
            'fields': ['accel_x', 'accel_y', 'accel_z'],
        }],
    )
    bridge.start()
"""
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .sensor_model import SensorReading
from .sensor_store import get_sensor_store

logger = logging.getLogger('hevolve_robotics')


class SerialSensorBridge:
    """Parse serial messages into SensorReading objects.

    Configurable via JSON schema mapping — maps line patterns to sensor
    readings with named fields.
    """

    def __init__(
        self,
        port: str = '',
        baudrate: int = 115200,
        mappings: Optional[List[Dict]] = None,
        store=None,
    ):
        self._port = port
        self._baudrate = baudrate
        self._store = store or get_sensor_store()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Compile pattern mappings
        self._mappings = []
        for m in (mappings or []):
            try:
                compiled = re.compile(m.get('line_pattern', ''))
                self._mappings.append({
                    'pattern': compiled,
                    'sensor_id': m.get('sensor_id', 'serial_0'),
                    'sensor_type': m.get('sensor_type', 'imu'),
                    'fields': m.get('fields', []),
                    'frame_id': m.get('frame_id', 'base_link'),
                })
            except re.error as e:
                logger.warning(f"SerialSensorBridge: invalid pattern: {e}")

    def parse_line(self, line: str) -> Optional[SensorReading]:
        """Parse a single serial line into a SensorReading, or None."""
        for mapping in self._mappings:
            match = mapping['pattern'].search(line)
            if match:
                groups = match.groups()
                data = {}
                for i, field_name in enumerate(mapping['fields']):
                    if i < len(groups):
                        try:
                            data[field_name] = float(groups[i])
                        except (ValueError, TypeError):
                            data[field_name] = groups[i]

                return SensorReading(
                    sensor_id=mapping['sensor_id'],
                    sensor_type=mapping['sensor_type'],
                    frame_id=mapping['frame_id'],
                    data=data,
                    source='serial',
                )
        return None

    def start(self):
        """Start reading serial port in a background thread."""
        if self._running or not self._port:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, name=f'serial_sensor_{self._port}', daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def _read_loop(self):
        try:
            import serial
        except ImportError:
            logger.warning("SerialSensorBridge: pyserial not installed")
            return

        while self._running:
            try:
                ser = serial.Serial(self._port, self._baudrate, timeout=0.1)
                while self._running:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        reading = self.parse_line(line)
                        if reading:
                            self._store.put_reading(reading)
                ser.close()
            except Exception as e:
                logger.debug(f"SerialSensorBridge read error: {e}")
                time.sleep(1.0)


class GPIOSensorBridge:
    """Convert GPIO pin state changes to proximity/contact sensor readings."""

    def __init__(
        self,
        pin_mappings: Optional[Dict[int, Dict]] = None,
        store=None,
    ):
        """
        Args:
            pin_mappings: {pin: {'sensor_id': 'prox_0', 'sensor_type': 'proximity',
                                  'active_low': True, 'distance_m': 0.01}}
        """
        self._store = store or get_sensor_store()
        self._pin_mappings = pin_mappings or {}

    def on_pin_change(self, pin: int, value: int):
        """Called when a GPIO pin changes state.

        Typically hooked into the existing GPIOAdapter's event system.
        """
        mapping = self._pin_mappings.get(pin)
        if not mapping:
            return

        active_low = mapping.get('active_low', True)
        is_active = (value == 0) if active_low else (value == 1)

        sensor_type = mapping.get('sensor_type', 'contact')
        data = {}

        if sensor_type == 'proximity':
            data['distance_m'] = mapping.get('distance_m', 0.0) if is_active else 999.0
            data['object_detected'] = is_active
        elif sensor_type == 'contact':
            data['is_contact'] = is_active
            data['force_n'] = mapping.get('force_n', 1.0) if is_active else 0.0
        else:
            data['value'] = value
            data['active'] = is_active

        reading = SensorReading(
            sensor_id=mapping.get('sensor_id', f'gpio_{pin}'),
            sensor_type=sensor_type,
            data=data,
            source='gpio',
        )
        self._store.put_reading(reading)


class ROSSensorBridge:
    """Subscribe to ROS 2 sensor topics and convert to SensorReadings.

    Extends the existing ROSBridgeAdapter pattern for sensor_msgs types:
    Imu, NavSatFix, LaserScan, JointState.
    """

    def __init__(
        self,
        topic_mappings: Optional[Dict[str, Dict]] = None,
        store=None,
    ):
        """
        Args:
            topic_mappings: {'/imu/data': {'sensor_id': 'imu_0', 'sensor_type': 'imu'},
                             '/gps/fix': {'sensor_id': 'gps_0', 'sensor_type': 'gps'}}
        """
        self._store = store or get_sensor_store()
        self._topic_mappings = topic_mappings or {}

    def on_ros_message(self, topic: str, msg_data: Dict):
        """Called when a ROS 2 message is received.

        msg_data should be the deserialized message as a dict.
        """
        mapping = self._topic_mappings.get(topic)
        if not mapping:
            return

        sensor_type = mapping.get('sensor_type', '')
        sensor_id = mapping.get('sensor_id', topic.replace('/', '_').strip('_'))

        data = self._extract_sensor_data(sensor_type, msg_data)
        if data is None:
            return

        reading = SensorReading(
            sensor_id=sensor_id,
            sensor_type=sensor_type,
            frame_id=msg_data.get('header', {}).get('frame_id', 'base_link'),
            data=data,
            source='ros',
        )
        self._store.put_reading(reading)

    def _extract_sensor_data(self, sensor_type: str, msg: Dict) -> Optional[Dict]:
        """Extract sensor-specific data from a ROS message dict."""
        if sensor_type == 'imu':
            lin = msg.get('linear_acceleration', {})
            ang = msg.get('angular_velocity', {})
            return {
                'accel_x': lin.get('x', 0), 'accel_y': lin.get('y', 0),
                'accel_z': lin.get('z', 0),
                'gyro_x': ang.get('x', 0), 'gyro_y': ang.get('y', 0),
                'gyro_z': ang.get('z', 0),
            }
        elif sensor_type == 'gps':
            return {
                'latitude': msg.get('latitude', 0),
                'longitude': msg.get('longitude', 0),
                'altitude': msg.get('altitude', 0),
            }
        elif sensor_type == 'lidar':
            return {
                'ranges': msg.get('ranges', []),
                'angle_min': msg.get('angle_min', 0),
                'angle_max': msg.get('angle_max', 0),
                'angle_increment': msg.get('angle_increment', 0),
            }
        elif sensor_type == 'encoder':
            positions = msg.get('position', [])
            velocities = msg.get('velocity', [])
            return {
                'position_ticks': positions[0] if positions else 0,
                'velocity_ticks_per_sec': velocities[0] if velocities else 0,
            }
        return msg  # Pass through for unknown types


class WAMPSensorBridge:
    """Listen on WAMP topics for IoT sensor payloads."""

    def __init__(
        self,
        topic_mappings: Optional[Dict[str, Dict]] = None,
        store=None,
    ):
        """
        Args:
            topic_mappings: {'com.hart.sensors.imu': {'sensor_id': 'imu_0', 'sensor_type': 'imu'}}
        """
        self._store = store or get_sensor_store()
        self._topic_mappings = topic_mappings or {}

    def on_wamp_event(self, topic: str, payload: Dict):
        """Called when a WAMP event is received on a sensor topic."""
        mapping = self._topic_mappings.get(topic)
        if not mapping:
            return

        reading = SensorReading(
            sensor_id=mapping.get('sensor_id', topic.split('.')[-1]),
            sensor_type=mapping.get('sensor_type', 'unknown'),
            data=payload.get('data', payload),
            quality=payload.get('quality', 1.0),
            source='wamp',
        )
        self._store.put_reading(reading)
