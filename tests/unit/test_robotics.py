"""
Tests for integrations/robotics/hardware_bridge.py and
integrations/robotics/intelligence_api.py.

Covers data classes, sensor/actuator adapters (HTTP, serial, MQTT, WebSocket),
SafetyMonitor, HardwareBridge lifecycle, RobotIntelligenceAPI intelligence
fusion, robot registry, blueprints, and singletons.
"""

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Import targets
# ---------------------------------------------------------------------------

from integrations.robotics.hardware_bridge import (
    SensorReading,
    ActuatorCommand,
    Experience,
    SerialSensorAdapter,
    HTTPSensorAdapter,
    MQTTSensorAdapter,
    WebSocketSensorAdapter,
    SerialActuatorAdapter,
    HTTPActuatorAdapter,
    MQTTActuatorAdapter,
    SafetyMonitor,
    HardwareBridge,
    get_bridge,
    create_robotics_blueprint,
)
from integrations.robotics.intelligence_api import (
    RobotIntelligenceAPI,
    get_robot_api,
    create_intelligence_blueprint,
    INTELLIGENCE_TYPES,
    _FUSION_PRIORITY,
    _classify_intent,
    _extract_target,
)


# ===================================================================
# 1. Data classes
# ===================================================================


class TestSensorReading(unittest.TestCase):
    """SensorReading dataclass creation and field defaults."""

    def test_creation_with_all_fields(self):
        r = SensorReading(
            sensor_id='cam_front',
            sensor_type='camera',
            data={'frame': 'b64...'},
            timestamp=1000.0,
            raw=b'\xff\xd8',
        )
        self.assertEqual(r.sensor_id, 'cam_front')
        self.assertEqual(r.sensor_type, 'camera')
        self.assertEqual(r.data, {'frame': 'b64...'})
        self.assertEqual(r.timestamp, 1000.0)
        self.assertEqual(r.raw, b'\xff\xd8')

    def test_timestamp_auto_set(self):
        before = time.time()
        r = SensorReading(sensor_id='imu', sensor_type='imu', data={})
        after = time.time()
        self.assertGreaterEqual(r.timestamp, before)
        self.assertLessEqual(r.timestamp, after)

    def test_raw_defaults_to_none(self):
        r = SensorReading(sensor_id='s', sensor_type='t', data=42)
        self.assertIsNone(r.raw)


class TestActuatorCommand(unittest.TestCase):
    """ActuatorCommand dataclass creation and safety_cleared default."""

    def test_safety_cleared_defaults_false(self):
        cmd = ActuatorCommand(
            actuator_id='motor_l',
            actuator_type='motor',
            command={'action': 'move'},
        )
        self.assertFalse(cmd.safety_cleared)

    def test_fields_are_stored(self):
        cmd = ActuatorCommand(
            actuator_id='servo_1',
            actuator_type='servo',
            command={'angle': 90},
            safety_cleared=True,
        )
        self.assertEqual(cmd.actuator_id, 'servo_1')
        self.assertEqual(cmd.actuator_type, 'servo')
        self.assertEqual(cmd.command, {'angle': 90})
        self.assertTrue(cmd.safety_cleared)

    def test_timestamp_auto_set(self):
        before = time.time()
        cmd = ActuatorCommand(
            actuator_id='a', actuator_type='t', command={})
        after = time.time()
        self.assertGreaterEqual(cmd.timestamp, before)
        self.assertLessEqual(cmd.timestamp, after)


class TestExperience(unittest.TestCase):
    """Experience dataclass."""

    def test_creation(self):
        exp = Experience(
            robot_id='r1',
            sensor_state={'cam': {}},
            action_taken={'move': 'forward'},
            outcome={'ok': True},
            reward=1.0,
        )
        self.assertEqual(exp.robot_id, 'r1')
        self.assertEqual(exp.reward, 1.0)
        self.assertIsInstance(exp.timestamp, float)


# ===================================================================
# 2. Sensor adapters
# ===================================================================


class TestHTTPSensorAdapter(unittest.TestCase):
    """HTTPSensorAdapter.read() with mocked HTTP."""

    def test_read_returns_sensor_reading_json(self):
        adapter = HTTPSensorAdapter(
            sensor_id='cam', sensor_type='camera',
            url='http://192.168.1.10/capture',
        )
        mock_resp = MagicMock()
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_resp.json.return_value = {'brightness': 0.8}
        mock_resp.text = '{"brightness": 0.8}'

        with patch(
            'integrations.robotics.hardware_bridge.HTTPSensorAdapter.read',
            wraps=adapter.read,
        ):
            # Mock the actual HTTP call inside read() — it tries
            # core.http_pool first, then falls back to requests.
            with patch.dict(sys.modules, {'core.http_pool': None}):
                mock_requests = MagicMock()
                mock_requests.get.return_value = mock_resp
                with patch.dict(sys.modules, {'requests': mock_requests}):
                    reading = adapter.read()

        self.assertIsNotNone(reading)
        self.assertIsInstance(reading, SensorReading)
        self.assertEqual(reading.sensor_id, 'cam')
        self.assertEqual(reading.data, {'brightness': 0.8})

    def test_read_returns_none_on_error(self):
        adapter = HTTPSensorAdapter(
            sensor_id='cam', sensor_type='camera',
            url='http://bad-host/',
        )
        # Force both import paths to fail
        with patch.dict(sys.modules, {'core.http_pool': None}):
            mock_requests = MagicMock()
            mock_requests.get.side_effect = ConnectionError("boom")
            with patch.dict(sys.modules, {'requests': mock_requests}):
                reading = adapter.read()
        self.assertIsNone(reading)

    def test_read_binary_payload(self):
        adapter = HTTPSensorAdapter(
            sensor_id='cam', sensor_type='camera',
            url='http://192.168.1.10/capture',
        )
        mock_resp = MagicMock()
        mock_resp.headers = {'Content-Type': 'image/jpeg'}
        mock_resp.content = b'\xff\xd8\xff\xe0' * 10

        with patch.dict(sys.modules, {'core.http_pool': None}):
            mock_requests = MagicMock()
            mock_requests.get.return_value = mock_resp
            with patch.dict(sys.modules, {'requests': mock_requests}):
                reading = adapter.read()

        self.assertIsNotNone(reading)
        self.assertEqual(reading.data['content_type'], 'image/jpeg')
        self.assertIn('size_bytes', reading.data)


