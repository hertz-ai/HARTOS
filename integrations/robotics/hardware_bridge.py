"""
Hardware Bridge -- Close the loop between physical robots and the hive.

Soft agents think. Hard agents act. This bridge connects them:

INBOUND (sensors -> hive):
  - Normalize heterogeneous sensor data into a common schema
  - Support: USB serial, GPIO, ROS topics, HTTP streams, WebSocket, MQTT
  - Buffer and batch sensor readings for efficiency
  - Push to intelligence_api.py for multi-intelligence fusion

OUTBOUND (hive -> actuators):
  - Translate action plans into hardware-specific commands
  - Support: serial commands, GPIO, ROS cmd_vel, HTTP actuator APIs
  - Safety gate: every command passes through safety_monitor before execution
  - Feedback loop: actuator response feeds back as sensor data

EXPERIENCE (learning loop):
  - Every sensor->action->outcome triple is an experience
  - Experiences queue to WorldModelBridge for HevolveAI training
  - The hive learns from every robot's actions
  - Better models pushed back to all robots via federation

Usage:
    from integrations.robotics.hardware_bridge import get_bridge

    bridge = get_bridge('robot_01')
    bridge.register_sensor(HTTPSensorAdapter(
        sensor_id='cam_front', sensor_type='camera',
        url='http://192.168.1.10/capture',
    ))
    bridge.register_actuator(SerialActuatorAdapter(
        actuator_id='motor_left', actuator_type='motor',
        port='/dev/ttyUSB0',
    ))
    bridge.start()

    # Full cycle: sense -> think -> act -> learn
    result = bridge.think_and_act(context='navigate to charging station')
"""

import json
import logging
import struct
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ======================================================================
# Data classes
# ======================================================================


@dataclass
class SensorReading:
    """Normalized sensor reading from any hardware source.

    Carries both the normalized ``data`` and the original ``raw`` payload
    so that downstream intelligence can choose the representation it needs.
    """
    sensor_id: str
    sensor_type: str        # camera, lidar, imu, audio, touch, gps, temperature, proximity
    data: Any               # Normalized data (dict, list, scalar)
    timestamp: float = field(default_factory=time.time)
    raw: Any = None         # Original hardware-specific data


@dataclass
class ActuatorCommand:
    """Command destined for a physical actuator.

    ``safety_cleared`` starts False and is only set True by the safety
    gate.  Actuator adapters MUST refuse to execute commands where this
    flag is still False.
    """
    actuator_id: str
    actuator_type: str      # motor, servo, led, speaker, display, gripper
    command: dict            # e.g. {action: 'move', params: {speed: 0.5, direction: 'forward'}}
    safety_cleared: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class Experience:
    """Sensor->action->outcome triple that feeds the learning loop.

    The hive learns from every robot's actions.  Each experience is
    flushed to WorldModelBridge -> HevolveAI so better models can be
    pushed back to all robots via federation.
    """
    robot_id: str
    sensor_state: dict      # Snapshot of all sensors at action time
    action_taken: dict       # What the robot did
    outcome: dict            # What happened (success/fail, sensor delta)
    reward: float            # Computed reward signal
    timestamp: float = field(default_factory=time.time)


# ======================================================================
# Adapter base classes
# ======================================================================


class SensorAdapter(ABC):
    """Base for all sensor input adapters.

    Subclasses bridge a specific transport (serial, HTTP, MQTT, GPIO, ROS)
    to the common SensorReading format.
    """

    def __init__(self, sensor_id: str, sensor_type: str):
        self.sensor_id = sensor_id
        self.sensor_type = sensor_type

    @abstractmethod
    def read(self) -> Optional[SensorReading]:
        """Read a single sensor value (blocking).

        Returns None if the sensor is unavailable or the read failed.
        """
        ...

    @abstractmethod
    def start_stream(self, callback: Callable[[SensorReading], None]) -> None:
        """Begin continuous streaming; call ``callback`` for each reading."""
        ...

    @abstractmethod
    def stop_stream(self) -> None:
        """Stop the continuous stream started by ``start_stream``."""
        ...


class ActuatorAdapter(ABC):
    """Base for all actuator output adapters.

    Subclasses translate ActuatorCommands into hardware-specific protocols
    (serial bytes, HTTP POST, GPIO writes, etc.).
    """

    def __init__(self, actuator_id: str, actuator_type: str):
        self.actuator_id = actuator_id
        self.actuator_type = actuator_type

    @abstractmethod
    def execute(self, command: ActuatorCommand) -> dict:
        """Execute a command.  Returns a result dict with at least ``{ok: bool}``."""
        ...

    @abstractmethod
    def get_state(self) -> dict:
        """Return current actuator state (position, speed, temperature, etc.)."""
        ...

    @abstractmethod
    def emergency_stop(self) -> None:
        """Immediately halt this actuator.  Must be idempotent."""
        ...


# ======================================================================
# Built-in sensor adapters
# ======================================================================


class SerialSensorAdapter(SensorAdapter):
    """Read sensors via USB serial (Arduino, ESP32, etc.).

    Expects line-oriented JSON from the device, e.g.::

        {\"accel_x\": 0.01, \"accel_y\": -9.81, \"accel_z\": 0.02}

    Falls back to a raw-line reading if JSON parsing fails.
    """

    def __init__(
        self,
        sensor_id: str,
        sensor_type: str,
        port: str = '',
        baudrate: int = 115200,
        timeout: float = 0.1,
    ):
        super().__init__(sensor_id, sensor_type)
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable] = None

    def read(self) -> Optional[SensorReading]:
        try:
            import serial as pyserial  # lazy -- optional dependency
        except ImportError:
            logger.debug("SerialSensorAdapter: pyserial not installed")
            return None
        try:
            ser = pyserial.Serial(self._port, self._baudrate, timeout=self._timeout)
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            ser.close()
            if not line:
                return None
            return self._parse_line(line)
        except Exception as exc:
            logger.debug("SerialSensorAdapter read error on %s: %s", self._port, exc)
            return None

    def start_stream(self, callback: Callable[[SensorReading], None]) -> None:
        if self._running:
            return
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(
            target=self._stream_loop,
            name=f'serial_sensor_{self.sensor_id}',
            daemon=True,
        )
        self._thread.start()

    def stop_stream(self) -> None:
        self._running = False

    def _stream_loop(self) -> None:
        try:
            import serial as pyserial
        except ImportError:
            logger.warning("SerialSensorAdapter: pyserial not installed, stream aborted")
            return
        while self._running:
            try:
                ser = pyserial.Serial(self._port, self._baudrate, timeout=self._timeout)
                while self._running:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        reading = self._parse_line(line)
                        if reading and self._callback:
                            self._callback(reading)
                ser.close()
            except Exception as exc:
                logger.debug("SerialSensorAdapter stream error: %s", exc)
                time.sleep(1.0)

    def _parse_line(self, line: str) -> SensorReading:
        raw = line
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            data = {'raw_line': line}
        return SensorReading(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            data=data,
            raw=raw,
        )


