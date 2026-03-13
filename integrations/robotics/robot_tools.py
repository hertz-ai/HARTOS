"""
Robot AutoGen Tools — Agentic interface to physical actions.

Every tool is a thin routing wrapper:
  validate input → WorldModelBridge → return result

NO intelligence here.  No path planning.  No sensor fusion.
HevolveAI owns those.  These tools let an LLM agent say "go there"
and the bridge routes it to HevolveAI which figures out how.

Tool categories:
  - Query: get_robot_capabilities, read_sensor, get_sensor_window, get_robot_status
  - Action: navigate_to, move_joint, execute_motion_sequence
  - Config: configure_sensor
"""
import json
import logging
from typing import Any, Dict

logger = logging.getLogger('hevolve_robotics')


def get_robot_capabilities(**kwargs) -> str:
    """Get this robot's advertised capabilities.

    Returns JSON with: locomotion, manipulation, sensors, actuators,
    form_factor, native_skills, workspace, payload_kg, battery.
    """
    try:
        from integrations.robotics.capability_advertiser import (
            get_capability_advertiser,
        )
        adv = get_capability_advertiser()
        caps = adv.get_capabilities()
        return json.dumps(caps, default=str)
    except Exception as e:
        return json.dumps({'error': str(e)})


def read_sensor(sensor_id: str = '', **kwargs) -> str:
    """Read the latest value from a specific sensor.

    Args:
        sensor_id: The sensor identifier (e.g. 'imu_0', 'gps_0').

    Returns JSON with the latest SensorReading or error.
    """
    if not sensor_id:
        return json.dumps({'error': 'sensor_id is required'})
    try:
        from integrations.robotics.sensor_store import get_sensor_store
        store = get_sensor_store()
        reading = store.get_latest(sensor_id)
        if reading is None:
            return json.dumps({'error': f'No data for sensor {sensor_id}'})
        return json.dumps(reading.to_dict())
    except Exception as e:
        return json.dumps({'error': str(e)})


def get_sensor_window(sensor_id: str = '', duration_s: float = 1.0,
                      **kwargs) -> str:
    """Get a time window of sensor readings.

    Args:
        sensor_id: The sensor identifier.
        duration_s: Window duration in seconds (default 1.0).

    Returns JSON list of SensorReading dicts.
    """
    if not sensor_id:
        return json.dumps({'error': 'sensor_id is required'})
    try:
        from integrations.robotics.sensor_store import get_sensor_store
        store = get_sensor_store()
        readings = store.get_window(sensor_id, duration_s)
        return json.dumps([r.to_dict() for r in readings])
    except Exception as e:
        return json.dumps({'error': str(e)})


def get_robot_status(**kwargs) -> str:
    """Get overall robot status: safety, sensors, bridge health.

    Returns JSON with safety_status, active_sensors, bridge_stats.
    """
    status: Dict[str, Any] = {}

    # Safety
    try:
        from integrations.robotics.safety_monitor import get_safety_monitor
        monitor = get_safety_monitor()
        status['safety'] = monitor.get_safety_status()
    except Exception:
        status['safety'] = {'available': False}

    # Sensors
    try:
        from integrations.robotics.sensor_store import get_sensor_store
        store = get_sensor_store()
        status['active_sensors'] = list(store.active_sensors().keys())
        status['sensor_stats'] = store.stats()
    except Exception:
        status['active_sensors'] = []

    # Bridge
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        status['bridge'] = bridge.get_stats()
    except Exception:
        status['bridge'] = {'available': False}

    return json.dumps(status, default=str)


def navigate_to(x: float = 0.0, y: float = 0.0, z: float = 0.0,
                **kwargs) -> str:
    """Send a navigate_to action through the world model bridge.

    HevolveAI owns the actual path planning, obstacle avoidance, and
    motor control.  This tool just says "go to (x, y, z)".

    Args:
        x, y, z: Target position in robot frame (meters).

    Returns JSON with success status.
    """
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        result = bridge.send_action({
            'type': 'navigate_to',
            'target': 'base',
            'params': {'x': float(x), 'y': float(y), 'z': float(z)},
        })
        return json.dumps({'success': result})
    except Exception as e:
        return json.dumps({'error': str(e)})