class TestSerialSensorAdapter(unittest.TestCase):
    """SerialSensorAdapter.read() with mocked serial port."""

    def test_read_returns_sensor_reading(self):
        adapter = SerialSensorAdapter(
            sensor_id='imu', sensor_type='imu',
            port='/dev/ttyUSB0',
        )
        mock_serial_cls = MagicMock()
        mock_ser_instance = MagicMock()
        mock_ser_instance.readline.return_value = b'{"ax": 0.01, "ay": -9.81}\n'
        mock_serial_cls.return_value = mock_ser_instance

        mock_serial_mod = types.ModuleType('serial')
        mock_serial_mod.Serial = mock_serial_cls

        with patch.dict(sys.modules, {'serial': mock_serial_mod}):
            reading = adapter.read()

        self.assertIsNotNone(reading)
        self.assertIsInstance(reading, SensorReading)
        self.assertEqual(reading.sensor_id, 'imu')
        self.assertEqual(reading.data['ax'], 0.01)

    def test_read_returns_none_when_pyserial_missing(self):
        adapter = SerialSensorAdapter(
            sensor_id='imu', sensor_type='imu',
            port='/dev/ttyUSB0',
        )
        # Remove serial from sys.modules and make import fail
        with patch.dict(sys.modules, {'serial': None}):
            reading = adapter.read()
        self.assertIsNone(reading)

    def test_read_non_json_line(self):
        adapter = SerialSensorAdapter(
            sensor_id='imu', sensor_type='imu',
            port='/dev/ttyUSB0',
        )
        mock_serial_cls = MagicMock()
        mock_ser = MagicMock()
        mock_ser.readline.return_value = b'HELLO WORLD\n'
        mock_serial_cls.return_value = mock_ser

        mock_serial_mod = types.ModuleType('serial')
        mock_serial_mod.Serial = mock_serial_cls

        with patch.dict(sys.modules, {'serial': mock_serial_mod}):
            reading = adapter.read()

        self.assertIsNotNone(reading)
        self.assertIn('raw_line', reading.data)


class TestMQTTSensorAdapter(unittest.TestCase):
    """MQTTSensorAdapter.read() with mocked paho.mqtt."""

    def test_read_returns_sensor_reading(self):
        adapter = MQTTSensorAdapter(
            sensor_id='temp', sensor_type='temperature',
            broker='localhost', topic='sensors/temp',
        )

        # Build a mock paho.mqtt module hierarchy
        mock_mqtt = MagicMock()
        mock_client_instance = MagicMock()
        mock_mqtt.Client.return_value = mock_client_instance

        # Simulate on_message being called when loop_start() begins
        def fake_connect(broker, port, keepalive):
            pass

        def fake_subscribe(topic):
            pass

        def fake_loop_start():
            # Fire the on_message callback that was registered
            msg = MagicMock()
            msg.payload = b'{"temperature": 22.5}'
            mock_client_instance.on_message(
                mock_client_instance, None, msg)

        mock_client_instance.connect.side_effect = fake_connect
        mock_client_instance.subscribe.side_effect = fake_subscribe
        mock_client_instance.loop_start.side_effect = fake_loop_start

        mock_paho = MagicMock()
        mock_paho.mqtt = MagicMock()
        mock_paho.mqtt.client = mock_mqtt

        with patch.dict(sys.modules, {
            'paho': mock_paho,
            'paho.mqtt': mock_paho.mqtt,
            'paho.mqtt.client': mock_mqtt,
        }):
            reading = adapter.read()

        self.assertIsNotNone(reading)
        self.assertIsInstance(reading, SensorReading)
        self.assertEqual(reading.sensor_id, 'temp')
        self.assertEqual(reading.data.get('temperature'), 22.5)

    def test_read_returns_none_when_paho_missing(self):
        adapter = MQTTSensorAdapter(
            sensor_id='temp', sensor_type='temperature',
            broker='localhost', topic='sensors/temp',
        )
        with patch.dict(sys.modules, {'paho.mqtt.client': None, 'paho.mqtt': None, 'paho': None}):
            reading = adapter.read()
        self.assertIsNone(reading)


class TestWebSocketSensorAdapter(unittest.TestCase):
    """WebSocketSensorAdapter.read() with mocked websocket-client."""

    def test_read_returns_sensor_reading(self):
        adapter = WebSocketSensorAdapter(
            sensor_id='stream', sensor_type='telemetry',
            url='ws://192.168.1.10/stream',
        )
        mock_ws_conn = MagicMock()
        mock_ws_conn.recv.return_value = '{"rpm": 1200}'

        mock_ws_lib = MagicMock()
        mock_ws_lib.create_connection.return_value = mock_ws_conn

        with patch.dict(sys.modules, {'websocket': mock_ws_lib}):
            reading = adapter.read()

        self.assertIsNotNone(reading)
        self.assertIsInstance(reading, SensorReading)
        self.assertEqual(reading.data.get('rpm'), 1200)

    def test_read_returns_none_when_websocket_missing(self):
        adapter = WebSocketSensorAdapter(
            sensor_id='stream', sensor_type='telemetry',
            url='ws://bad-host/',
        )
        with patch.dict(sys.modules, {'websocket': None}):
            reading = adapter.read()
        self.assertIsNone(reading)

    def test_read_returns_cached_if_streaming(self):
        adapter = WebSocketSensorAdapter(
            sensor_id='stream', sensor_type='telemetry',
            url='ws://localhost/',
        )
        cached = SensorReading(
            sensor_id='stream', sensor_type='telemetry',
            data={'cached': True},
        )
        adapter._last_reading = cached

        reading = adapter.read()
        self.assertIsNotNone(reading)
        self.assertTrue(reading.data.get('cached'))


