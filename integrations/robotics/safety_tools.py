"""
Safety Tools — AutoGen tools for robot safety management.

4 tools for the unified agent goal engine:
  - configure_workspace_limits
  - get_safety_status
  - test_estop
  - configure_estop_sources
"""
import json
import logging
from typing import Dict

logger = logging.getLogger('hevolve_robotics')


def configure_workspace_limits(limits_json: str) -> str:
    """Set operational domain bounds for the robot.

    Args:
        limits_json: JSON string with axis limits, e.g.
            '{"x": [-1.0, 1.0], "y": [-0.5, 0.5], "z": [0.0, 1.2],
              "joint_limits": {"joint_0": [-90, 90]}}'

    Returns:
        Status message.
    """
    try:
        limits = json.loads(limits_json)
    except (json.JSONDecodeError, TypeError) as e:
        return f"Error: invalid JSON — {e}"

    from integrations.robotics.safety_monitor import get_safety_monitor
    monitor = get_safety_monitor()
    monitor.register_workspace_limits(limits)
    return f"Workspace limits configured: {list(limits.keys())}"


def get_safety_status() -> str:
    """Query current safety state including E-stop status and workspace limits.

    Returns:
        JSON string with full safety status.
    """
    from integrations.robotics.safety_monitor import get_safety_monitor
    monitor = get_safety_monitor()
    status = monitor.get_safety_status()
    return json.dumps(status, indent=2, default=str)


def test_estop(confirm: str = 'false') -> str:
    """Trigger and immediately clear a test E-stop.

    Only works when confirm='true'.  Used for safety verification.

    Args:
        confirm: Must be 'true' to execute the test.

    Returns:
        Status message.
    """
    if confirm.lower() != 'true':
        return "Test E-stop not executed. Pass confirm='true' to proceed."

    from integrations.robotics.safety_monitor import get_safety_monitor
    monitor = get_safety_monitor()

    # Trigger
    monitor.trigger_estop('Safety test — automatic clear follows', source='test')

    # Immediately clear (test operator)
    cleared = monitor.clear_estop('test_operator_safety_check')

    if cleared:
        return "Test E-stop: triggered and cleared successfully. Safety system operational."
    else:
        return "Test E-stop: triggered but clear FAILED. Manual intervention required."


def configure_estop_sources(config_json: str) -> str:
    """Register GPIO pins and/or serial ports as E-stop sources.

    Args:
        config_json: JSON string, e.g.
            '{"gpio_pins": [17, 27], "serial": [{"port": "/dev/ttyUSB0", "pattern": "ESTOP"}]}'

    Returns:
        Status message.
    """
    try:
        config = json.loads(config_json)
    except (json.JSONDecodeError, TypeError) as e:
        return f"Error: invalid JSON — {e}"

    from integrations.robotics.safety_monitor import get_safety_monitor
    monitor = get_safety_monitor()

    registered = []

    for pin in config.get('gpio_pins', []):
        monitor.register_estop_pin(int(pin))
        registered.append(f'GPIO pin {pin}')

    for serial_cfg in config.get('serial', []):
        port = serial_cfg.get('port', '')
        pattern = serial_cfg.get('pattern', 'ESTOP')
        if port:
            monitor.register_estop_serial(port, pattern)
            registered.append(f'Serial {port} (pattern={pattern})')

    if registered:
        monitor.start()  # Start monitor if not already running
        return f"E-stop sources registered: {', '.join(registered)}"
    return "No E-stop sources configured."


# Tool metadata for AutoGen registration
SAFETY_TOOLS = [
    {
        'name': 'configure_workspace_limits',
        'func': configure_workspace_limits,
        'description': (
            'Set operational domain bounds for the robot. '
            'Input: JSON with axis limits {x: [min, max], y: [...], z: [...], '
            'joint_limits: {name: [min, max]}}.'
        ),
    },
    {
        'name': 'get_safety_status',
        'func': get_safety_status,
        'description': (
            'Query current robot safety state: E-stop status, workspace limits, '
            'audit trail, monitor status.'
        ),
    },
    {
        'name': 'test_estop',
        'func': test_estop,
        'description': (
            'Trigger and immediately clear a test E-stop to verify safety system. '
            "Pass confirm='true' to execute."
        ),
    },
    {
        'name': 'configure_estop_sources',
        'func': configure_estop_sources,
        'description': (
            'Register GPIO pins and/or serial ports as hardware E-stop sources. '
            'Input: JSON with gpio_pins and/or serial arrays.'
        ),
    },
]
