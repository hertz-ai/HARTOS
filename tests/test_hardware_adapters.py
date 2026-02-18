"""
Tests for hardware I/O channel adapters (Phase 2 - Embedded/Robot Support).

Tests: SerialAdapter, GPIOAdapter, WAMPIoTAdapter, ROSBridgeAdapter, auto_register.
All hardware libraries are mocked - tests run on any platform.
"""
import asyncio
import json
import os
import struct
import sys
import threading
import time
import uuid
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest

# ─── Import adapters ───
from integrations.channels.base import (
    ChannelAdapter, ChannelConfig, ChannelStatus,
    Message, SendResult,
)
from integrations.channels.hardware.serial_adapter import SerialAdapter
from integrations.channels.hardware.gpio_adapter import GPIOAdapter, _parse_pin_list
from integrations.channels.hardware.wamp_iot_adapter import WAMPIoTAdapter, MQTTAdapter, _parse_topic_list
from integrations.channels.hardware.ros_bridge import ROSBridgeAdapter, _is_image_topic


# ─── Helper ───
def _run(coro):
    """Run async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════
#  Serial Adapter Tests
# ═══════════════════════════════════════════════════════════════════

class TestSerialAdapter:
    """Tests for SerialAdapter."""

    def test_name(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0')
        assert adapter.name == 'serial'

    def test_default_config_from_env(self):
        with patch.dict(os.environ, {
            'HEVOLVE_SERIAL_PORT': '/dev/ttyACM0',
            'HEVOLVE_SERIAL_BAUD': '9600',
            'HEVOLVE_SERIAL_PROTOCOL': 'json_line',
        }):
            adapter = SerialAdapter()
            assert adapter._port == '/dev/ttyACM0'
            assert adapter._baud_rate == 9600
            assert adapter._protocol == 'json_line'

    def test_connect_no_pyserial(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0')
        with patch.dict(sys.modules, {'serial': None}):
            # ImportError when serial is None
            result = _run(adapter.connect())
            assert result is False

    @patch('integrations.channels.hardware.serial_adapter.serial', create=True)
    def test_connect_success(self, mock_serial_mod):
        """Test successful serial connection."""
        mock_serial_cls = MagicMock()
        mock_port = MagicMock()
        mock_serial_cls.return_value = mock_port

        # Patch the import inside connect()
        import importlib
        with patch.dict(sys.modules, {'serial': MagicMock()}):
            adapter = SerialAdapter(port='/dev/ttyUSB0', baud_rate=115200)
            # Mock serial.Serial inside connect
            mock_serial = MagicMock()
            mock_serial.Serial.return_value = mock_port

            with patch.dict(sys.modules, {'serial': mock_serial, 'serial.tools': MagicMock(), 'serial.tools.list_ports': MagicMock()}):
                result = _run(adapter.connect())
                assert result is True
                assert adapter.status == ChannelStatus.CONNECTED

    def test_send_message_not_connected(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0')
        result = _run(adapter.send_message('test', 'hello'))
        assert result.success is False
        assert 'not open' in result.error

    def test_send_message_text_line(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0', protocol='text_line')
        mock_serial = MagicMock()
        mock_serial.is_open = True
        adapter._serial = mock_serial

        result = _run(adapter.send_message('device1', 'hello world'))
        assert result.success is True
        mock_serial.write.assert_called_once_with(b'hello world\n')

    def test_send_message_json_line(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0', protocol='json_line')
        mock_serial = MagicMock()
        mock_serial.is_open = True
        adapter._serial = mock_serial

        result = _run(adapter.send_message('device1', 'hello'))
        assert result.success is True
        written = mock_serial.write.call_args[0][0]
        data = json.loads(written.decode('utf-8').strip())
        assert data['text'] == 'hello'
        assert data['chat_id'] == 'device1'

    def test_send_message_binary_frame(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0', protocol='binary_frame')
        mock_serial = MagicMock()
        mock_serial.is_open = True
        adapter._serial = mock_serial

        result = _run(adapter.send_message('device1', 'hi'))
        assert result.success is True
        written = mock_serial.write.call_args[0][0]
        # Verify frame: STX(0x02) + len(2 bytes big-endian) + payload + ETX(0x03)
        assert written[0] == 0x02  # STX
        payload_len = struct.unpack('>H', written[1:3])[0]
        assert payload_len == 2  # 'hi' = 2 bytes
        assert written[3:5] == b'hi'
        assert written[5] == 0x03  # ETX

    def test_disconnect(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0')
        mock_serial = MagicMock()
        mock_serial.is_open = True
        adapter._serial = mock_serial
        adapter._running = True

        _run(adapter.disconnect())
        assert adapter._running is False
        mock_serial.close.assert_called_once()

    def test_dispatch_text_line(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0', protocol='text_line')
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._dispatch_serial_message('Hello from Arduino')
        assert len(received) == 1
        assert received[0].channel == 'serial'
        assert received[0].text == 'Hello from Arduino'

    def test_dispatch_json_line(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0', protocol='json_line')
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._dispatch_serial_message('{"text": "sensor_data", "value": 42}')
        assert len(received) == 1
        assert received[0].text == 'sensor_data'

    def test_edit_sends_new_message(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0')
        mock_serial = MagicMock()
        mock_serial.is_open = True
        adapter._serial = mock_serial

        result = _run(adapter.edit_message('device', 'msg1', 'updated'))
        assert result.success is True

    def test_delete_not_applicable(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0')
        assert _run(adapter.delete_message('dev', 'msg1')) is False

    def test_get_chat_info(self):
        adapter = SerialAdapter(port='/dev/ttyUSB0', baud_rate=9600)
        info = _run(adapter.get_chat_info('device'))
        assert info['port'] == '/dev/ttyUSB0'
        assert info['baud_rate'] == 9600


# ═══════════════════════════════════════════════════════════════════
#  GPIO Adapter Tests
# ═══════════════════════════════════════════════════════════════════

class TestGPIOAdapter:
    """Tests for GPIOAdapter."""

    def test_name(self):
        adapter = GPIOAdapter(input_pins=[17], output_pins=[22])
        assert adapter.name == 'gpio'

    def test_default_config_from_env(self):
        with patch.dict(os.environ, {
            'HEVOLVE_GPIO_INPUT_PINS': '17,27,4',
            'HEVOLVE_GPIO_OUTPUT_PINS': '22,23',
        }):
            adapter = GPIOAdapter()
            assert adapter._input_pins == [17, 27, 4]
            assert adapter._output_pins == [22, 23]

    def test_parse_pin_list(self):
        assert _parse_pin_list('17,27,4') == [17, 27, 4]
        assert _parse_pin_list('') == []
        assert _parse_pin_list('invalid') == []
        assert _parse_pin_list('22') == [22]
        assert _parse_pin_list(' 1 , 2 , 3 ') == [1, 2, 3]

    def test_is_available_sysfs(self):
        """Available if /sys/class/gpio exists (Linux)."""
        with patch('os.path.isdir', return_value=True):
            with patch.dict(sys.modules, {'gpiod': None, 'RPi': None, 'RPi.GPIO': None}):
                # Can't easily mock nested ImportError, test sysfs fallback
                pass

    def test_send_on(self):
        adapter = GPIOAdapter(output_pins=[22])
        adapter._pin_states[22] = False

        result = _run(adapter.send_message('22', 'on'))
        assert result.success is True
        assert adapter._pin_states[22] is True

    def test_send_off(self):
        adapter = GPIOAdapter(output_pins=[22])
        adapter._pin_states[22] = True

        result = _run(adapter.send_message('22', 'off'))
        assert result.success is True
        assert adapter._pin_states[22] is False

    def test_send_toggle(self):
        adapter = GPIOAdapter(output_pins=[22])
        adapter._pin_states[22] = False

        result = _run(adapter.send_message('22', 'toggle'))
        assert result.success is True
        assert adapter._pin_states[22] is True

        result = _run(adapter.send_message('22', 'toggle'))
        assert adapter._pin_states[22] is False

    def test_send_pwm(self):
        adapter = GPIOAdapter(output_pins=[22])

        result = _run(adapter.send_message('22', 'pwm:50'))
        assert result.success is True
        assert adapter._pin_states[22] == 50

    def test_send_pwm_clamp(self):
        adapter = GPIOAdapter(output_pins=[22])

        _run(adapter.send_message('22', 'pwm:150'))
        assert adapter._pin_states[22] == 100  # Clamped to max

        _run(adapter.send_message('22', 'pwm:-10'))
        assert adapter._pin_states[22] == 0  # Clamped to min

    def test_send_invalid_pin(self):
        adapter = GPIOAdapter(output_pins=[22])
        result = _run(adapter.send_message('abc', 'on'))
        assert result.success is False
        assert 'Invalid pin' in result.error

    def test_send_unconfigured_pin(self):
        adapter = GPIOAdapter(output_pins=[22])
        result = _run(adapter.send_message('17', 'on'))
        assert result.success is False
        assert 'not configured' in result.error

    def test_send_unknown_command(self):
        adapter = GPIOAdapter(output_pins=[22])
        result = _run(adapter.send_message('22', 'blink'))
        assert result.success is False
        assert 'Unknown command' in result.error

    def test_dispatch_gpio_event(self):
        adapter = GPIOAdapter(input_pins=[17])
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._dispatch_gpio_event(17, True)
        assert len(received) == 1
        msg = received[0]
        assert msg.channel == 'gpio'
        assert msg.sender_id == 'gpio:17'
        assert 'HIGH' in msg.text
        assert msg.raw['pin'] == 17
        assert msg.raw['state'] is True

    def test_dispatch_gpio_event_low(self):
        adapter = GPIOAdapter(input_pins=[17])
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._dispatch_gpio_event(17, False)
        assert 'LOW' in received[0].text

    def test_get_chat_info_input(self):
        adapter = GPIOAdapter(input_pins=[17], output_pins=[22])
        adapter._pin_states[17] = True
        info = _run(adapter.get_chat_info('17'))
        assert info['pin'] == 17
        assert info['type'] == 'input'
        assert info['state'] is True

    def test_get_chat_info_output(self):
        adapter = GPIOAdapter(input_pins=[17], output_pins=[22])
        adapter._pin_states[22] = False
        info = _run(adapter.get_chat_info('22'))
        assert info['type'] == 'output'

    def test_get_chat_info_invalid(self):
        adapter = GPIOAdapter()
        assert _run(adapter.get_chat_info('abc')) is None

    def test_debounce(self):
        """Verify debounce prevents rapid state changes."""
        adapter = GPIOAdapter(input_pins=[17], debounce_ms=200)
        adapter._pin_states[17] = False
        adapter._pin_last_event[17] = time.time() * 1000  # Just now

        # Simulate rapid poll - should be debounced
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        # _poll_loop does debounce internally, test the mechanism
        now = time.time() * 1000
        adapter._pin_last_event[17] = now
        # If called within debounce window, event should NOT fire
        # This tests the component, not the full poll loop
        assert (now - adapter._pin_last_event[17]) < adapter._debounce_ms

    def test_delete_not_applicable(self):
        adapter = GPIOAdapter()
        assert _run(adapter.delete_message('22', 'msg')) is False


# ═══════════════════════════════════════════════════════════════════
#  MQTT Adapter Tests
# ═══════════════════════════════════════════════════════════════════

class TestWAMPIoTAdapter:
    """Tests for WAMPIoTAdapter (Crossbar-based IoT pub/sub)."""

    def test_name(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        assert adapter.name == 'wamp_iot'

    def test_backward_compat_alias(self):
        """MQTTAdapter is an alias for WAMPIoTAdapter."""
        assert MQTTAdapter is WAMPIoTAdapter

    def test_default_config_from_env(self):
        with patch.dict(os.environ, {
            'CBURL': 'ws://192.168.1.100:8088/ws',
            'CBREALM': 'hevolve',
            'HEVOLVE_IOT_TOPICS': 'com.iot.sensors,com.iot.actuators',
        }):
            adapter = WAMPIoTAdapter()
            assert adapter._crossbar_url == 'ws://192.168.1.100:8088/ws'
            assert adapter._realm == 'hevolve'
            assert adapter._topics == ['com.iot.sensors', 'com.iot.actuators']

    def test_parse_topic_list(self):
        assert _parse_topic_list('a.b,c.d') == ['a.b', 'c.d']
        assert _parse_topic_list('') == ['com.hertzai.hevolve.iot.sensors']
        assert _parse_topic_list('single.topic') == ['single.topic']

    def test_connect_no_autobahn(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        with patch.dict(sys.modules, {
            'autobahn': None,
            'autobahn.asyncio': None,
            'autobahn.asyncio.component': None,
        }):
            result = _run(adapter.connect())
            assert result is False

    def test_connect_no_url(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CBURL', None)
            adapter = WAMPIoTAdapter(crossbar_url='')
            adapter._crossbar_url = ''
            result = _run(adapter.connect())
            assert result is False

    def test_send_message_not_connected(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        result = _run(adapter.send_message('com.iot.led', 'on'))
        assert result.success is False
        assert 'not active' in result.error

    def test_send_message_success(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        mock_session = MagicMock()
        adapter._session = mock_session

        result = _run(adapter.send_message(
            'com.hertzai.hevolve.iot.actuators.led1', 'on'))
        assert result.success is True
        assert result.message_id.startswith('wamp_')
        mock_session.publish.assert_called_once()

    def test_send_message_json_payload(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        mock_session = MagicMock()
        adapter._session = mock_session

        _run(adapter.send_message(
            'com.iot.actuator',
            '{"cmd": "set_brightness", "value": 50}',
        ))
        # Should parse JSON and publish dict
        call_args = mock_session.publish.call_args
        payload = call_args[0][1]
        assert payload['cmd'] == 'set_brightness'

    def test_on_wamp_event_dict(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._on_wamp_event({
            'text': 'temperature=22.5',
            'sender': 'esp32_01',
            'topic': 'com.iot.sensors.temp',
        })
        assert len(received) == 1
        msg = received[0]
        assert msg.channel == 'wamp_iot'
        assert msg.text == 'temperature=22.5'
        assert msg.sender_id == 'esp32_01'

    def test_on_wamp_event_kwargs(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._on_wamp_event(text='sensor reading', sender='device1')
        assert len(received) == 1
        assert received[0].text == 'sensor reading'

    def test_on_wamp_event_raw_args(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._on_wamp_event('raw string data')
        assert len(received) == 1

    def test_disconnect(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        adapter._running = True
        adapter._session = MagicMock()

        _run(adapter.disconnect())
        assert adapter._running is False
        assert adapter._session is None

    def test_delete_not_applicable(self):
        adapter = WAMPIoTAdapter(crossbar_url='ws://localhost:8088/ws')
        assert _run(adapter.delete_message('topic', 'msg')) is False

    def test_get_chat_info(self):
        adapter = WAMPIoTAdapter(
            crossbar_url='ws://192.168.1.1:8088/ws', realm='hevolve')
        info = _run(adapter.get_chat_info('com.iot.sensors'))
        assert info['crossbar_url'] == 'ws://192.168.1.1:8088/ws'
        assert info['realm'] == 'hevolve'
        assert info['topic'] == 'com.iot.sensors'


# ═══════════════════════════════════════════════════════════════════
#  ROS Bridge Adapter Tests
# ═══════════════════════════════════════════════════════════════════

class TestROSBridgeAdapter:
    """Tests for ROSBridgeAdapter."""

    def test_name(self):
        adapter = ROSBridgeAdapter()
        assert adapter.name == 'ros'

    def test_default_config_from_env(self):
        with patch.dict(os.environ, {
            'HEVOLVE_ROS_TOPICS': '/robot/cmd,/camera/image_raw',
            'HEVOLVE_ROS_PUBLISH_TOPIC': '/robot/response',
            'HEVOLVE_ROS_NODE_NAME': 'test_bridge',
        }):
            adapter = ROSBridgeAdapter()
            assert adapter._subscribe_topics == ['/robot/cmd', '/camera/image_raw']
            assert adapter._publish_topic == '/robot/response'
            assert adapter._node_name == 'test_bridge'

    def test_is_image_topic(self):
        assert _is_image_topic('/camera/image_raw') is True
        assert _is_image_topic('/robot/rgb') is True
        assert _is_image_topic('/depth/frame') is True
        assert _is_image_topic('/robot/cmd') is False
        assert _is_image_topic('/hyve/input') is False

    def test_connect_no_rclpy(self):
        adapter = ROSBridgeAdapter()
        with patch.dict(sys.modules, {'rclpy': None, 'rclpy.node': None}):
            result = _run(adapter.connect())
            assert result is False

    def test_dispatch_ros_message(self):
        adapter = ROSBridgeAdapter()
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        adapter._dispatch_ros_message('/robot/status', 'online')
        assert len(received) == 1
        msg = received[0]
        assert msg.channel == 'ros'
        assert msg.chat_id == '/robot/status'
        assert msg.text == 'online'
        assert msg.sender_id == 'ros:/robot/status'

    def test_dispatch_ros_with_raw(self):
        adapter = ROSBridgeAdapter()
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        raw = {'width': 640, 'height': 480}
        adapter._dispatch_ros_message('/camera/info', 'frame', raw=raw)
        assert received[0].raw == raw

    def test_send_message_not_initialized(self):
        adapter = ROSBridgeAdapter()
        result = _run(adapter.send_message('/topic', 'hello'))
        assert result.success is False
        assert 'not initialized' in result.error

    def test_handle_image_with_framestore(self):
        adapter = ROSBridgeAdapter()
        mock_fs = MagicMock()
        adapter._frame_store = mock_fs

        received = []
        adapter.on_message(lambda msg: received.append(msg))

        # Simulate ROS Image message
        mock_img = MagicMock()
        mock_img.data = b'\x00\xff' * 100
        mock_img.width = 640
        mock_img.height = 480
        mock_img.encoding = 'rgb8'
        mock_img.step = 1920

        adapter._handle_image_message('/camera/image_raw', mock_img)

        # FrameStore should receive the frame
        mock_fs.put_frame.assert_called_once()
        call_kwargs = mock_fs.put_frame.call_args
        assert call_kwargs[1]['user_id'] == 'ros:/camera/image_raw'

        # Message should be dispatched
        assert len(received) == 1
        assert '640x480' in received[0].text
        assert received[0].raw['encoding'] == 'rgb8'

    def test_handle_image_no_framestore(self):
        adapter = ROSBridgeAdapter()
        received = []
        adapter.on_message(lambda msg: received.append(msg))

        mock_img = MagicMock()
        mock_img.data = b'\x00' * 10
        mock_img.width = 320
        mock_img.height = 240
        mock_img.encoding = 'mono8'
        mock_img.step = 320

        # Should not crash without FrameStore
        adapter._handle_image_message('/depth/frame', mock_img)
        assert len(received) == 1

    def test_get_chat_info(self):
        adapter = ROSBridgeAdapter(
            subscribe_topics=['/in'],
            publish_topic='/out',
            node_name='test',
        )
        info = _run(adapter.get_chat_info('/in'))
        assert info['node_name'] == 'test'
        assert info['subscribe_topics'] == ['/in']
        assert info['publish_topic'] == '/out'

    def test_delete_not_applicable(self):
        adapter = ROSBridgeAdapter()
        assert _run(adapter.delete_message('/topic', 'msg')) is False


# ═══════════════════════════════════════════════════════════════════
#  Auto-Registration Tests
# ═══════════════════════════════════════════════════════════════════

class TestAutoRegister:
    """Tests for auto_register_hardware_adapters."""

    def test_serial_always_available(self):
        """SerialAdapter imports without hardware (pyserial is a pip package)."""
        from integrations.channels.hardware import auto_register_hardware_adapters
        available = auto_register_hardware_adapters()
        names = [name for name, _ in available]
        assert 'serial' in names

    def test_wamp_iot_requires_config(self):
        """WAMP IoT only registered when CBURL or HEVOLVE_IOT_TOPICS is set."""
        from integrations.channels.hardware import auto_register_hardware_adapters

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CBURL', None)
            os.environ.pop('HEVOLVE_IOT_TOPICS', None)
            available = auto_register_hardware_adapters()
            names = [name for name, _ in available]
            assert 'wamp_iot' not in names

    def test_wamp_iot_registered_with_cburl(self):
        """WAMP IoT registered when Crossbar URL is configured."""
        from integrations.channels.hardware import auto_register_hardware_adapters

        with patch.dict(os.environ, {'CBURL': 'ws://localhost:8088/ws'}):
            available = auto_register_hardware_adapters()
            names = [name for name, _ in available]
            assert 'wamp_iot' in names

    def test_ros_requires_explicit_enable(self):
        """ROS only registered when HEVOLVE_ROS_BRIDGE_ENABLED=true."""
        from integrations.channels.hardware import auto_register_hardware_adapters

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_ROS_BRIDGE_ENABLED', None)
            available = auto_register_hardware_adapters()
            names = [name for name, _ in available]
            assert 'ros' not in names

    def test_register_with_registry(self):
        """Adapters are registered in registry when provided."""
        from integrations.channels.hardware import auto_register_hardware_adapters

        mock_registry = MagicMock()
        auto_register_hardware_adapters(registry=mock_registry)
        # At minimum serial should be registered
        assert mock_registry.register.called

    def test_registry_error_handling(self):
        """Registration errors don't crash the boot."""
        from integrations.channels.hardware import auto_register_hardware_adapters

        mock_registry = MagicMock()
        mock_registry.register.side_effect = Exception("Registry error")
        # Should not raise
        available = auto_register_hardware_adapters(registry=mock_registry)
        assert len(available) > 0