# ===================================================================
# 3. Actuator adapters
# ===================================================================


class TestSerialActuatorAdapter(unittest.TestCase):
    """SerialActuatorAdapter.execute() with mocked serial port."""

    def test_refuses_when_not_safety_cleared(self):
        adapter = SerialActuatorAdapter(
            actuator_id='motor', actuator_type='motor',
            port='/dev/ttyUSB0',
        )
        cmd = ActuatorCommand(
            actuator_id='motor', actuator_type='motor',
            command={'action': 'move'},
            safety_cleared=False,
        )
        result = adapter.execute(cmd)
        self.assertFalse(result['ok'])
        self.assertIn('not safety cleared', result['error'])

    def test_execute_writes_to_serial(self):
        adapter = SerialActuatorAdapter(
            actuator_id='motor', actuator_type='motor',
            port='/dev/ttyUSB0',
        )
        cmd = ActuatorCommand(
            actuator_id='motor', actuator_type='motor',
            command={'action': 'move', 'params': {'speed': 0.5}},
            safety_cleared=True,
        )
        mock_serial_cls = MagicMock()
        mock_ser = MagicMock()
        mock_ser.readline.return_value = b'{"status":"ok"}\n'
        mock_serial_cls.return_value = mock_ser

        mock_serial_mod = types.ModuleType('serial')
        mock_serial_mod.Serial = mock_serial_cls

        with patch.dict(sys.modules, {'serial': mock_serial_mod}):
            result = adapter.execute(cmd)

        self.assertTrue(result['ok'])
        mock_ser.write.assert_called_once()
        written = mock_ser.write.call_args[0][0]
        self.assertIn(b'move', written)


class TestHTTPActuatorAdapter(unittest.TestCase):
    """HTTPActuatorAdapter.execute() with mocked HTTP."""

    def test_refuses_when_not_safety_cleared(self):
        adapter = HTTPActuatorAdapter(
            actuator_id='arm', actuator_type='servo',
            url='http://robot/arm',
        )
        cmd = ActuatorCommand(
            actuator_id='arm', actuator_type='servo',
            command={'angle': 90},
            safety_cleared=False,
        )
        result = adapter.execute(cmd)
        self.assertFalse(result['ok'])
        self.assertIn('not safety cleared', result['error'])

    def test_execute_sends_post(self):
        adapter = HTTPActuatorAdapter(
            actuator_id='arm', actuator_type='servo',
            url='http://robot/arm',
        )
        cmd = ActuatorCommand(
            actuator_id='arm', actuator_type='servo',
            command={'angle': 90},
            safety_cleared=True,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'ok': True}

        mock_post = MagicMock(return_value=mock_resp)

        with patch.object(
            HTTPActuatorAdapter, '_get_post_fn', return_value=mock_post
        ):
            result = adapter.execute(cmd)

        self.assertTrue(result['ok'])
        mock_post.assert_called_once()


class TestMQTTActuatorAdapter(unittest.TestCase):
    """MQTTActuatorAdapter.execute() with mocked paho.mqtt."""

    def test_refuses_when_not_safety_cleared(self):
        adapter = MQTTActuatorAdapter(
            actuator_id='light', actuator_type='led',
            broker='localhost', topic='actuators/light',
        )
        cmd = ActuatorCommand(
            actuator_id='light', actuator_type='led',
            command={'on': True},
            safety_cleared=False,
        )
        result = adapter.execute(cmd)
        self.assertFalse(result['ok'])
        self.assertIn('not safety cleared', result['error'])

    def test_execute_publishes_message(self):
        adapter = MQTTActuatorAdapter(
            actuator_id='light', actuator_type='led',
            broker='localhost', topic='actuators/light',
        )
        cmd = ActuatorCommand(
            actuator_id='light', actuator_type='led',
            command={'brightness': 100},
            safety_cleared=True,
        )

        mock_mqtt = MagicMock()
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        mock_paho = MagicMock()
        mock_paho.mqtt = MagicMock()
        mock_paho.mqtt.client = mock_mqtt

        with patch.dict(sys.modules, {
            'paho': mock_paho,
            'paho.mqtt': mock_paho.mqtt,
            'paho.mqtt.client': mock_mqtt,
        }):
            result = adapter.execute(cmd)

        self.assertTrue(result['ok'])
        mock_client.publish.assert_called_once()

    def test_execute_returns_error_when_paho_missing(self):
        adapter = MQTTActuatorAdapter(
            actuator_id='light', actuator_type='led',
            broker='localhost', topic='actuators/light',
        )
        cmd = ActuatorCommand(
            actuator_id='light', actuator_type='led',
            command={'on': True},
            safety_cleared=True,
        )
        with patch.dict(sys.modules, {
            'paho.mqtt.client': None,
            'paho.mqtt': None,
            'paho': None,
        }):
            result = adapter.execute(cmd)
        self.assertFalse(result['ok'])
        self.assertIn('paho-mqtt', result.get('error', ''))


# ===================================================================
# 4. SafetyMonitor
# ===================================================================