class HTTPSensorAdapter(SensorAdapter):
    """Read sensors via HTTP endpoint (IP cameras, REST APIs).

    GETs ``url`` and parses the JSON response as sensor data.
    For binary payloads (e.g. JPEG from an IP camera), the raw bytes
    are stored in ``raw`` and a description dict in ``data``.
    """

    def __init__(
        self,
        sensor_id: str,
        sensor_type: str,
        url: str = '',
        poll_interval: float = 1.0,
        timeout: float = 5.0,
        headers: Optional[dict] = None,
    ):
        super().__init__(sensor_id, sensor_type)
        self._url = url
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._headers = headers or {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable] = None

    def read(self) -> Optional[SensorReading]:
        try:
            from core.http_pool import pooled_get
        except ImportError:
            try:
                import requests
                pooled_get = requests.get
            except ImportError:
                logger.debug("HTTPSensorAdapter: no HTTP library available")
                return None
        try:
            resp = pooled_get(self._url, headers=self._headers, timeout=self._timeout)
            return self._parse_response(resp)
        except Exception as exc:
            logger.debug("HTTPSensorAdapter read error on %s: %s", self._url, exc)
            return None

    def start_stream(self, callback: Callable[[SensorReading], None]) -> None:
        if self._running:
            return
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f'http_sensor_{self.sensor_id}',
            daemon=True,
        )
        self._thread.start()

    def stop_stream(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        while self._running:
            reading = self.read()
            if reading and self._callback:
                self._callback(reading)
            time.sleep(self._poll_interval)

    def _parse_response(self, resp) -> Optional[SensorReading]:
        content_type = resp.headers.get('Content-Type', '')
        if 'json' in content_type:
            try:
                data = resp.json()
            except (ValueError, AttributeError):
                data = {'raw_text': resp.text[:4096]}
            return SensorReading(
                sensor_id=self.sensor_id,
                sensor_type=self.sensor_type,
                data=data,
                raw=resp.text[:4096],
            )
        # Binary payload (e.g. JPEG from camera)
        raw_bytes = resp.content[:10_000_000]  # cap at 10 MB
        return SensorReading(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            data={
                'content_type': content_type,
                'size_bytes': len(raw_bytes),
            },
            raw=raw_bytes,
        )


class MQTTSensorAdapter(SensorAdapter):
    """Read sensors via MQTT topics (IoT devices).

    Subscribes to ``topic`` and fires readings on every message.
    Requires paho-mqtt (optional dependency).
    """

    def __init__(
        self,
        sensor_id: str,
        sensor_type: str,
        broker: str = 'localhost',
        port: int = 1883,
        topic: str = '',
        username: str = '',
        password: str = '',
    ):
        super().__init__(sensor_id, sensor_type)
        self._broker = broker
        self._port = port
        self._topic = topic
        self._username = username
        self._password = password
        self._client = None
        self._callback: Optional[Callable] = None
        self._running = False

    def read(self) -> Optional[SensorReading]:
        # MQTT is event-driven; single read not naturally supported.
        # Do a quick subscribe, grab one message, and disconnect.
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.debug("MQTTSensorAdapter: paho-mqtt not installed")
            return None

        result_holder: List[Optional[SensorReading]] = [None]
        event = threading.Event()

        def on_message(_client, _userdata, msg):
            result_holder[0] = self._parse_payload(msg.payload)
            event.set()

        try:
            client = mqtt.Client()
            if self._username:
                client.username_pw_set(self._username, self._password)
            client.on_message = on_message
            client.connect(self._broker, self._port, keepalive=10)
            client.subscribe(self._topic)
            client.loop_start()
            event.wait(timeout=5.0)
            client.loop_stop()
            client.disconnect()
        except Exception as exc:
            logger.debug("MQTTSensorAdapter read error: %s", exc)
        return result_holder[0]

    def start_stream(self, callback: Callable[[SensorReading], None]) -> None:
        if self._running:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning("MQTTSensorAdapter: paho-mqtt not installed, stream aborted")
            return

        self._callback = callback
        self._running = True

        def on_message(_client, _userdata, msg):
            reading = self._parse_payload(msg.payload)
            if reading and self._callback:
                self._callback(reading)

        self._client = mqtt.Client()
        if self._username:
            self._client.username_pw_set(self._username, self._password)
        self._client.on_message = on_message
        try:
            self._client.connect(self._broker, self._port, keepalive=60)
            self._client.subscribe(self._topic)
            self._client.loop_start()
        except Exception as exc:
            logger.warning("MQTTSensorAdapter connect error: %s", exc)
            self._running = False

    def stop_stream(self) -> None:
        self._running = False
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def _parse_payload(self, payload: bytes) -> Optional[SensorReading]:
        raw = payload
        try:
            text = payload.decode('utf-8', errors='ignore')
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            data = {'raw_bytes': len(payload)}
        return SensorReading(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            data=data,
            raw=raw,
        )


class WebSocketSensorAdapter(SensorAdapter):
    """Read sensors via WebSocket stream.

    Connects to a WebSocket endpoint and receives JSON messages as
    sensor readings.  Useful for high-frequency data (IMU streams,
    depth cameras, real-time telemetry).

    Requires the ``websocket-client`` package (optional dependency).
    """

    def __init__(
        self,
        sensor_id: str,
        sensor_type: str,
        url: str = '',
        headers: Optional[dict] = None,
        reconnect_delay: float = 2.0,
    ):
        super().__init__(sensor_id, sensor_type)
        self._url = url
        self._headers = headers or {}
        self._reconnect_delay = reconnect_delay
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable] = None
        self._ws = None
        self._last_reading: Optional[SensorReading] = None
        self._lock = threading.Lock()

    def read(self) -> Optional[SensorReading]:
        """Return the most recent reading received via the WebSocket.

        If the stream is not active, attempts a one-shot connect,
        reads a single frame, and disconnects.
        """
        # If streaming, return cached latest
        with self._lock:
            if self._last_reading is not None:
                return self._last_reading

        # One-shot read
        try:
            import websocket as ws_lib  # websocket-client
        except ImportError:
            logger.debug("WebSocketSensorAdapter: websocket-client not installed")
            return None

        try:
            ws = ws_lib.create_connection(
                self._url,
                header=self._headers,
                timeout=5.0,
            )
            frame = ws.recv()
            ws.close()
            return self._parse_frame(frame)
        except Exception as exc:
            logger.debug("WebSocketSensorAdapter read error on %s: %s",
                         self._url, exc)
            return None

    def start_stream(self, callback: Callable[[SensorReading], None]) -> None:
        if self._running:
            return
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(
            target=self._stream_loop,
            name=f'ws_sensor_{self.sensor_id}',
            daemon=True,
        )
        self._thread.start()

    def stop_stream(self) -> None:
        self._running = False
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None

    def _stream_loop(self) -> None:
        try:
            import websocket as ws_lib
        except ImportError:
            logger.warning("WebSocketSensorAdapter: websocket-client not installed, "
                           "stream aborted")
            return

        while self._running:
            try:
                ws = ws_lib.create_connection(
                    self._url,
                    header=self._headers,
                    timeout=10.0,
                )
                self._ws = ws
                while self._running:
                    frame = ws.recv()
                    if not frame:
                        break
                    reading = self._parse_frame(frame)
                    if reading:
                        with self._lock:
                            self._last_reading = reading
                        if self._callback:
                            self._callback(reading)
                ws.close()
            except Exception as exc:
                logger.debug("WebSocketSensorAdapter stream error: %s", exc)
            finally:
                self._ws = None

            if self._running:
                time.sleep(self._reconnect_delay)

    def _parse_frame(self, frame) -> Optional[SensorReading]:
        if isinstance(frame, bytes):
            raw = frame
            try:
                text = frame.decode('utf-8', errors='ignore')
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                data = {'raw_bytes': len(frame)}
        else:
            raw = frame
            try:
                data = json.loads(frame)
            except (json.JSONDecodeError, ValueError):
                data = {'raw_text': str(frame)[:4096]}

        return SensorReading(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            data=data,
            raw=raw,
        )


