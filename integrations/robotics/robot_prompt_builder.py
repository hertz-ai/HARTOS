"""
Robot Goal Prompt Builder — Builds prompts for 'robot' goal type.

Injects:
  - Robot capabilities (what this robot can do)
  - Available tools (what the agent can command)
  - Safety constraints (workspace limits, E-stop status)
  - Active sensor state (what the robot currently sees/feels)

The prompt tells the LLM agent WHAT it can do.
HevolveAI figures out HOW to do it.
"""
import json
import logging
from typing import Dict, Optional

logger = logging.getLogger('hevolve_robotics')


def build_robot_prompt(goal_dict: Dict,
                       product_dict: Optional[Dict] = None) -> str:
    """Build a /chat prompt for a robot goal.

    The agent receives:
      1. What hardware is available (capabilities)
      2. What tools it can use (navigate_to, move_joint, etc.)
      3. Current safety status (E-stop, workspace limits)
      4. Current sensor state (what's live right now)
      5. The goal itself

    The agent decides WHAT to do.  HevolveAI decides HOW.
    """
    config = goal_dict.get('config', goal_dict.get('config_json', {})) or {}

    # Guard: skip if no robot bridge is connected. Without an actual robot,
    # the agent loops trying to call get_robot_status() which fails, wastes
    # LLM budget, and gets killed by the watchdog.
    try:
        from integrations.robotics.capability_advertiser import get_capability_advertiser
        caps = get_capability_advertiser().get_capabilities()
        has_any = (caps.get('locomotion') or caps.get('manipulation')
                   or any(v for v in caps.get('sensors', {}).values()))
        if not has_any:
            import logging
            logging.getLogger('hevolve_social').info(
                f"Robot goal '{goal_dict.get('title', '')}': skipping — "
                f"no locomotion, manipulation, or sensors detected")
            return None
    except Exception:
        pass  # Capability advertiser unavailable — continue with fallback prompt

    # Capabilities
    caps_section = _get_capabilities_section()

    # Safety
    safety_section = _get_safety_section()

    # Sensors
    sensor_section = _get_sensor_section()

    return (
        f"YOU ARE A ROBOT TASK AGENT.\n\n"
        f"You control a physical robot through the HART platform. "
        f"You decide WHAT the robot should do. The native embodiment layer "
        f"(HevolveAI) handles HOW — motor control, path planning, sensor fusion, "
        f"kinematics. You never compute trajectories or PID values yourself.\n\n"
        f"{caps_section}\n"
        f"{safety_section}\n"
        f"{sensor_section}\n"
        f"YOUR TOOLS:\n"
        f"  navigate_to(x, y, z) — Move the robot to a position\n"
        f"  move_joint(joint_id, position, velocity) — Move a specific joint\n"
        f"  execute_motion_sequence(steps) — Execute a series of actions\n"
        f"  read_sensor(sensor_id) — Read a sensor value\n"
        f"  get_sensor_window(sensor_id, duration_s) — Get sensor history\n"
        f"  get_robot_capabilities() — Query full capability set\n"
        f"  get_robot_status() — Get safety + sensor + bridge status\n"
        f"  configure_sensor(sensor_id, config) — Adjust sensor parameters\n\n"
        f"GOAL:\n"
        f"  Title: {goal_dict.get('title', '')}\n"
        f"  Description: {goal_dict.get('description', '')}\n\n"
        f"RULES:\n"
        f"  - ALWAYS check get_robot_status() before starting physical actions\n"
        f"  - If E-stop is active, do NOT send any motion commands\n"
        f"  - Use read_sensor() to verify outcomes after actions\n"
        f"  - If a motion fails, check safety status before retrying\n"
        f"  - Record outcomes with save_data_in_memory for recipe learning\n"
        f"  - Never compute trajectories — let the native layer handle it\n"
    )


def _get_capabilities_section() -> str:
    """Build capabilities section from the advertiser."""
    try:
        from integrations.robotics.capability_advertiser import (
            get_capability_advertiser,
        )
        adv = get_capability_advertiser()
        caps = adv.get_capabilities()

        lines = ["ROBOT CAPABILITIES:"]
        lines.append(f"  Form factor: {caps.get('form_factor', 'unknown')}")

        if caps.get('locomotion'):
            loc = caps['locomotion']
            lines.append(f"  Locomotion: {loc.get('type', 'yes')} "
                         f"(max speed: {loc.get('max_speed', 'N/A')})")
        else:
            lines.append("  Locomotion: NONE (stationary)")

        if caps.get('manipulation'):
            manip = caps['manipulation']
            lines.append(f"  Manipulation: {manip.get('arms', 0)} arm(s), "
                         f"{manip.get('grippers', 0)} gripper(s), "
                         f"{manip.get('dof', 'N/A')} DOF")
        else:
            lines.append("  Manipulation: NONE")

        sensors = [k for k, v in caps.get('sensors', {}).items() if v]
        lines.append(f"  Sensors: {', '.join(sensors) if sensors else 'none detected'}")

        if caps.get('actuators'):
            lines.append(f"  Actuators: {', '.join(caps['actuators'])}")

        if caps.get('payload_kg') is not None:
            lines.append(f"  Payload: {caps['payload_kg']} kg")

        if caps.get('native_skills'):
            lines.append(f"  Native skills: {', '.join(caps['native_skills'])}")

        return '\n'.join(lines)
    except Exception:
        return "ROBOT CAPABILITIES: detection unavailable"


def _get_safety_section() -> str:
    """Build safety status section."""
    try:
        from integrations.robotics.safety_monitor import get_safety_monitor
        monitor = get_safety_monitor()
        status = monitor.get_safety_status()

        lines = ["SAFETY STATUS:"]
        if status.get('is_estopped'):
            lines.append(f"  *** E-STOP ACTIVE: {status.get('estop_reason', 'unknown')} ***")
            lines.append("  NO MOTION COMMANDS WILL BE ACCEPTED")
        else:
            lines.append("  E-stop: CLEAR")

        if status.get('workspace_limits'):
            limits = status['workspace_limits']
            lines.append(f"  Workspace limits: "
                         f"x=[{limits.get('x_min', '-inf')}, {limits.get('x_max', 'inf')}] "
                         f"y=[{limits.get('y_min', '-inf')}, {limits.get('y_max', 'inf')}] "
                         f"z=[{limits.get('z_min', '-inf')}, {limits.get('z_max', 'inf')}]")
        return '\n'.join(lines)
    except Exception:
        return "SAFETY STATUS: monitor unavailable"


def _get_sensor_section() -> str:
    """Build active sensor section."""
    try:
        from integrations.robotics.sensor_store import get_sensor_store
        store = get_sensor_store()
        active = store.active_sensors()

        if not active:
            return "ACTIVE SENSORS: none"

        lines = ["ACTIVE SENSORS:"]
        for sensor_id, info in active.items():
            lines.append(f"  {sensor_id}: type={info.get('sensor_type', '?')}, "
                         f"readings={info.get('count', 0)}")
        return '\n'.join(lines)
    except Exception:
        return "ACTIVE SENSORS: store unavailable"