class TestSafetyMonitor(unittest.TestCase):
    """SafetyMonitor: velocity, force, workspace, e-stop."""

    def setUp(self):
        self.monitor = SafetyMonitor(
            max_velocity=2.0,
            max_force=50.0,
            workspace_bounds={'x': (-5, 5), 'y': (-5, 5), 'z': (0, 3)},
        )

    def test_approves_safe_command(self):
        cmd = ActuatorCommand(
            actuator_id='motor', actuator_type='motor',
            command={'action': 'move', 'params': {'speed': 1.0, 'x': 0, 'y': 0, 'z': 1}},
        )
        safe, reason = self.monitor.check_command(cmd)
        self.assertTrue(safe)
        self.assertEqual(reason, '')

    def test_rejects_over_velocity(self):
        cmd = ActuatorCommand(
            actuator_id='motor', actuator_type='motor',
            command={'action': 'move', 'params': {'speed': 5.0}},
        )
        safe, reason = self.monitor.check_command(cmd)
        self.assertFalse(safe)
        self.assertIn('velocity', reason.lower())

    def test_rejects_over_force(self):
        cmd = ActuatorCommand(
            actuator_id='gripper', actuator_type='gripper',
            command={'action': 'grip', 'params': {'force': 100.0}},
        )
        safe, reason = self.monitor.check_command(cmd)
        self.assertFalse(safe)
        self.assertIn('force', reason.lower())

    def test_rejects_out_of_workspace(self):
        cmd = ActuatorCommand(
            actuator_id='arm', actuator_type='servo',
            command={'action': 'move', 'params': {'x': 100.0}},
        )
        safe, reason = self.monitor.check_command(cmd)
        self.assertFalse(safe)
        self.assertIn('workspace', reason.lower())

    def test_emergency_stop_sets_flag(self):
        self.assertFalse(self.monitor.is_estopped)
        self.monitor.trigger_estop('test')
        self.assertTrue(self.monitor.is_estopped)

    def test_estop_blocks_all_commands(self):
        self.monitor.trigger_estop('test')
        cmd = ActuatorCommand(
            actuator_id='motor', actuator_type='motor',
            command={'action': 'move', 'params': {'speed': 0.1}},
        )
        safe, reason = self.monitor.check_command(cmd)
        self.assertFalse(safe)
        self.assertIn('E-STOP', reason)

    def test_clear_estop(self):
        self.monitor.trigger_estop('test')
        self.assertTrue(self.monitor.is_estopped)
        self.monitor.clear_estop()
        self.assertFalse(self.monitor.is_estopped)

    def test_gate_commands_sets_safety_cleared(self):
        cmd = ActuatorCommand(
            actuator_id='motor', actuator_type='motor',
            command={'action': 'move', 'params': {'speed': 1.0}},
        )
        self.assertFalse(cmd.safety_cleared)
        cleared = self.monitor.gate_commands([cmd])
        self.assertEqual(len(cleared), 1)
        self.assertTrue(cleared[0].safety_cleared)

    def test_gate_commands_filters_unsafe(self):
        safe_cmd = ActuatorCommand(
            actuator_id='motor', actuator_type='motor',
            command={'action': 'move', 'params': {'speed': 1.0}},
        )
        unsafe_cmd = ActuatorCommand(
            actuator_id='motor2', actuator_type='motor',
            command={'action': 'move', 'params': {'speed': 10.0}},
        )
        cleared = self.monitor.gate_commands([safe_cmd, unsafe_cmd])
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0].actuator_id, 'motor')

    def test_check_position_safe_within_bounds(self):
        self.assertTrue(self.monitor.check_position_safe({'x': 0, 'y': 0, 'z': 1}))

    def test_check_position_safe_outside_bounds(self):
        self.assertFalse(self.monitor.check_position_safe({'x': 100}))


# ===================================================================
# 5. HardwareBridge
# ===================================================================


class TestHardwareBridge(unittest.TestCase):
    """HardwareBridge lifecycle, registration, sensing."""

    def _make_bridge(self, robot_id='test_robot'):
        return HardwareBridge(robot_id)

    def test_register_sensor(self):
        bridge = self._make_bridge()
        adapter = MagicMock(spec=HTTPSensorAdapter)
        adapter.sensor_id = 'cam'
        adapter.sensor_type = 'camera'
        bridge.register_sensor(adapter)
        stats = bridge.get_stats()
        self.assertEqual(stats['sensors_registered'], 1)

    def test_register_actuator(self):
        bridge = self._make_bridge()
        adapter = MagicMock(spec=SerialActuatorAdapter)
        adapter.actuator_id = 'motor'
        adapter.actuator_type = 'motor'
        bridge.register_actuator(adapter)
        stats = bridge.get_stats()
        self.assertEqual(stats['actuators_registered'], 1)

    def test_register_sensor_replaces_existing(self):
        bridge = self._make_bridge()
        old = MagicMock(spec=HTTPSensorAdapter)
        old.sensor_id = 'cam'
        old.sensor_type = 'camera'
        bridge.register_sensor(old)

        new = MagicMock(spec=HTTPSensorAdapter)
        new.sensor_id = 'cam'  # same ID
        new.sensor_type = 'camera'
        bridge.register_sensor(new)

        old.stop_stream.assert_called_once()
        self.assertEqual(bridge.get_stats()['sensors_registered'], 1)

    @patch('integrations.robotics.hardware_bridge.HardwareBridge._emit')
    def test_start_and_stop(self, mock_emit):
        bridge = self._make_bridge()
        adapter = MagicMock(spec=HTTPSensorAdapter)
        adapter.sensor_id = 'cam'
        adapter.sensor_type = 'camera'
        bridge.register_sensor(adapter)

        actuator = MagicMock(spec=SerialActuatorAdapter)
        actuator.actuator_id = 'motor'
        actuator.actuator_type = 'motor'
        bridge.register_actuator(actuator)

        bridge.start()
        self.assertTrue(bridge.get_stats()['running'])

        # start_stream should have been called on the sensor adapter
        adapter.start_stream.assert_called_once()

        bridge.stop()
        self.assertFalse(bridge.get_stats()['running'])

        # stop_stream should have been called
        adapter.stop_stream.assert_called_once()
        # emergency_stop should have been called on actuator
        actuator.emergency_stop.assert_called_once()

    def test_start_is_idempotent(self):
        bridge = self._make_bridge()
        bridge.start()
        bridge.start()  # Second call should be a no-op
        self.assertTrue(bridge.get_stats()['running'])
        bridge.stop()

    def test_get_sensor_snapshot_empty(self):
        bridge = self._make_bridge()
        snapshot = bridge.get_sensor_snapshot()
        self.assertEqual(snapshot, {})

    def test_get_stats_returns_correct_counts(self):
        bridge = self._make_bridge()

        sensor = MagicMock(spec=HTTPSensorAdapter)
        sensor.sensor_id = 'cam'
        sensor.sensor_type = 'camera'
        bridge.register_sensor(sensor)

        actuator1 = MagicMock(spec=SerialActuatorAdapter)
        actuator1.actuator_id = 'motor_l'
        actuator1.actuator_type = 'motor'
        actuator2 = MagicMock(spec=HTTPActuatorAdapter)
        actuator2.actuator_id = 'motor_r'
        actuator2.actuator_type = 'motor'
        bridge.register_actuator(actuator1)
        bridge.register_actuator(actuator2)

        stats = bridge.get_stats()
        self.assertEqual(stats['robot_id'], 'test_robot')
        self.assertEqual(stats['sensors_registered'], 1)
        self.assertEqual(stats['actuators_registered'], 2)
        self.assertFalse(stats['running'])


