"""
Tests for integrations.robotics.sensor_model, sensor_store, sensor_adapters.

Covers:
  - SensorReading creation, validation, serialization
  - SensorStore: put, get_latest, get_window, TTL expiry, thread safety
  - SerialSensorBridge: line parsing
  - GPIOSensorBridge: pin change to reading
  - ROSSensorBridge: ROS message to reading
  - WAMPSensorBridge: WAMP event to reading
  - system_requirements new fields (has_imu, has_gps, has_lidar)
"""
import json
import threading
import time
import pytest


# ── SensorReading Tests ─────────────────────────────────────────

class TestSensorReading:
    def test_create_imu_reading(self):
        from integrations.robotics.sensor_model import SensorReading
        r = SensorReading(
            sensor_id='imu_0', sensor_type='imu',
            data={'accel_x': 0.1, 'accel_y': -9.8, 'accel_z': 0.0},
        )
        assert r.sensor_id == 'imu_0'
        assert r.sensor_type == 'imu'
        assert r.data['accel_y'] == -9.8
        assert r.quality == 1.0
        assert r.source == 'local'

    def test_create_gps_reading(self):
        from integrations.robotics.sensor_model import SensorReading
        r = SensorReading(
            sensor_id='gps_0', sensor_type='gps',
            data={'latitude': 37.7749, 'longitude': -122.4194, 'altitude': 10.0},
        )
        assert r.data['latitude'] == 37.7749

    def test_create_encoder_reading(self):
        from integrations.robotics.sensor_model import SensorReading
        r = SensorReading(
            sensor_id='encoder_left', sensor_type='encoder',
            data={'position_ticks': 1234, 'velocity_ticks_per_sec': 50},
        )
        assert r.data['position_ticks'] == 1234

    def test_to_dict(self):
        from integrations.robotics.sensor_model import SensorReading
        r = SensorReading(
            sensor_id='imu_0', sensor_type='imu', timestamp=1000.0,
            data={'accel_x': 0.5}, covariance=[0.01, 0.0, 0.0, 0.01],
        )
        d = r.to_dict()
        assert d['sensor_id'] == 'imu_0'
        assert d['covariance'] == [0.01, 0.0, 0.0, 0.01]
        assert 'timestamp' in d

    def test_from_dict(self):
        from integrations.robotics.sensor_model import SensorReading
        d = {
            'sensor_id': 'gps_0', 'sensor_type': 'gps',
            'data': {'latitude': 40.0, 'longitude': -74.0},
            'quality': 0.9,
        }
        r = SensorReading.from_dict(d)
        assert r.sensor_id == 'gps_0'
        assert r.quality == 0.9

    def test_to_dict_omits_none_covariance(self):
        from integrations.robotics.sensor_model import SensorReading
        r = SensorReading(sensor_id='x', sensor_type='imu', data={})
        d = r.to_dict()
        assert 'covariance' not in d


class TestValidateReading:
    def test_valid_imu(self):
        from integrations.robotics.sensor_model import SensorReading, validate_reading
        r = SensorReading(sensor_id='imu_0', sensor_type='imu', data={'accel_x': 1.0})
        assert validate_reading(r) is True

    def test_valid_gps_with_required_fields(self):
        from integrations.robotics.sensor_model import SensorReading, validate_reading
        r = SensorReading(
            sensor_id='gps_0', sensor_type='gps',
            data={'latitude': 0.0, 'longitude': 0.0},
        )
        assert validate_reading(r) is True

    def test_invalid_gps_missing_required(self):
        from integrations.robotics.sensor_model import SensorReading, validate_reading
        r = SensorReading(sensor_id='gps_0', sensor_type='gps', data={'altitude': 100})
        assert validate_reading(r) is False

    def test_invalid_empty_sensor_id(self):
        from integrations.robotics.sensor_model import SensorReading, validate_reading
        r = SensorReading(sensor_id='', sensor_type='imu', data={})
        assert validate_reading(r) is False

    def test_invalid_quality_out_of_range(self):
        from integrations.robotics.sensor_model import SensorReading, validate_reading
        r = SensorReading(sensor_id='x', sensor_type='imu', data={}, quality=1.5)
        assert validate_reading(r) is False

    def test_unknown_type_is_valid(self):
        from integrations.robotics.sensor_model import SensorReading, validate_reading
        r = SensorReading(sensor_id='x', sensor_type='custom_sensor', data={'foo': 1})
        assert validate_reading(r) is True