# ======================================================================
# Built-in actuator adapters
# ======================================================================


class SerialActuatorAdapter(ActuatorAdapter):
    """Send commands via USB serial to motor controllers, servo boards, etc.

    Writes JSON-encoded commands to the serial port.  The device is
    expected to respond with a JSON status line.
    """

    def __init__(
        self,
        actuator_id: str,
        actuator_type: str,
        port: str = '',
        baudrate: int = 115200,
        timeout: float = 1.0,
    ):
        super().__init__(actuator_id, actuator_type)
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._lock = threading.Lock()

    def execute(self, command: ActuatorCommand) -> dict:
        if not command.safety_cleared:
            return {'ok': False, 'error': 'command not safety cleared'}
        try:
            import serial as pyserial
        except ImportError:
            return {'ok': False, 'error': 'pyserial not installed'}
        with self._lock:
            try:
                ser = pyserial.Serial(self._port, self._baudrate, timeout=self._timeout)
                payload = json.dumps(command.command) + '\n'
                ser.write(payload.encode('utf-8'))
                response_line = ser.readline().decode('utf-8', errors='ignore').strip()
                ser.close()
                if response_line:
                    try:
                        return {'ok': True, 'response': json.loads(response_line)}
                    except (json.JSONDecodeError, ValueError):
                        return {'ok': True, 'response': response_line}
                return {'ok': True, 'response': None}
            except Exception as exc:
                return {'ok': False, 'error': str(exc)}

    def get_state(self) -> dict:
        try:
            import serial as pyserial
        except ImportError:
            return {'error': 'pyserial not installed'}
        with self._lock:
            try:
                ser = pyserial.Serial(self._port, self._baudrate, timeout=self._timeout)
                ser.write(b'{"action":"get_state"}\n')
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                ser.close()
                if line:
                    try:
                        return json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        return {'raw': line}
                return {}
            except Exception as exc:
                return {'error': str(exc)}

    def emergency_stop(self) -> None:
        try:
            import serial as pyserial
        except ImportError:
            return
        with self._lock:
            try:
                ser = pyserial.Serial(self._port, self._baudrate, timeout=self._timeout)
                ser.write(b'{"action":"emergency_stop"}\n')
                ser.close()
            except Exception as exc:
                logger.error("SerialActuatorAdapter E-stop write failed: %s", exc)