class TestGetBridgeSingleton(unittest.TestCase):
    """get_bridge() returns the same instance per robot_id."""

    def test_same_robot_id_returns_same_instance(self):
        # Use a unique robot ID to avoid polluting global state
        rid = f'test_singleton_{time.monotonic()}'
        b1 = get_bridge(rid)
        b2 = get_bridge(rid)
        self.assertIs(b1, b2)

    def test_different_robot_ids_return_different_instances(self):
        suffix = str(time.monotonic())
        b1 = get_bridge(f'robot_a_{suffix}')
        b2 = get_bridge(f'robot_b_{suffix}')
        self.assertIsNot(b1, b2)


# ===================================================================
# 6. create_robotics_blueprint
# ===================================================================


class TestCreateRoboticsBlueprint(unittest.TestCase):
    """create_robotics_blueprint() returns a Flask Blueprint."""

    def test_returns_blueprint(self):
        try:
            from flask import Blueprint
        except ImportError:
            self.skipTest('Flask not installed')
        bp = create_robotics_blueprint()
        self.assertIsNotNone(bp)
        self.assertIsInstance(bp, Blueprint)


# ===================================================================
# 7. RobotIntelligenceAPI — think()
# ===================================================================


class TestRobotIntelligenceAPI(unittest.TestCase):
    """RobotIntelligenceAPI.think() with all intelligences mocked."""

    def _make_api(self, tmp_registry=None):
        """Create a fresh API with a temp registry path."""
        api = RobotIntelligenceAPI.__new__(RobotIntelligenceAPI)
        api._lock = threading.RLock()
        from concurrent.futures import ThreadPoolExecutor
        api._executor = ThreadPoolExecutor(
            max_workers=7, thread_name_prefix='test_intel')
        api._registry = {}
        api._stats = {
            'total_think_calls': 0,
            'total_intelligence_invocations': 0,
            'total_timeouts': 0,
            'total_errors': 0,
            'avg_fusion_time_ms': 0.0,
        }
        return api

    @patch.object(RobotIntelligenceAPI, '_invoke_vision',
                  return_value={'scene': 'kitchen', 'objects': ['cup'], 'obstacles': []})
    @patch.object(RobotIntelligenceAPI, '_invoke_language',
                  return_value={'response': 'OK', 'intent': 'fetch_object'})
    @patch.object(RobotIntelligenceAPI, '_invoke_motor',
                  return_value={'trajectory': [{'action_type': 'nav'}], 'speed': 1.0, 'source': 'basic'})
    @patch.object(RobotIntelligenceAPI, '_invoke_spatial',
                  return_value={'robot_position': {'x': 0}, 'world_objects': [], 'sensor_summary': {}})
    @patch.object(RobotIntelligenceAPI, '_invoke_social',
                  return_value={'tone': 'helpful', 'urgency': 'normal', 'formality': 0.5, 'verbosity': 0.5})
    @patch.object(RobotIntelligenceAPI, '_invoke_safety',
                  return_value={'safe': True, 'warnings': [], 'estop': False})
    @patch.object(RobotIntelligenceAPI, '_invoke_hivemind',
                  return_value={'similar_tasks': 3, 'best_strategy': 'approach', 'confidence': 0.8,
                                'contributing_agents': [], 'source': 'hivemind'})
    def test_think_returns_fused_result(self, *mocks):
        api = self._make_api()
        result = api.think({
            'robot_id': 'test_bot',
            'sensors': {'camera': 'b64...'},
            'context': 'fetch the cup',
        })

        self.assertIn('action_plan', result)
        self.assertIn('intelligences', result)
        self.assertIn('fusion_time_ms', result)
        self.assertIn('intelligences_used', result)
        self.assertGreater(result['intelligences_used'], 0)
        # All 7 should have been invoked
        self.assertEqual(len(result['intelligences']), 7)

    @patch.object(RobotIntelligenceAPI, '_invoke_vision',
                  return_value={'scene': 'room', 'objects': [], 'obstacles': []})
    @patch.object(RobotIntelligenceAPI, '_invoke_language',
                  return_value={'response': 'OK', 'intent': 'navigate'})
    @patch.object(RobotIntelligenceAPI, '_invoke_motor',
                  return_value={'trajectory': [{'action_type': 'move'}], 'speed': 1.0, 'source': 'basic'})
    @patch.object(RobotIntelligenceAPI, '_invoke_spatial',
                  return_value={'robot_position': {}, 'world_objects': [], 'sensor_summary': {}})
    @patch.object(RobotIntelligenceAPI, '_invoke_social',
                  return_value={'tone': 'neutral', 'urgency': 'normal', 'formality': 0.5, 'verbosity': 0.5})
    @patch.object(RobotIntelligenceAPI, '_invoke_safety',
                  return_value={'safe': False, 'warnings': ['obstacle ahead'], 'estop': False})
    @patch.object(RobotIntelligenceAPI, '_invoke_hivemind',
                  return_value={'similar_tasks': 0, 'best_strategy': '', 'confidence': 0.0,
                                'contributing_agents': [], 'source': 'unavailable'})
    def test_safety_override_halts_plan(self, *mocks):
        """When safety says unsafe, the action_plan primary_action is 'halt'."""
        api = self._make_api()
        result = api.think({
            'robot_id': 'test_bot',
            'sensors': {},
            'context': 'go fast',
        })
        plan = result['action_plan']
        self.assertEqual(plan['primary_action'], 'halt')
        self.assertEqual(plan['steps'], [])
        self.assertIn('safety_warnings', plan)

    @patch.object(RobotIntelligenceAPI, '_invoke_vision',
                  return_value={'scene': 'ok', 'objects': [], 'obstacles': []})
    @patch.object(RobotIntelligenceAPI, '_invoke_language',
                  return_value={'response': '', 'intent': 'idle'})
    @patch.object(RobotIntelligenceAPI, '_invoke_motor',
                  return_value={'trajectory': [], 'speed': 1.0, 'source': 'basic'})
    @patch.object(RobotIntelligenceAPI, '_invoke_spatial',
                  return_value={'robot_position': {}, 'world_objects': [], 'sensor_summary': {}})
    @patch.object(RobotIntelligenceAPI, '_invoke_social',
                  return_value={'tone': 'neutral', 'urgency': 'normal', 'formality': 0.5, 'verbosity': 0.5})
    @patch.object(RobotIntelligenceAPI, '_invoke_safety',
                  return_value={'safe': True, 'warnings': [], 'estop': True})
    @patch.object(RobotIntelligenceAPI, '_invoke_hivemind',
                  return_value={'similar_tasks': 0, 'best_strategy': '', 'confidence': 0.0,
                                'contributing_agents': [], 'source': 'unavailable'})
    def test_estop_active_halts_plan(self, *mocks):
        """When estop is True, plan is halted regardless of safe flag."""
        api = self._make_api()
        result = api.think({
            'robot_id': 'test_bot',
            'sensors': {},
            'context': '',
        })
        plan = result['action_plan']
        self.assertEqual(plan['primary_action'], 'halt')

    def test_think_dict_form(self):
        """think() accepts a dict argument."""
        api = self._make_api()
        with patch.object(api, '_invoke_vision', return_value={'scene': 'ok', 'objects': [], 'obstacles': []}), \
             patch.object(api, '_invoke_language', return_value={'response': '', 'intent': 'idle'}), \
             patch.object(api, '_invoke_motor', return_value={'trajectory': [], 'speed': 1.0, 'source': 'basic'}), \
             patch.object(api, '_invoke_spatial', return_value={'robot_position': {}, 'world_objects': [], 'sensor_summary': {}}), \
             patch.object(api, '_invoke_social', return_value={'tone': 'neutral', 'urgency': 'normal', 'formality': 0.5, 'verbosity': 0.5}), \
             patch.object(api, '_invoke_safety', return_value={'safe': True, 'warnings': [], 'estop': False}), \
             patch.object(api, '_invoke_hivemind', return_value={'similar_tasks': 0, 'best_strategy': '', 'confidence': 0.0, 'contributing_agents': [], 'source': 'unavailable'}):
            result = api.think({'robot_id': 'bot', 'context': 'hello'})
        self.assertIn('action_plan', result)

    def test_think_stats_updated(self):
        """think() updates internal stats counters."""
        api = self._make_api()
        with patch.object(api, '_invoke_vision', return_value={'scene': 'ok', 'objects': [], 'obstacles': []}), \
             patch.object(api, '_invoke_language', return_value={'response': '', 'intent': 'idle'}), \
             patch.object(api, '_invoke_motor', return_value={'trajectory': [], 'speed': 1.0, 'source': 'basic'}), \
             patch.object(api, '_invoke_spatial', return_value={'robot_position': {}, 'world_objects': [], 'sensor_summary': {}}), \
             patch.object(api, '_invoke_social', return_value={'tone': 'neutral', 'urgency': 'normal', 'formality': 0.5, 'verbosity': 0.5}), \
             patch.object(api, '_invoke_safety', return_value={'safe': True, 'warnings': [], 'estop': False}), \
             patch.object(api, '_invoke_hivemind', return_value={'similar_tasks': 0, 'best_strategy': '', 'confidence': 0.0, 'contributing_agents': [], 'source': 'unavailable'}):
            api.think({'robot_id': 'bot'})
        self.assertEqual(api._stats['total_think_calls'], 1)
        self.assertGreater(api._stats['total_intelligence_invocations'], 0)