# ── SensorStore Tests ───────────────────────────────────────────

class TestSensorStore:
    @pytest.fixture
    def store(self):
        from integrations.robotics.sensor_store import SensorStore
        return SensorStore(max_entries_per_sensor=10)

    def test_put_and_get_latest(self, store):
        from integrations.robotics.sensor_model import SensorReading
        r = SensorReading(sensor_id='imu_0', sensor_type='imu', data={'accel_x': 1.0})
        store.put_reading(r)
        latest = store.get_latest('imu_0')
        assert latest is not None
        assert latest.data['accel_x'] == 1.0

    def test_get_latest_returns_none_for_unknown(self, store):
        assert store.get_latest('nonexistent') is None

    def test_ttl_expiry(self, store):
        from integrations.robotics.sensor_model import SensorReading
        # Create a reading that's already expired
        r = SensorReading(
            sensor_id='imu_0', sensor_type='imu',
            timestamp=time.time() - 100,  # 100 seconds ago
            data={'accel_x': 1.0},
        )
        store.put_reading(r)
        assert store.get_latest('imu_0') is None  # Expired (IMU TTL = 0.5s)

    def test_get_window(self, store):
        from integrations.robotics.sensor_model import SensorReading
        now = time.time()
        for i in range(5):
            r = SensorReading(
                sensor_id='enc_0', sensor_type='encoder',
                timestamp=now - 0.1 * i,
                data={'position_ticks': i},
            )
            store.put_reading(r)
        window = store.get_window('enc_0', duration_sec=0.3)
        assert len(window) >= 3

    def test_get_window_empty(self, store):
        assert store.get_window('nope', 1.0) == []

    def test_bounded_deque(self, store):
        from integrations.robotics.sensor_model import SensorReading
        for i in range(20):
            r = SensorReading(sensor_id='s', sensor_type='imu', data={'v': i})
            store.put_reading(r)
        # Max entries is 10
        assert len(store._sensors['s']) == 10

    def test_get_all_latest(self, store):
        from integrations.robotics.sensor_model import SensorReading
        store.put_reading(SensorReading(sensor_id='a', sensor_type='imu', data={}))
        store.put_reading(SensorReading(sensor_id='b', sensor_type='gps',
                                         data={'latitude': 0, 'longitude': 0}))
        all_latest = store.get_all_latest()
        assert 'a' in all_latest
        assert 'b' in all_latest

    def test_active_sensors(self, store):
        from integrations.robotics.sensor_model import SensorReading
        store.put_reading(SensorReading(sensor_id='x', sensor_type='temperature', data={'celsius': 25}))
        active = store.active_sensors()
        assert 'x' in active

    def test_has_sensor(self, store):
        from integrations.robotics.sensor_model import SensorReading
        assert not store.has_sensor('imu_0')
        store.put_reading(SensorReading(sensor_id='imu_0', sensor_type='imu', data={}))
        assert store.has_sensor('imu_0')

    def test_set_ttl(self, store):
        store.set_ttl('imu', 10.0)
        assert store.get_ttl('imu') == 10.0

    def test_stats(self, store):
        from integrations.robotics.sensor_model import SensorReading
        for i in range(5):
            store.put_reading(SensorReading(sensor_id='s', sensor_type='temperature',
                                             data={'celsius': 20 + i}))
        stats = store.stats()
        assert 's' in stats
        assert stats['s']['total_count'] == 5
        assert stats['s']['sensor_type'] == 'temperature'

    def test_clear_specific_sensor(self, store):
        from integrations.robotics.sensor_model import SensorReading
        store.put_reading(SensorReading(sensor_id='a', sensor_type='imu', data={}))
        store.put_reading(SensorReading(sensor_id='b', sensor_type='gps',
                                         data={'latitude': 0, 'longitude': 0}))
        store.clear('a')
        assert not store.has_sensor('a')
        assert store.has_sensor('b')

    def test_clear_all(self, store):
        from integrations.robotics.sensor_model import SensorReading
        store.put_reading(SensorReading(sensor_id='a', sensor_type='imu', data={}))
        store.clear()
        assert store.stats() == {}

    def test_thread_safety(self, store):
        """Concurrent put/get from multiple threads."""
        from integrations.robotics.sensor_model import SensorReading
        errors = []

        def writer(sid):
            try:
                for i in range(50):
                    store.put_reading(SensorReading(
                        sensor_id=sid, sensor_type='encoder',
                        data={'position_ticks': i},
                    ))
            except Exception as e:
                errors.append(e)

        def reader(sid):
            try:
                for _ in range(50):
                    store.get_latest(sid)
                    store.get_window(sid, 0.5)
            except Exception as e:
                errors.append(e)

        threads = []
        for sid in ['s0', 's1', 's2']:
            threads.append(threading.Thread(target=writer, args=(sid,)))
            threads.append(threading.Thread(target=reader, args=(sid,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0


# ── SerialSensorBridge Tests ────────────────────────────────────

class TestSerialSensorBridge:
    def test_parse_imu_line(self):
        from integrations.robotics.sensor_adapters import SerialSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = SerialSensorBridge(
            mappings=[{
                'line_pattern': r'IMU:(.+),(.+),(.+)',
                'sensor_id': 'imu_0',
                'sensor_type': 'imu',
                'fields': ['accel_x', 'accel_y', 'accel_z'],
            }],
            store=store,
        )
        reading = bridge.parse_line('IMU:0.1,-9.8,0.03')
        assert reading is not None
        assert reading.sensor_id == 'imu_0'
        assert reading.data['accel_y'] == -9.8
        assert reading.source == 'serial'

    def test_parse_no_match(self):
        from integrations.robotics.sensor_adapters import SerialSensorBridge
        bridge = SerialSensorBridge(mappings=[{
            'line_pattern': r'GPS:(.+),(.+)',
            'sensor_id': 'gps_0',
            'sensor_type': 'gps',
            'fields': ['latitude', 'longitude'],
        }])
        assert bridge.parse_line('RANDOM NOISE') is None

    def test_parse_gps_line(self):
        from integrations.robotics.sensor_adapters import SerialSensorBridge
        bridge = SerialSensorBridge(mappings=[{
            'line_pattern': r'GPS:(.+),(.+)',
            'sensor_id': 'gps_0',
            'sensor_type': 'gps',
            'fields': ['latitude', 'longitude'],
        }])
        reading = bridge.parse_line('GPS:37.7749,-122.4194')
        assert reading is not None
        assert reading.data['latitude'] == 37.7749


# ── GPIOSensorBridge Tests ──────────────────────────────────────

class TestGPIOSensorBridge:
    def test_active_low_contact(self):
        from integrations.robotics.sensor_adapters import GPIOSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = GPIOSensorBridge(
            pin_mappings={
                17: {'sensor_id': 'contact_0', 'sensor_type': 'contact',
                     'active_low': True, 'force_n': 2.0},
            },
            store=store,
        )
        bridge.on_pin_change(17, 0)  # LOW = active
        latest = store.get_latest('contact_0')
        assert latest is not None
        assert latest.data['is_contact'] is True
        assert latest.data['force_n'] == 2.0

    def test_active_low_release(self):
        from integrations.robotics.sensor_adapters import GPIOSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = GPIOSensorBridge(
            pin_mappings={17: {'sensor_id': 'c0', 'sensor_type': 'contact', 'active_low': True}},
            store=store,
        )
        bridge.on_pin_change(17, 1)  # HIGH = not active
        latest = store.get_latest('c0')
        assert latest.data['is_contact'] is False

    def test_proximity_sensor(self):
        from integrations.robotics.sensor_adapters import GPIOSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = GPIOSensorBridge(
            pin_mappings={
                27: {'sensor_id': 'prox_0', 'sensor_type': 'proximity',
                     'active_low': True, 'distance_m': 0.05},
            },
            store=store,
        )
        bridge.on_pin_change(27, 0)  # Object detected
        latest = store.get_latest('prox_0')
        assert latest.data['object_detected'] is True
        assert latest.data['distance_m'] == 0.05

    def test_unmapped_pin_ignored(self):
        from integrations.robotics.sensor_adapters import GPIOSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = GPIOSensorBridge(pin_mappings={}, store=store)
        bridge.on_pin_change(99, 0)  # No mapping
        assert store.stats() == {}


# ── ROSSensorBridge Tests ───────────────────────────────────────

class TestROSSensorBridge:
    def test_imu_message(self):
        from integrations.robotics.sensor_adapters import ROSSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = ROSSensorBridge(
            topic_mappings={'/imu/data': {'sensor_id': 'imu_0', 'sensor_type': 'imu'}},
            store=store,
        )
        bridge.on_ros_message('/imu/data', {
            'header': {'frame_id': 'imu_link'},
            'linear_acceleration': {'x': 0.1, 'y': -9.8, 'z': 0.0},
            'angular_velocity': {'x': 0.01, 'y': 0.0, 'z': -0.02},
        })
        latest = store.get_latest('imu_0')
        assert latest is not None
        assert latest.data['accel_y'] == -9.8
        assert latest.frame_id == 'imu_link'

    def test_gps_message(self):
        from integrations.robotics.sensor_adapters import ROSSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = ROSSensorBridge(
            topic_mappings={'/gps/fix': {'sensor_id': 'gps_0', 'sensor_type': 'gps'}},
            store=store,
        )
        bridge.on_ros_message('/gps/fix', {
            'latitude': 37.7749, 'longitude': -122.4194, 'altitude': 5.0,
        })
        latest = store.get_latest('gps_0')
        assert latest.data['latitude'] == 37.7749

    def test_unmapped_topic_ignored(self):
        from integrations.robotics.sensor_adapters import ROSSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = ROSSensorBridge(topic_mappings={}, store=store)
        bridge.on_ros_message('/unknown', {'data': 1})
        assert store.stats() == {}


# ── WAMPSensorBridge Tests ──────────────────────────────────────

class TestWAMPSensorBridge:
    def test_wamp_event_to_reading(self):
        from integrations.robotics.sensor_adapters import WAMPSensorBridge
        from integrations.robotics.sensor_store import SensorStore
        store = SensorStore()
        bridge = WAMPSensorBridge(
            topic_mappings={
                'com.hart.sensors.temp': {
                    'sensor_id': 'temp_0', 'sensor_type': 'temperature',
                },
            },
            store=store,
        )
        bridge.on_wamp_event('com.hart.sensors.temp', {
            'data': {'celsius': 22.5}, 'quality': 0.95,
        })
        latest = store.get_latest('temp_0')
        assert latest is not None
        assert latest.data['celsius'] == 22.5
        assert latest.quality == 0.95
        assert latest.source == 'wamp'


# ── System Requirements New Fields Tests ────────────────────────

class TestSystemRequirementsNewFields:
    def test_hardware_profile_has_imu_gps_lidar(self):
        from security.system_requirements import HardwareProfile
        hw = HardwareProfile()
        assert hasattr(hw, 'has_imu')
        assert hasattr(hw, 'has_gps')
        assert hasattr(hw, 'has_lidar')
        assert hw.has_imu is False
        assert hw.has_gps is False
        assert hw.has_lidar is False

    def test_to_dict_includes_new_fields(self):
        from security.system_requirements import HardwareProfile
        hw = HardwareProfile(has_imu=True, has_gps=True, has_lidar=False)
        d = hw.to_dict()
        assert d['has_imu'] is True
        assert d['has_gps'] is True
        assert d['has_lidar'] is False

    def test_sensor_fusion_in_feature_map(self):
        from security.system_requirements import FEATURE_TIER_MAP
        assert 'sensor_fusion' in FEATURE_TIER_MAP