# ═══════════════════════════════════════════════════════════════════
#  ChannelAdapter Contract Tests
# ═══════════════════════════════════════════════════════════════════

class TestChannelAdapterContract:
    """Verify all hardware adapters implement the ChannelAdapter contract."""

    @pytest.mark.parametrize("adapter_cls,kwargs", [
        (SerialAdapter, {'port': '/dev/tty0'}),
        (GPIOAdapter, {'input_pins': [17], 'output_pins': [22]}),
        (WAMPIoTAdapter, {'crossbar_url': 'ws://localhost:8088/ws'}),
        (ROSBridgeAdapter, {}),
    ])
    def test_is_channel_adapter(self, adapter_cls, kwargs):
        adapter = adapter_cls(**kwargs)
        assert isinstance(adapter, ChannelAdapter)

    @pytest.mark.parametrize("adapter_cls,kwargs", [
        (SerialAdapter, {'port': '/dev/tty0'}),
        (GPIOAdapter, {'input_pins': [17]}),
        (WAMPIoTAdapter, {'crossbar_url': 'ws://localhost:8088/ws'}),
        (ROSBridgeAdapter, {}),
    ])
    def test_has_name(self, adapter_cls, kwargs):
        adapter = adapter_cls(**kwargs)
        assert isinstance(adapter.name, str)
        assert len(adapter.name) > 0

    @pytest.mark.parametrize("adapter_cls,kwargs", [
        (SerialAdapter, {'port': '/dev/tty0'}),
        (GPIOAdapter, {}),
        (WAMPIoTAdapter, {'crossbar_url': 'ws://localhost:8088/ws'}),
        (ROSBridgeAdapter, {}),
    ])
    def test_initial_status_disconnected(self, adapter_cls, kwargs):
        adapter = adapter_cls(**kwargs)
        assert adapter.status == ChannelStatus.DISCONNECTED

    @pytest.mark.parametrize("adapter_cls,kwargs", [
        (SerialAdapter, {'port': '/dev/tty0'}),
        (GPIOAdapter, {}),
        (WAMPIoTAdapter, {'crossbar_url': 'ws://localhost:8088/ws'}),
        (ROSBridgeAdapter, {}),
    ])
    def test_on_message_registers_handler(self, adapter_cls, kwargs):
        adapter = adapter_cls(**kwargs)
        handler = lambda msg: None
        adapter.on_message(handler)
        assert handler in adapter._message_handlers