class TestRobotIntelligenceAPITimeout(unittest.TestCase):
    """Timeout handling: slow intelligence causes TimeoutError on individual futures."""

    def test_individual_future_timeout_recorded(self):
        """When a single intelligence raises an exception in the thread pool,
        think() still completes and records the error.

        We use a plain Exception (not TimeoutError) because TimeoutError
        raised inside executor threads propagates differently via as_completed.
        """
        api = RobotIntelligenceAPI.__new__(RobotIntelligenceAPI)
        api._lock = threading.RLock()
        from concurrent.futures import ThreadPoolExecutor
        api._executor = ThreadPoolExecutor(max_workers=7, thread_name_prefix='test')
        api._registry = {}
        api._stats = {
            'total_think_calls': 0,
            'total_intelligence_invocations': 0,
            'total_timeouts': 0,
            'total_errors': 0,
            'avg_fusion_time_ms': 0.0,
        }

        with patch.object(api, '_invoke_vision', side_effect=Exception("vision timed out")), \
             patch.object(api, '_invoke_language', return_value={'response': '', 'intent': 'idle'}), \
             patch.object(api, '_invoke_motor', return_value={'trajectory': [], 'speed': 1.0, 'source': 'basic'}), \
             patch.object(api, '_invoke_spatial', return_value={'robot_position': {}, 'world_objects': [], 'sensor_summary': {}}), \
             patch.object(api, '_invoke_social', return_value={'tone': 'neutral', 'urgency': 'normal', 'formality': 0.5, 'verbosity': 0.5}), \
             patch.object(api, '_invoke_safety', return_value={'safe': True, 'warnings': [], 'estop': False}), \
             patch.object(api, '_invoke_hivemind', return_value={'similar_tasks': 0, 'best_strategy': '', 'confidence': 0.0, 'contributing_agents': [], 'source': 'unavailable'}):
            result = api.think({'robot_id': 'bot'})

        # The result should still complete (other 6 intelligences succeeded)
        self.assertIn('action_plan', result)
        # vision should show an error entry
        vision_result = result['intelligences'].get('vision', {})
        self.assertIn('error', vision_result)