class HTTPActuatorAdapter(ActuatorAdapter):
    """Send commands via HTTP POST (robot APIs, smart home devices).

    POSTs the command dict as JSON to ``url``.
    """

    def __init__(
        self,
        actuator_id: str,
        actuator_type: str,
        url: str = '',
        timeout: float = 5.0,
        headers: Optional[dict] = None,
    ):
        super().__init__(actuator_id, actuator_type)
        self._url = url
        self._timeout = timeout
        self._headers = headers or {}

    def execute(self, command: ActuatorCommand) -> dict:
        if not command.safety_cleared:
            return {'ok': False, 'error': 'command not safety cleared'}
        post_fn = self._get_post_fn()
        if post_fn is None:
            return {'ok': False, 'error': 'no HTTP library available'}
        try:
            resp = post_fn(
                self._url,
                json=command.command,
                headers=self._headers,
                timeout=self._timeout,
            )
            try:
                body = resp.json()
            except (ValueError, AttributeError):
                body = {'status_code': getattr(resp, 'status_code', None)}
            ok = getattr(resp, 'status_code', 500) < 400
            return {'ok': ok, 'response': body}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def get_state(self) -> dict:
        get_fn = self._get_get_fn()
        if get_fn is None:
            return {'error': 'no HTTP library available'}
        try:
            resp = get_fn(
                self._url,
                headers=self._headers,
                timeout=self._timeout,
            )
            try:
                return resp.json()
            except (ValueError, AttributeError):
                return {'raw': getattr(resp, 'text', '')[:4096]}
        except Exception as exc:
            return {'error': str(exc)}

    def emergency_stop(self) -> None:
        post_fn = self._get_post_fn()
        if post_fn is None:
            return
        try:
            post_fn(
                self._url,
                json={'action': 'emergency_stop'},
                headers=self._headers,
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.error("HTTPActuatorAdapter E-stop failed: %s", exc)

    @staticmethod
    def _get_post_fn():
        try:
            from core.http_pool import pooled_post
            return pooled_post
        except ImportError:
            pass
        try:
            import requests
            return requests.post
        except ImportError:
            return None

    @staticmethod
    def _get_get_fn():
        try:
            from core.http_pool import pooled_get
            return pooled_get
        except ImportError:
            pass
        try:
            import requests
            return requests.get
        except ImportError:
            return None


class MQTTActuatorAdapter(ActuatorAdapter):
    """Send commands via MQTT publish (IoT actuators, smart home devices).

    Publishes JSON-encoded commands to the configured ``topic``.
    Optionally subscribes to a ``response_topic`` for acknowledgment.

    Requires paho-mqtt (optional dependency).
    """

    def __init__(
        self,
        actuator_id: str,
        actuator_type: str,
        broker: str = 'localhost',
        port: int = 1883,
        topic: str = '',
        response_topic: str = '',
        username: str = '',
        password: str = '',
        qos: int = 1,
    ):
        super().__init__(actuator_id, actuator_type)
        self._broker = broker
        self._port = port
        self._topic = topic
        self._response_topic = response_topic
        self._username = username
        self._password = password
        self._qos = qos
        self._lock = threading.Lock()

    def execute(self, command: ActuatorCommand) -> dict:
        if not command.safety_cleared:
            return {'ok': False, 'error': 'command not safety cleared'}

        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return {'ok': False, 'error': 'paho-mqtt not installed'}

        with self._lock:
            try:
                client = mqtt.Client()
                if self._username:
                    client.username_pw_set(self._username, self._password)

                # If response topic is set, subscribe for ack
                ack_holder: List[Optional[dict]] = [None]
                ack_event = threading.Event()

                if self._response_topic:
                    def on_message(_c, _u, msg):
                        try:
                            ack_holder[0] = json.loads(
                                msg.payload.decode('utf-8', errors='ignore'))
                        except (json.JSONDecodeError, ValueError):
                            ack_holder[0] = {'raw': msg.payload.decode(
                                'utf-8', errors='ignore')}
                        ack_event.set()

                    client.on_message = on_message

                client.connect(self._broker, self._port, keepalive=10)

                if self._response_topic:
                    client.subscribe(self._response_topic)

                client.loop_start()

                payload = json.dumps(command.command)
                client.publish(self._topic, payload.encode('utf-8'),
                               qos=self._qos)

                if self._response_topic:
                    ack_event.wait(timeout=5.0)

                client.loop_stop()
                client.disconnect()

                return {
                    'ok': True,
                    'response': ack_holder[0],
                    'topic': self._topic,
                }
            except Exception as exc:
                return {'ok': False, 'error': str(exc)}

    def get_state(self) -> dict:
        # MQTT actuators typically don't support state queries.
        # Return connection metadata instead.
        return {
            'broker': self._broker,
            'topic': self._topic,
            'response_topic': self._response_topic or None,
        }

    def emergency_stop(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return

        with self._lock:
            try:
                client = mqtt.Client()
                if self._username:
                    client.username_pw_set(self._username, self._password)
                client.connect(self._broker, self._port, keepalive=10)
                payload = json.dumps({'action': 'emergency_stop'})
                client.publish(self._topic, payload.encode('utf-8'), qos=2)
                client.disconnect()
            except Exception as exc:
                logger.error("MQTTActuatorAdapter E-stop failed: %s", exc)


# ======================================================================
# Safety Monitor (inline -- used when external safety_monitor is absent)
# ======================================================================


class SafetyMonitor:
    """Validates every actuator command before execution.

    Enforces:
      - Maximum velocity limits
      - Maximum force limits
      - Workspace bounds (3D bounding box)
      - Emergency stop detection and propagation

    This is a lightweight inline monitor.  The full-featured
    ``integrations.robotics.safety_monitor.SafetyMonitor`` takes
    precedence when available (HardwareBridge._safety_gate checks
    for it first).

    Thread-safe.
    """

    DEFAULT_MAX_VELOCITY: float = 2.0      # m/s
    DEFAULT_MAX_FORCE: float = 50.0        # Newtons
    DEFAULT_WORKSPACE_BOUNDS: Dict[str, Tuple[float, float]] = {
        'x': (-5.0, 5.0),
        'y': (-5.0, 5.0),
        'z': (0.0, 3.0),
    }

    def __init__(
        self,
        max_velocity: float = DEFAULT_MAX_VELOCITY,
        max_force: float = DEFAULT_MAX_FORCE,
        workspace_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
    ):
        self._lock = threading.Lock()
        self.max_velocity = max_velocity
        self.max_force = max_force
        self.workspace_bounds = workspace_bounds or dict(self.DEFAULT_WORKSPACE_BOUNDS)
        self._estop_active = False
        self._estop_reason = ''

    @property
    def is_estopped(self) -> bool:
        """Return True if emergency stop is active."""
        with self._lock:
            return self._estop_active

    def trigger_estop(self, reason: str = 'manual') -> None:
        """Activate emergency stop."""
        with self._lock:
            self._estop_active = True
            self._estop_reason = reason
        logger.warning("SafetyMonitor: E-STOP triggered -- %s", reason)

    def clear_estop(self) -> None:
        """Clear emergency stop.  Only human operators should call this."""
        with self._lock:
            self._estop_active = False
            self._estop_reason = ''
        logger.info("SafetyMonitor: E-STOP cleared")

    def check_command(self, command: ActuatorCommand) -> Tuple[bool, str]:
        """Validate a command against all safety constraints.

        Returns (safe, reason).  If safe is False, reason explains why.
        """
        if self.is_estopped:
            return False, f'E-STOP active: {self._estop_reason}'

        params = command.command.get('params', {})

        # Velocity check
        speed = params.get('speed', params.get('velocity'))
        if speed is not None:
            try:
                if abs(float(speed)) > self.max_velocity:
                    return False, (
                        f'velocity {speed} exceeds limit {self.max_velocity} m/s')
            except (TypeError, ValueError):
                pass

        # Force check
        force = params.get('force', params.get('torque'))
        if force is not None:
            try:
                if abs(float(force)) > self.max_force:
                    return False, (
                        f'force {force} exceeds limit {self.max_force} N')
            except (TypeError, ValueError):
                pass

        # Workspace bounds check
        if not self.check_position_safe(params):
            return False, 'position outside workspace bounds'

        return True, ''

    def check_position_safe(self, params: dict) -> bool:
        """Check whether position parameters fall within workspace bounds.

        Supports both Cartesian (x,y,z) and joint (joint_N) keys.
        """
        for axis, (lo, hi) in self.workspace_bounds.items():
            val = params.get(axis)
            if val is not None:
                try:
                    if float(val) < lo or float(val) > hi:
                        return False
                except (TypeError, ValueError):
                    pass
        return True

    def gate_commands(
        self, commands: List[ActuatorCommand],
    ) -> List[ActuatorCommand]:
        """Filter a batch of commands.  Sets safety_cleared on passed ones.

        Returns only commands that passed.
        """
        cleared: List[ActuatorCommand] = []
        for cmd in commands:
            safe, reason = self.check_command(cmd)
            if safe:
                cmd.safety_cleared = True
                cleared.append(cmd)
            else:
                logger.warning(
                    "SafetyMonitor blocked command for %s: %s",
                    cmd.actuator_id, reason,
                )
        return cleared


# Module-level inline safety monitor singleton
_inline_safety: Optional[SafetyMonitor] = None
_inline_safety_lock = threading.Lock()


def get_inline_safety_monitor() -> SafetyMonitor:
    """Get or create the inline SafetyMonitor singleton."""
    global _inline_safety
    if _inline_safety is None:
        with _inline_safety_lock:
            if _inline_safety is None:
                _inline_safety = SafetyMonitor()
    return _inline_safety


# ======================================================================
# Hardware Bridge
# ======================================================================

# Experience buffer limits
_EXPERIENCE_MAX_SIZE = 10_000
_EXPERIENCE_AUTO_FLUSH = 100


class HardwareBridge:
    """Close the loop between physical robots and the hive.

    Owns three responsibilities:

    1. **Inbound** -- collects sensor readings from registered adapters,
       normalizes them, and pushes them to SensorStore + EventBus.
    2. **Outbound** -- translates action plans into actuator commands,
       gates them through SafetyMonitor, and executes them.
    3. **Learning** -- records sensor->action->outcome triples as
       Experiences and flushes them to WorldModelBridge so the hive
       learns from every robot.
    """

    def __init__(self, robot_id: str):
        self._robot_id = robot_id
        self._sensor_adapters: Dict[str, SensorAdapter] = {}
        self._actuator_adapters: Dict[str, ActuatorAdapter] = {}
        self._experience_buffer: deque = deque(maxlen=_EXPERIENCE_MAX_SIZE)
        self._running = False
        self._sensor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._latest_readings: Dict[str, SensorReading] = {}
        self._stats = {
            'readings_received': 0,
            'actions_executed': 0,
            'experiences_recorded': 0,
            'experiences_flushed': 0,
        }

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_sensor(self, adapter: SensorAdapter) -> None:
        """Register a sensor input adapter.

        The adapter's ``sensor_id`` is used as the key.  Registering a
        second adapter with the same ID replaces the first (the old
        stream is stopped).
        """
        with self._lock:
            old = self._sensor_adapters.get(adapter.sensor_id)
            if old is not None:
                try:
                    old.stop_stream()
                except Exception:
                    pass
            self._sensor_adapters[adapter.sensor_id] = adapter
            logger.info(
                "[HardwareBridge:%s] sensor registered: %s (%s)",
                self._robot_id, adapter.sensor_id, adapter.sensor_type,
            )
            # If already running, start the new adapter's stream immediately
            if self._running:
                self._start_adapter_stream(adapter)

    def register_actuator(self, adapter: ActuatorAdapter) -> None:
        """Register an actuator output adapter.

        The adapter's ``actuator_id`` is used as the key.
        """
        with self._lock:
            old = self._actuator_adapters.get(adapter.actuator_id)
            if old is not None:
                try:
                    old.emergency_stop()
                except Exception:
                    pass
            self._actuator_adapters[adapter.actuator_id] = adapter
            logger.info(
                "[HardwareBridge:%s] actuator registered: %s (%s)",
                self._robot_id, adapter.actuator_id, adapter.actuator_type,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all sensor streams and begin experience collection."""
        with self._lock:
            if self._running:
                return
            self._running = True

        logger.info("[HardwareBridge:%s] starting", self._robot_id)

        with self._lock:
            adapters = list(self._sensor_adapters.values())

        for adapter in adapters:
            self._start_adapter_stream(adapter)

    def stop(self) -> None:
        """Stop all sensor streams, emergency-stop actuators, flush experiences."""
        with self._lock:
            if not self._running:
                return
            self._running = False

        logger.info("[HardwareBridge:%s] stopping", self._robot_id)

        # Stop all sensor streams
        with self._lock:
            sensor_adapters = list(self._sensor_adapters.values())
            actuator_adapters = list(self._actuator_adapters.values())

        for adapter in sensor_adapters:
            try:
                adapter.stop_stream()
            except Exception as exc:
                logger.debug("Error stopping sensor %s: %s", adapter.sensor_id, exc)

        # Emergency-stop all actuators
        for adapter in actuator_adapters:
            try:
                adapter.emergency_stop()
            except Exception as exc:
                logger.debug("Error stopping actuator %s: %s", adapter.actuator_id, exc)

        # Flush remaining experiences
        self._flush_experiences()

    # ------------------------------------------------------------------
    # Sensor snapshot
    # ------------------------------------------------------------------

    def get_sensor_snapshot(self) -> dict:
        """Return the latest reading from every registered sensor.

        Returns a dict keyed by sensor_id, values are reading dicts.
        """
        with self._lock:
            snapshot = {}
            for sid, reading in self._latest_readings.items():
                snapshot[sid] = {
                    'sensor_id': reading.sensor_id,
                    'sensor_type': reading.sensor_type,
                    'data': reading.data,
                    'timestamp': reading.timestamp,
                }
            return snapshot

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    def execute_action(self, plan: dict) -> dict:
        """Execute an action plan from the intelligence layer.

        The plan is a dict with ``commands`` -- a list of dicts each
        having at least ``actuator_id`` and ``command``.  Example::

            {
                "commands": [
                    {"actuator_id": "motor_left",  "command": {"action": "move", "params": {"speed": 0.5}}},
                    {"actuator_id": "motor_right", "command": {"action": "move", "params": {"speed": 0.5}}},
                ]
            }

        Every command passes through the safety gate before reaching
        the actuator.

        Returns a result dict with per-actuator outcomes.
        """
        # Resource governor check
        if not self._resource_ok():
            return {'ok': False, 'error': 'resource governor denied', 'results': {}}

        raw_commands = plan.get('commands', [])
        if not raw_commands:
            return {'ok': False, 'error': 'no commands in plan', 'results': {}}

        # Build ActuatorCommand objects
        commands: List[ActuatorCommand] = []
        for entry in raw_commands:
            aid = entry.get('actuator_id', '')
            with self._lock:
                adapter = self._actuator_adapters.get(aid)
            if adapter is None:
                logger.warning(
                    "[HardwareBridge:%s] unknown actuator_id: %s",
                    self._robot_id, aid,
                )
                continue
            commands.append(ActuatorCommand(
                actuator_id=aid,
                actuator_type=adapter.actuator_type,
                command=entry.get('command', {}),
            ))

        if not commands:
            return {'ok': False, 'error': 'no valid actuator targets', 'results': {}}

        # Safety gate -- mandatory
        cleared = self._safety_gate(commands)

        # Execute cleared commands
        results: Dict[str, dict] = {}
        any_ok = False
        for cmd in cleared:
            with self._lock:
                adapter = self._actuator_adapters.get(cmd.actuator_id)
            if adapter is None:
                results[cmd.actuator_id] = {'ok': False, 'error': 'adapter gone'}
                continue
            try:
                result = adapter.execute(cmd)
                results[cmd.actuator_id] = result
                if result.get('ok'):
                    any_ok = True
            except Exception as exc:
                results[cmd.actuator_id] = {'ok': False, 'error': str(exc)}

        with self._lock:
            self._stats['actions_executed'] += 1

        # Emit event
        self._emit('robot.action_executed', {
            'robot_id': self._robot_id,
            'plan': plan,
            'results': results,
        })

        # Record experience: snapshot sensors before and after
        sensor_before = self.get_sensor_snapshot()
        # Small pause to let sensors update with post-action readings
        # (non-blocking, real feedback comes on next sensor cycle)
        outcome = {
            'results': results,
            'any_ok': any_ok,
            'sensor_after': self.get_sensor_snapshot(),
        }
        self._record_experience(
            sensor_state=sensor_before,
            action=plan,
            outcome=outcome,
        )

        return {'ok': any_ok, 'results': results}

    # ------------------------------------------------------------------
    # Think-and-act: full soft+hard cycle
    # ------------------------------------------------------------------

    def think_and_act(self, context: str = '') -> dict:
        """Full loop: sense -> think -> act -> learn.

        1. Read sensor snapshot
        2. Call the intelligence API (intelligence_api.think())
        3. Execute the resulting action plan
        4. Record the experience

        This is the complete soft+hard cycle.

        Args:
            context: Optional textual context for the intelligence layer
                     (e.g. 'navigate to charging station').

        Returns:
            Dict with ``sensors``, ``thought``, ``action_result``, ``experience_count``.
        """
        # 1. Sense
        sensor_snapshot = self.get_sensor_snapshot()

        # 2. Think -- call intelligence API
        thought = self._call_intelligence(sensor_snapshot, context)

        # 3. Act -- execute the action plan from the intelligence layer
        action_plan = thought.get('action_plan', {})
        action_result = {}
        if action_plan and action_plan.get('commands'):
            action_result = self.execute_action(action_plan)
        else:
            # No action plan, but still record the observation experience
            self._record_experience(
                sensor_state=sensor_snapshot,
                action={'noop': True, 'context': context},
                outcome={'thought': thought, 'action_result': None},
            )

        return {
            'robot_id': self._robot_id,
            'sensors': sensor_snapshot,
            'thought': thought,
            'action_result': action_result,
            'experience_count': len(self._experience_buffer),
        }

    # ------------------------------------------------------------------
    # Safety gate
    # ------------------------------------------------------------------

    def _safety_gate(self, commands: List[ActuatorCommand]) -> List[ActuatorCommand]:
        """Filter commands through SafetyMonitor.

        Every command must pass the safety check.  Commands that fail
        are logged and dropped -- they never reach an actuator.

        Prefers the full external SafetyMonitor from safety_monitor.py.
        Falls back to the inline SafetyMonitor defined in this module
        when the external one is not importable.  Safety is
        NON-NEGOTIABLE -- every command is gated.

        Returns the list of commands with ``safety_cleared=True``.
        """
        # Try the full-featured external monitor first
        monitor = None
        try:
            from integrations.robotics.safety_monitor import get_safety_monitor
            monitor = get_safety_monitor()
        except ImportError:
            pass

        # Fall back to inline SafetyMonitor
        if monitor is None:
            monitor = get_inline_safety_monitor()
            logger.debug(
                "[HardwareBridge:%s] using inline SafetyMonitor "
                "(external safety_monitor not available)", self._robot_id,
            )

        # Global E-stop check
        if monitor.is_estopped:
            logger.warning(
                "[HardwareBridge:%s] E-stop active -- all commands blocked",
                self._robot_id,
            )
            return []

        # Gate each command through the monitor
        cleared: List[ActuatorCommand] = []
        for cmd in commands:
            # If the monitor has check_command (inline SafetyMonitor),
            # use it for comprehensive validation.
            if hasattr(monitor, 'check_command'):
                safe, reason = monitor.check_command(cmd)
                if not safe:
                    logger.warning(
                        "[HardwareBridge:%s] command for %s blocked: %s",
                        self._robot_id, cmd.actuator_id, reason,
                    )
                    continue
                cmd.safety_cleared = True
                cleared.append(cmd)
            else:
                # External monitor: position + velocity checks
                params = cmd.command.get('params', {})
                position_keys = {'x', 'y', 'z', 'joint_0', 'joint_1',
                                 'joint_2', 'joint_3', 'joint_4', 'joint_5'}
                position = {k: v for k, v in params.items()
                            if k in position_keys}

                if position:
                    if not monitor.check_position_safe(position):
                        logger.warning(
                            "[HardwareBridge:%s] command for %s blocked: "
                            "position outside workspace limits",
                            self._robot_id, cmd.actuator_id,
                        )
                        continue

                # Velocity sanity check
                speed = params.get('speed', params.get('velocity'))
                if speed is not None:
                    try:
                        if abs(float(speed)) > 10.0:
                            logger.warning(
                                "[HardwareBridge:%s] command for %s blocked: "
                                "speed %.2f exceeds limit",
                                self._robot_id, cmd.actuator_id, float(speed),
                            )
                            continue
                    except (TypeError, ValueError):
                        pass

                cmd.safety_cleared = True
                cleared.append(cmd)

        return cleared

    # ------------------------------------------------------------------
    # Experience recording + flushing
    # ------------------------------------------------------------------

    def _record_experience(
        self,
        sensor_state: dict,
        action: dict,
        outcome: dict,
    ) -> None:
        """Buffer a sensor->action->outcome triple.

        Auto-flushes every ``_EXPERIENCE_AUTO_FLUSH`` experiences.
        """
        reward = self._compute_reward(action, outcome)

        exp = Experience(
            robot_id=self._robot_id,
            sensor_state=sensor_state,
            action_taken=action,
            outcome=outcome,
            reward=reward,
        )

        with self._lock:
            self._experience_buffer.append(exp)
            self._stats['experiences_recorded'] += 1
            buffer_len = len(self._experience_buffer)

        self._emit('robot.experience_recorded', {
            'robot_id': self._robot_id,
            'reward': reward,
            'buffer_size': buffer_len,
        })

        # Auto-flush when buffer reaches threshold
        if buffer_len >= _EXPERIENCE_AUTO_FLUSH:
            self._flush_experiences()

    def _flush_experiences(self) -> int:
        """Push buffered experiences to WorldModelBridge for HevolveAI training.

        Returns the number of experiences successfully flushed.
        """
        with self._lock:
            if not self._experience_buffer:
                return 0
            batch = list(self._experience_buffer)
            self._experience_buffer.clear()

        # Convert to dicts for transport
        experience_dicts = []
        for exp in batch:
            experience_dicts.append({
                'robot_id': exp.robot_id,
                'sensor_state': exp.sensor_state,
                'action_taken': exp.action_taken,
                'outcome': exp.outcome,
                'reward': exp.reward,
                'timestamp': exp.timestamp,
            })

        flushed = 0
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            bridge = get_world_model_bridge()

            # Use ingest_sensor_batch for the sensor component
            sensor_readings = []
            for exp_dict in experience_dicts:
                for sid, reading_data in exp_dict.get('sensor_state', {}).items():
                    sensor_readings.append(reading_data)
            if sensor_readings:
                bridge.ingest_sensor_batch(sensor_readings)

            # Send actions for the action component
            for exp_dict in experience_dicts:
                action = exp_dict.get('action_taken', {})
                if action and not action.get('noop'):
                    bridge.send_action(action)

            flushed = len(batch)
        except ImportError:
            logger.debug(
                "[HardwareBridge:%s] WorldModelBridge unavailable -- "
                "%d experiences dropped", self._robot_id, len(batch),
            )
        except Exception as exc:
            logger.warning(
                "[HardwareBridge:%s] experience flush failed: %s",
                self._robot_id, exc,
            )

        with self._lock:
            self._stats['experiences_flushed'] += flushed

        return flushed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_adapter_stream(self, adapter: SensorAdapter) -> None:
        """Start a single sensor adapter's stream with our ingestion callback."""
        def on_reading(reading: SensorReading):
            self._on_sensor_reading(reading)

        try:
            adapter.start_stream(on_reading)
        except Exception as exc:
            logger.warning(
                "[HardwareBridge:%s] failed to start stream for %s: %s",
                self._robot_id, adapter.sensor_id, exc,
            )

    def _on_sensor_reading(self, reading: SensorReading) -> None:
        """Handle an incoming sensor reading from any adapter."""
        # Resource governor gate
        if not self._resource_ok():
            return

        with self._lock:
            self._latest_readings[reading.sensor_id] = reading
            self._stats['readings_received'] += 1

        # Push to SensorStore (existing robotics infrastructure)
        try:
            from integrations.robotics.sensor_store import get_sensor_store
            from integrations.robotics.sensor_model import SensorReading as StoreSensorReading

            store = get_sensor_store()
            store_reading = StoreSensorReading(
                sensor_id=reading.sensor_id,
                sensor_type=reading.sensor_type,
                data=reading.data if isinstance(reading.data, dict) else {'value': reading.data},
                source='hardware_bridge',
            )
            store.put_reading(store_reading)
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("SensorStore push failed: %s", exc)

        # Emit event
        self._emit('robot.sensor_reading', {
            'robot_id': self._robot_id,
            'sensor_id': reading.sensor_id,
            'sensor_type': reading.sensor_type,
            'data': reading.data,
            'timestamp': reading.timestamp,
        })

    def _call_intelligence(self, sensor_snapshot: dict, context: str) -> dict:
        """Call the intelligence API for multi-intelligence fusion.

        Falls back gracefully if the intelligence API is not available.
        """
        try:
            from integrations.robotics.intelligence_api import think
            return think(
                robot_id=self._robot_id,
                sensor_snapshot=sensor_snapshot,
                context=context,
            )
        except ImportError:
            logger.debug(
                "[HardwareBridge:%s] intelligence_api not available, "
                "returning empty thought", self._robot_id,
            )
            return {
                'action_plan': {},
                'reasoning': 'intelligence API not available',
            }
        except Exception as exc:
            logger.warning(
                "[HardwareBridge:%s] intelligence call failed: %s",
                self._robot_id, exc,
            )
            return {
                'action_plan': {},
                'reasoning': f'intelligence error: {exc}',
            }

    @staticmethod
    def _compute_reward(action: dict, outcome: dict) -> float:
        """Compute a simple reward signal from action and outcome.

        Basic heuristic: +1.0 for success, -0.5 for failure, 0.0 for noop.
        Real reward shaping belongs in HevolveAI -- this is just a bootstrap
        signal so the experience buffer carries a non-zero reward.
        """
        if action.get('noop'):
            return 0.0
        results = outcome.get('results', {})
        if not results:
            return 0.0
        successes = sum(1 for r in results.values() if r.get('ok'))
        total = len(results)
        if total == 0:
            return 0.0
        ratio = successes / total
        # Scale: all success = +1.0, all fail = -0.5, mixed = proportional
        return ratio * 1.5 - 0.5

    @staticmethod
    def _resource_ok() -> bool:
        """Check resource governor.  Returns True if processing is allowed."""
        try:
            from core.resource_governor import should_proceed
            return should_proceed('cpu_heavy')
        except ImportError:
            return True  # No governor = no throttling

    @staticmethod
    def _emit(topic: str, data: Any) -> None:
        """Emit an event on the platform EventBus (best-effort)."""
        try:
            from core.platform.events import emit_event
            emit_event(topic, data)
        except ImportError:
            pass
        except Exception:
            pass

    def get_stats(self) -> dict:
        """Return bridge statistics for monitoring."""
        with self._lock:
            return {
                'robot_id': self._robot_id,
                'sensors_registered': len(self._sensor_adapters),
                'actuators_registered': len(self._actuator_adapters),
                'experience_buffer_size': len(self._experience_buffer),
                'running': self._running,
                **dict(self._stats),
            }


# ======================================================================
# Module-level bridge registry
# ======================================================================

_bridges: Dict[str, HardwareBridge] = {}
_bridges_lock = threading.Lock()


def get_bridge(robot_id: str) -> HardwareBridge:
    """Get or create a HardwareBridge for the given robot ID.

    Thread-safe.  Creates a new bridge on first access for each robot_id.
    """
    if robot_id not in _bridges:
        with _bridges_lock:
            if robot_id not in _bridges:
                _bridges[robot_id] = HardwareBridge(robot_id)
    return _bridges[robot_id]


def list_bridges() -> List[str]:
    """Return all registered robot IDs."""
    with _bridges_lock:
        return list(_bridges.keys())


# ======================================================================
# Flask Blueprint
# ======================================================================


def _create_blueprint():
    """Create the Flask blueprint for hardware bridge endpoints.

    Lazy import to avoid requiring Flask at module load time.
    """
    try:
        from flask import Blueprint, jsonify, request
    except ImportError:
        return None

    hardware_bp = Blueprint('hardware_bridge', __name__)

    @hardware_bp.route('/api/robot/<robot_id>/sensors/register', methods=['POST'])
    def register_sensor_endpoint(robot_id: str):
        """Register a sensor adapter via HTTP.

        Body: {
            "sensor_id": "cam_front",
            "sensor_type": "camera",
            "adapter_type": "http",          # http | mqtt | serial
            "config": {                       # adapter-specific config
                "url": "http://...",
                "poll_interval": 1.0
            }
        }
        """
        body = request.get_json(silent=True) or {}
        sensor_id = body.get('sensor_id', '')
        sensor_type = body.get('sensor_type', '')
        adapter_type = body.get('adapter_type', '')
        config = body.get('config', {})

        if not sensor_id or not sensor_type:
            return jsonify({'error': 'sensor_id and sensor_type required'}), 400

        adapter = _build_sensor_adapter(sensor_id, sensor_type, adapter_type, config)
        if adapter is None:
            return jsonify({'error': f'unknown adapter_type: {adapter_type}'}), 400

        bridge = get_bridge(robot_id)
        bridge.register_sensor(adapter)
        return jsonify({'ok': True, 'sensor_id': sensor_id}), 200

    @hardware_bp.route('/api/robot/<robot_id>/actuators/register', methods=['POST'])
    def register_actuator_endpoint(robot_id: str):
        """Register an actuator adapter via HTTP.

        Body: {
            "actuator_id": "motor_left",
            "actuator_type": "motor",
            "adapter_type": "serial",        # serial | http
            "config": {
                "port": "/dev/ttyUSB0",
                "baudrate": 115200
            }
        }
        """
        body = request.get_json(silent=True) or {}
        actuator_id = body.get('actuator_id', '')
        actuator_type = body.get('actuator_type', '')
        adapter_type = body.get('adapter_type', '')
        config = body.get('config', {})

        if not actuator_id or not actuator_type:
            return jsonify({'error': 'actuator_id and actuator_type required'}), 400

        adapter = _build_actuator_adapter(
            actuator_id, actuator_type, adapter_type, config,
        )
        if adapter is None:
            return jsonify({'error': f'unknown adapter_type: {adapter_type}'}), 400

        bridge = get_bridge(robot_id)
        bridge.register_actuator(adapter)
        return jsonify({'ok': True, 'actuator_id': actuator_id}), 200

    @hardware_bp.route('/api/robot/<robot_id>/act', methods=['POST'])
    def act_endpoint(robot_id: str):
        """Execute an action plan.

        Body: {
            "commands": [
                {"actuator_id": "motor_left", "command": {"action": "move", "params": {"speed": 0.5}}}
            ]
        }
        """
        body = request.get_json(silent=True) or {}
        bridge = get_bridge(robot_id)
        result = bridge.execute_action(body)
        status = 200 if result.get('ok') else 400
        return jsonify(result), status

    @hardware_bp.route('/api/robot/<robot_id>/think_and_act', methods=['POST'])
    def think_and_act_endpoint(robot_id: str):
        """Full loop: sense -> think -> act -> learn.

        Body: {"context": "optional textual context"}
        """
        body = request.get_json(silent=True) or {}
        context = body.get('context', '')
        bridge = get_bridge(robot_id)
        result = bridge.think_and_act(context=context)
        return jsonify(result), 200

    @hardware_bp.route('/api/robot/<robot_id>/sensors/snapshot', methods=['GET'])
    def sensors_snapshot_endpoint(robot_id: str):
        """Get current state of all sensors."""
        bridge = get_bridge(robot_id)
        return jsonify(bridge.get_sensor_snapshot()), 200

    @hardware_bp.route('/api/robot/<robot_id>/experience/stats', methods=['GET'])
    def experience_stats_endpoint(robot_id: str):
        """Get experience buffer statistics."""
        bridge = get_bridge(robot_id)
        return jsonify(bridge.get_stats()), 200

    return hardware_bp


def _build_sensor_adapter(
    sensor_id: str,
    sensor_type: str,
    adapter_type: str,
    config: dict,
) -> Optional[SensorAdapter]:
    """Factory for sensor adapters from HTTP registration payloads."""
    adapter_type = adapter_type.lower()
    if adapter_type == 'http':
        return HTTPSensorAdapter(
            sensor_id=sensor_id,
            sensor_type=sensor_type,
            url=config.get('url', ''),
            poll_interval=config.get('poll_interval', 1.0),
            timeout=config.get('timeout', 5.0),
            headers=config.get('headers'),
        )
    elif adapter_type == 'mqtt':
        return MQTTSensorAdapter(
            sensor_id=sensor_id,
            sensor_type=sensor_type,
            broker=config.get('broker', 'localhost'),
            port=config.get('port', 1883),
            topic=config.get('topic', ''),
            username=config.get('username', ''),
            password=config.get('password', ''),
        )
    elif adapter_type == 'serial':
        return SerialSensorAdapter(
            sensor_id=sensor_id,
            sensor_type=sensor_type,
            port=config.get('port', ''),
            baudrate=config.get('baudrate', 115200),
            timeout=config.get('timeout', 0.1),
        )
    elif adapter_type in ('websocket', 'ws'):
        return WebSocketSensorAdapter(
            sensor_id=sensor_id,
            sensor_type=sensor_type,
            url=config.get('url', ''),
            headers=config.get('headers'),
            reconnect_delay=config.get('reconnect_delay', 2.0),
        )
    return None


def _build_actuator_adapter(
    actuator_id: str,
    actuator_type: str,
    adapter_type: str,
    config: dict,
) -> Optional[ActuatorAdapter]:
    """Factory for actuator adapters from HTTP registration payloads."""
    adapter_type = adapter_type.lower()
    if adapter_type == 'serial':
        return SerialActuatorAdapter(
            actuator_id=actuator_id,
            actuator_type=actuator_type,
            port=config.get('port', ''),
            baudrate=config.get('baudrate', 115200),
            timeout=config.get('timeout', 1.0),
        )
    elif adapter_type == 'http':
        return HTTPActuatorAdapter(
            actuator_id=actuator_id,
            actuator_type=actuator_type,
            url=config.get('url', ''),
            timeout=config.get('timeout', 5.0),
            headers=config.get('headers'),
        )
    elif adapter_type == 'mqtt':
        return MQTTActuatorAdapter(
            actuator_id=actuator_id,
            actuator_type=actuator_type,
            broker=config.get('broker', 'localhost'),
            port=config.get('port', 1883),
            topic=config.get('topic', ''),
            response_topic=config.get('response_topic', ''),
            username=config.get('username', ''),
            password=config.get('password', ''),
            qos=config.get('qos', 1),
        )
    return None


# ======================================================================
# Unified Robotics Blueprint (/api/robotics/...)
# ======================================================================


def create_robotics_blueprint():
    """Create the Flask blueprint for unified robotics endpoints.

    Exposes hive-wide robotics operations at ``/api/robotics/...``.
    Per-robot endpoints live on the ``_create_blueprint()`` blueprint above
    at ``/api/robot/<robot_id>/...``.

    Routes:
        GET  /api/robotics/status      -- all bridges status
        POST /api/robotics/think       -- trigger think_and_act
        GET  /api/robotics/sensors     -- all sensor readings (all robots)
        POST /api/robotics/command     -- direct actuator command (safety-gated)
    """
    try:
        from flask import Blueprint, jsonify, request
    except ImportError:
        return None

    robotics_bp = Blueprint('robotics', __name__)

    @robotics_bp.route('/api/robotics/status', methods=['GET'])
    def robotics_status():
        """GET /api/robotics/status -- all bridges status."""
        robot_ids = list_bridges()
        bridge_stats = []
        for rid in robot_ids:
            bridge = get_bridge(rid)
            bridge_stats.append(bridge.get_stats())
        return jsonify({
            'robot_count': len(robot_ids),
            'robots': bridge_stats,
        })

    @robotics_bp.route('/api/robotics/think', methods=['POST'])
    def robotics_think():
        """POST /api/robotics/think -- trigger think_and_act on a robot.

        Body: {
            "robot_id": "my-robot",
            "context": "navigate to kitchen"
        }
        """
        body = request.get_json(silent=True) or {}
        robot_id = body.get('robot_id', '')
        if not robot_id:
            return jsonify({'error': 'robot_id is required'}), 400
        context = body.get('context', '')
        bridge = get_bridge(robot_id)
        result = bridge.think_and_act(context=context)
        return jsonify(result)

    @robotics_bp.route('/api/robotics/sensors', methods=['GET'])
    def robotics_sensors():
        """GET /api/robotics/sensors -- all sensor readings from all robots."""
        robot_ids = list_bridges()
        all_readings: Dict[str, dict] = {}
        for rid in robot_ids:
            bridge = get_bridge(rid)
            snapshot = bridge.get_sensor_snapshot()
            if snapshot:
                all_readings[rid] = snapshot
        return jsonify({
            'robot_count': len(robot_ids),
            'readings': all_readings,
        })

    @robotics_bp.route('/api/robotics/command', methods=['POST'])
    def robotics_command():
        """POST /api/robotics/command -- direct actuator command (safety-gated).

        Body: {
            "robot_id": "my-robot",
            "commands": [
                {"actuator_id": "motor_left", "command": {"action": "move", "params": {"speed": 0.5}}}
            ]
        }
        """
        body = request.get_json(silent=True) or {}
        robot_id = body.get('robot_id', '')
        if not robot_id:
            return jsonify({'error': 'robot_id is required'}), 400
        bridge = get_bridge(robot_id)
        result = bridge.execute_action(body)
        status = 200 if result.get('ok') else 400
        return jsonify(result), status

    return robotics_bp