def move_joint(joint_id: str = '', position: float = 0.0,
               velocity: float = 0.0, **kwargs) -> str:
    """Send a joint move command through the world model bridge.

    HevolveAI owns the actual kinematics and PID control.
    This tool just says "move joint X to position Y".

    Args:
        joint_id: Joint identifier (e.g. 'shoulder', 'elbow', 'wrist').
        position: Target position (radians or meters depending on joint type).
        velocity: Optional velocity limit.

    Returns JSON with success status.
    """
    if not joint_id:
        return json.dumps({'error': 'joint_id is required'})
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        params = {'position': float(position)}
        if velocity:
            params['velocity'] = float(velocity)
        result = bridge.send_action({
            'type': 'servo_position',
            'target': joint_id,
            'params': params,
        })
        return json.dumps({'success': result})
    except Exception as e:
        return json.dumps({'error': str(e)})


def execute_motion_sequence(steps: str = '[]', **kwargs) -> str:
    """Execute a sequence of actions through the world model bridge.

    Each step is sent in order.  HevolveAI handles the actual execution,
    timing, and real-time adaptation (pause on obstacle, etc.).

    Args:
        steps: JSON string — list of action dicts, each with
               {type, target, params}.

    Returns JSON with results per step.
    """
    try:
        step_list = json.loads(steps) if isinstance(steps, str) else steps
    except json.JSONDecodeError:
        return json.dumps({'error': 'steps must be valid JSON array'})

    if not isinstance(step_list, list) or not step_list:
        return json.dumps({'error': 'steps must be a non-empty list'})

    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        results = []
        for i, step in enumerate(step_list):
            ok = bridge.send_action(step)
            results.append({'step': i, 'success': ok})
            if not ok:
                break  # Stop sequence on failure
        return json.dumps({'results': results})
    except Exception as e:
        return json.dumps({'error': str(e)})


def configure_sensor(sensor_id: str = '', config: str = '{}',
                     **kwargs) -> str:
    """Configure a sensor's parameters via the world model bridge.

    The actual sensor configuration is handled by HevolveAI's native layer.
    This tool routes the configuration request through the bridge.

    Args:
        sensor_id: The sensor to configure.
        config: JSON string with configuration parameters.

    Returns JSON with success status.
    """
    if not sensor_id:
        return json.dumps({'error': 'sensor_id is required'})
    try:
        cfg = json.loads(config) if isinstance(config, str) else config
    except json.JSONDecodeError:
        return json.dumps({'error': 'config must be valid JSON'})

    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        result = bridge.send_action({
            'type': 'sensor_config',
            'target': sensor_id,
            'params': cfg,
        })
        return json.dumps({'success': result})
    except Exception as e:
        return json.dumps({'error': str(e)})


# ── Tool registration list for AutoGen ─────────────────────────

ROBOT_TOOLS = [
    {
        'function': get_robot_capabilities,
        'name': 'get_robot_capabilities',
        'description': (
            'Get this robot\'s hardware capabilities: locomotion, '
            'manipulation, sensors, actuators, form factor, native skills.'
        ),
    },
    {
        'function': read_sensor,
        'name': 'read_sensor',
        'description': 'Read the latest value from a specific sensor by ID.',
    },
    {
        'function': get_sensor_window,
        'name': 'get_sensor_window',
        'description': (
            'Get a time window of sensor readings for analysis. '
            'Returns the last N seconds of data.'
        ),
    },
    {
        'function': get_robot_status,
        'name': 'get_robot_status',
        'description': (
            'Get overall robot status: safety state, active sensors, '
            'bridge health, and capability summary.'
        ),
    },
    {
        'function': navigate_to,
        'name': 'navigate_to',
        'description': (
            'Navigate the robot to a target position (x, y, z). '
            'Path planning and obstacle avoidance are handled by the '
            'native embodiment layer.'
        ),
    },
    {
        'function': move_joint,
        'name': 'move_joint',
        'description': (
            'Move a specific joint to a target position. '
            'Kinematics and PID control are handled natively.'
        ),
    },
    {
        'function': execute_motion_sequence,
        'name': 'execute_motion_sequence',
        'description': (
            'Execute a sequence of motor actions in order. '
            'Each step is sent to the native layer for execution.'
        ),
    },
    {
        'function': configure_sensor,
        'name': 'configure_sensor',
        'description': (
            'Configure a sensor\'s parameters (sample rate, range, etc). '
            'Configuration is applied by the native layer.'
        ),
    },
]