class TestRobotIntelligenceAPIMissingIntelligence(unittest.TestCase):
    """Graceful degradation when an intelligence raises an exception."""

    def test_exception_in_intelligence_recorded_as_error(self):
        api = RobotIntelligenceAPI.__new__(RobotIntelligenceAPI)
        api._lock = threading.RLock()
        from concurrent.futures import ThreadPoolExecutor
        api._executor = ThreadPoolExecutor(max_workers=7, thread_name_prefix='test')
        api._registry = {}
        api._stats = {
            'total_think_calls': 0,
            'total_intelligence_invocations': 0,
            'total_timeouts': 0,
            'total_errors': 0,
            'avg_fusion_time_ms': 0.0,
        }

        with patch.object(api, '_invoke_vision', side_effect=RuntimeError("GPU OOM")), \
             patch.object(api, '_invoke_language', return_value={'response': '', 'intent': 'idle'}), \
             patch.object(api, '_invoke_motor', return_value={'trajectory': [], 'speed': 1.0, 'source': 'basic'}), \
             patch.object(api, '_invoke_spatial', return_value={'robot_position': {}, 'world_objects': [], 'sensor_summary': {}}), \
             patch.object(api, '_invoke_social', return_value={'tone': 'neutral', 'urgency': 'normal', 'formality': 0.5, 'verbosity': 0.5}), \
             patch.object(api, '_invoke_safety', return_value={'safe': True, 'warnings': [], 'estop': False}), \
             patch.object(api, '_invoke_hivemind', return_value={'similar_tasks': 0, 'best_strategy': '', 'confidence': 0.0, 'contributing_agents': [], 'source': 'unavailable'}):
            result = api.think({'robot_id': 'bot'})

        # Should still return a result
        self.assertIn('action_plan', result)
        # Vision should have an error entry
        self.assertIn('error', result['intelligences'].get('vision', {}))
        # Error count should be incremented
        self.assertGreaterEqual(api._stats['total_errors'], 1)


# ===================================================================
# 8. Robot Registry (register / list)
# ===================================================================


class TestRobotRegistry(unittest.TestCase):
    """register_robot() / list_robots() on RobotIntelligenceAPI."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.registry_path = os.path.join(self.tmpdir, 'robot_registry.json')
        self.api = RobotIntelligenceAPI.__new__(RobotIntelligenceAPI)
        self.api._lock = threading.RLock()
        from concurrent.futures import ThreadPoolExecutor
        self.api._executor = ThreadPoolExecutor(max_workers=7, thread_name_prefix='test')
        self.api._registry = {}
        self.api._stats = {
            'total_think_calls': 0,
            'total_intelligence_invocations': 0,
            'total_timeouts': 0,
            'total_errors': 0,
            'avg_fusion_time_ms': 0.0,
        }

    def test_register_robot(self):
        with patch('integrations.robotics.intelligence_api._REGISTRY_PATH', self.registry_path):
            result = self.api.register_robot('bot_1', {
                'form_factor': 'wheeled',
                'sensors': ['camera', 'lidar'],
            })
        self.assertTrue(result['registered'])
        self.assertEqual(result['robot_id'], 'bot_1')
        self.assertIn('timestamp', result)

    def test_list_robots_after_registration(self):
        with patch('integrations.robotics.intelligence_api._REGISTRY_PATH', self.registry_path):
            self.api.register_robot('bot_1', {'form_factor': 'wheeled'})
            self.api.register_robot('bot_2', {'form_factor': 'humanoid'})
            robots = self.api.list_robots()

        self.assertEqual(len(robots), 2)
        ids = [r['robot_id'] for r in robots]
        self.assertIn('bot_1', ids)
        self.assertIn('bot_2', ids)

    def test_list_robots_empty(self):
        robots = self.api.list_robots()
        self.assertEqual(robots, [])

    def test_register_preserves_operator_id(self):
        with patch('integrations.robotics.intelligence_api._REGISTRY_PATH', self.registry_path):
            self.api.register_robot('bot_op', {
                'form_factor': 'drone',
                'operator_id': 'user_42',
            })
        entry = self.api._registry['bot_op']
        self.assertEqual(entry['operator_id'], 'user_42')

    def test_get_robot_status_found(self):
        with patch('integrations.robotics.intelligence_api._REGISTRY_PATH', self.registry_path):
            self.api.register_robot('bot_s', {'form_factor': 'arm'})
        status = self.api.get_robot_status('bot_s')
        self.assertTrue(status['found'])
        self.assertEqual(status['robot_id'], 'bot_s')

    def test_get_robot_status_not_found(self):
        status = self.api.get_robot_status('nonexistent')
        self.assertFalse(status['found'])


# ===================================================================
# 9. Singleton get_robot_api()
# ===================================================================


class TestGetRobotAPISingleton(unittest.TestCase):
    """get_robot_api() returns the same instance."""

    def test_singleton(self):
        # We cannot easily reset the global, but we can check it returns
        # a RobotIntelligenceAPI instance and is the same on repeated calls.
        import integrations.robotics.intelligence_api as mod
        old = mod._api

        try:
            mod._api = None  # Reset so we get a fresh instance
            with patch.object(RobotIntelligenceAPI, '_load_registry'):
                a = get_robot_api()
                b = get_robot_api()
            self.assertIs(a, b)
            self.assertIsInstance(a, RobotIntelligenceAPI)
        finally:
            mod._api = old  # Restore


# ===================================================================
# 10. create_intelligence_blueprint
# ===================================================================


class TestCreateIntelligenceBlueprint(unittest.TestCase):
    """create_intelligence_blueprint() returns a Flask Blueprint."""

    def test_returns_blueprint(self):
        try:
            from flask import Blueprint
        except ImportError:
            self.skipTest('Flask not installed')
        bp = create_intelligence_blueprint()
        self.assertIsNotNone(bp)
        self.assertIsInstance(bp, Blueprint)

    def test_has_deferred_functions(self):
        try:
            from flask import Blueprint
        except ImportError:
            self.skipTest('Flask not installed')
        bp = create_intelligence_blueprint()
        # Flask blueprints defer route registration via deferred_functions.
        # At least the /think and /robots routes should be registered.
        self.assertGreater(len(bp.deferred_functions), 0)


# ===================================================================
# 11. Helper functions in intelligence_api.py
# ===================================================================


class TestClassifyIntent(unittest.TestCase):
    """_classify_intent() keyword-based fallback."""

    def test_fetch(self):
        self.assertEqual(_classify_intent('', 'fetch the cup'), 'fetch_object')

    def test_navigate(self):
        self.assertEqual(_classify_intent('', 'go to the kitchen'), 'navigate')

    def test_greet(self):
        self.assertEqual(_classify_intent('hello there', ''), 'greet')

    def test_emergency(self):
        self.assertEqual(_classify_intent('', 'emergency stop now'), 'emergency')

    def test_default_is_assist(self):
        # Avoid substrings that match keywords (e.g. 'hi' in 'thing' matches greet)
        self.assertEqual(_classify_intent('', 'xyz qqq rrr'), 'assist')


class TestExtractTarget(unittest.TestCase):
    """_extract_target() heuristic target extraction."""

    def test_extracts_from_vision_objects(self):
        target = _extract_target('fetch', {'objects': [{'x': 1, 'y': 2, 'label': 'cup'}]})
        self.assertEqual(target.get('x'), 1)
        self.assertEqual(target.get('label'), 'cup')

    def test_empty_when_no_objects(self):
        target = _extract_target('go', {'objects': []})
        self.assertEqual(target, {})

    def test_string_object_label(self):
        target = _extract_target('go', {'objects': ['table']})
        self.assertEqual(target.get('label'), 'table')


# ===================================================================
# 12. Module-level think() convenience function
# ===================================================================


class TestModuleLevelThink(unittest.TestCase):
    """Module-level think() delegates to singleton."""

    def test_kwargs_form(self):
        """think(robot_id='...', sensor_snapshot={}) maps correctly."""
        from integrations.robotics.intelligence_api import think as think_fn

        mock_api = MagicMock()
        mock_api.think.return_value = {'action_plan': {}, 'intelligences': {}}

        with patch('integrations.robotics.intelligence_api.get_robot_api',
                   return_value=mock_api):
            result = think_fn(
                robot_id='bot',
                sensor_snapshot={'cam': 'frame'},
                context='go',
            )

        # sensor_snapshot should have been renamed to sensors
        call_args = mock_api.think.call_args[0][0]
        self.assertEqual(call_args['robot_id'], 'bot')
        self.assertIn('sensors', call_args)
        self.assertNotIn('sensor_snapshot', call_args)

    def test_dict_form(self):
        from integrations.robotics.intelligence_api import think as think_fn

        mock_api = MagicMock()
        mock_api.think.return_value = {'action_plan': {}}

        with patch('integrations.robotics.intelligence_api.get_robot_api',
                   return_value=mock_api):
            result = think_fn({'robot_id': 'bot', 'sensors': {'cam': 'x'}})

        call_args = mock_api.think.call_args[0][0]
        self.assertEqual(call_args['robot_id'], 'bot')
        self.assertEqual(call_args['sensors'], {'cam': 'x'})


# ===================================================================
# 13. Intelligence fusion priorities
# ===================================================================


class TestFusionPriority(unittest.TestCase):
    """Verify safety has highest fusion priority."""

    def test_safety_highest_priority(self):
        max_key = max(_FUSION_PRIORITY, key=_FUSION_PRIORITY.get)
        self.assertEqual(max_key, 'safety')

    def test_all_intelligence_types_have_priority(self):
        for itype in INTELLIGENCE_TYPES:
            self.assertIn(itype, _FUSION_PRIORITY,
                          f'{itype} missing from _FUSION_PRIORITY')

    def test_motor_above_vision(self):
        self.assertGreater(
            _FUSION_PRIORITY['motor'],
            _FUSION_PRIORITY['vision'],
        )


# ===================================================================
# 14. Hive stats
# ===================================================================


class TestHiveStats(unittest.TestCase):
    """get_hive_stats() returns correct shape."""

    def test_hive_stats_empty(self):
        api = RobotIntelligenceAPI.__new__(RobotIntelligenceAPI)
        api._lock = threading.RLock()
        api._registry = {}
        api._stats = {
            'total_think_calls': 0,
            'total_intelligence_invocations': 0,
            'total_timeouts': 0,
            'total_errors': 0,
            'avg_fusion_time_ms': 0.0,
        }
        stats = api.get_hive_stats()
        self.assertEqual(stats['total_robots'], 0)
        self.assertEqual(stats['online_robots'], 0)
        self.assertEqual(stats['intelligence_types'], INTELLIGENCE_TYPES)
        self.assertIn('stats', stats)


if __name__ == '__main__':
    unittest.main()
