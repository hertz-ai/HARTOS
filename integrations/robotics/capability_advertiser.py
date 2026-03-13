"""
Robot Capability Advertiser — Discovery + Advertisement for fleet dispatch.

Answers: "What can this robot DO?" — by combining:
  1. system_requirements.py HardwareProfile (CPU, RAM, GPU, sensors)
  2. SensorStore.active_sensors() (what sensors are currently live)
  3. robot_config.json (static configuration — arm DOF, payload, form factor)
  4. HevolveAI capability query via WorldModelBridge (what native skills exist)

This is DISCOVERY, not intelligence.  HevolveAI owns the actual capabilities.
We just ask it what it can do, and advertise that to the fleet.

The match_score() method lets dispatch route tasks to the right robot:
  "Navigate to warehouse 3" → match against locomotion capability.
  "Pick up the box" → match against manipulation.gripper capability.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve_robotics')


class RobotCapabilityAdvertiser:
    """Discovers and advertises robot capabilities for fleet dispatch.

    NOT intelligence.  Just a structured query of what this node can do.
    """

    def __init__(self):
        self._capabilities: Dict[str, Any] = {}
        self._detected = False

    def detect_capabilities(self) -> Dict[str, Any]:
        """Detect all capabilities from hardware profile, sensors, config, HevolveAI.

        Returns a structured dict:
            locomotion: {type, max_speed, ...} or None
            manipulation: {arms, grippers, dof, ...} or None
            sensors: {imu: True, gps: True, lidar: False, ...}
            actuators: [list of actuator IDs]
            workspace: {x_min, x_max, y_min, y_max, z_min, z_max} or None
            payload_kg: float or None
            battery: {voltage, capacity_wh} or None
            form_factor: str (rover, arm, drone, humanoid, stationary, unknown)
            native_skills: [list from HevolveAI] or []
        """
        caps: Dict[str, Any] = {
            'locomotion': None,
            'manipulation': None,
            'sensors': {},
            'actuators': [],
            'workspace': None,
            'payload_kg': None,
            'battery': None,
            'form_factor': 'unknown',
            'native_skills': [],
        }

        # 1. Hardware profile (system_requirements.py)
        self._detect_from_hardware_profile(caps)

        # 2. Active sensors (SensorStore)
        self._detect_from_sensor_store(caps)

        # 3. Static config (robot_config.json)
        self._detect_from_config_file(caps)

        # 4. HevolveAI native capabilities (via WorldModelBridge)
        self._detect_from_hevolveai(caps)

        self._capabilities = caps
        self._detected = True
        return caps

    def get_capabilities(self) -> Dict[str, Any]:
        """Return cached capabilities, detecting if needed."""
        if not self._detected:
            self.detect_capabilities()
        return self._capabilities

    def get_gossip_payload(self) -> Dict[str, Any]:
        """Compact capability summary for gossip beacon.

        Keeps it small for constrained bandwidth profiles.
        """
        caps = self.get_capabilities()
        return {
            'form_factor': caps.get('form_factor', 'unknown'),
            'has_locomotion': caps.get('locomotion') is not None,
            'has_manipulation': caps.get('manipulation') is not None,
            'sensor_types': list(
                k for k, v in caps.get('sensors', {}).items() if v
            ),
            'native_skill_count': len(caps.get('native_skills', [])),
        }

    def matches_task_requirements(self, task_reqs: Dict) -> float:
        """Score how well this robot matches a task's requirements.

        Args:
            task_reqs: Dict with optional keys:
                required_capabilities: list of strings
                    e.g. ['locomotion', 'gripper', 'gps']
                preferred_form_factor: str
                min_payload_kg: float

        Returns:
            0.0 (no match) to 1.0 (perfect match)
        """
        caps = self.get_capabilities()

        required = task_reqs.get('required_capabilities', [])
        if not required:
            return 0.5  # No requirements = neutral match

        matched = 0
        total = len(required)

        for req in required:
            if self._has_capability(caps, req):
                matched += 1

        if total == 0:
            return 0.5

        score = matched / total

        # Bonus for matching form factor
        preferred = task_reqs.get('preferred_form_factor')
        if preferred and caps.get('form_factor') == preferred:
            score = min(1.0, score + 0.1)

        # Penalty for insufficient payload
        min_payload = task_reqs.get('min_payload_kg')
        if min_payload and caps.get('payload_kg') is not None:
            if caps['payload_kg'] < min_payload:
                score *= 0.5

        return round(score, 2)

    # ── Private helpers ──────────────────────────────────────────

    def _has_capability(self, caps: Dict, req: str) -> bool:
        """Check if a specific capability requirement is met."""
        req_lower = req.lower()

        if req_lower == 'locomotion':
            return caps.get('locomotion') is not None
        if req_lower in ('manipulation', 'arm'):
            return caps.get('manipulation') is not None
        if req_lower == 'gripper':
            manip = caps.get('manipulation')
            return manip is not None and manip.get('grippers', 0) > 0
        # Sensor types
        if req_lower in ('imu', 'gps', 'lidar', 'camera', 'depth',
                         'encoder', 'force_torque', 'proximity', 'battery'):
            return caps.get('sensors', {}).get(req_lower, False)
        # Native skills
        if req_lower in caps.get('native_skills', []):
            return True
        return False

    def _detect_from_hardware_profile(self, caps: Dict):
        """Pull sensor detection from system_requirements HardwareProfile."""
        try:
            from security.system_requirements import detect_hardware
            hw = detect_hardware()
            caps['sensors']['imu'] = getattr(hw, 'has_imu', False)
            caps['sensors']['gps'] = getattr(hw, 'has_gps', False)
            caps['sensors']['lidar'] = getattr(hw, 'has_lidar', False)
            caps['sensors']['camera'] = getattr(hw, 'has_camera', False)
        except Exception as e:
            logger.debug(f"Hardware profile detection skipped: {e}")

    def _detect_from_sensor_store(self, caps: Dict):
        """Check SensorStore for live sensors."""
        try:
            from integrations.robotics.sensor_store import get_sensor_store
            store = get_sensor_store()
            for sensor_id, info in store.active_sensors().items():
                sensor_type = info.get('sensor_type', '')
                if sensor_type:
                    caps['sensors'][sensor_type] = True
        except Exception as e:
            logger.debug(f"SensorStore detection skipped: {e}")

    def _detect_from_config_file(self, caps: Dict):
        """Load static robot configuration from robot_config.json."""
        config_path = os.environ.get(
            'HEVOLVE_ROBOT_CONFIG',
            os.path.join('agent_data', 'robot_config.json'),
        )
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        caps['form_factor'] = config.get('form_factor', caps['form_factor'])
        caps['payload_kg'] = config.get('payload_kg')

        if 'locomotion' in config:
            caps['locomotion'] = config['locomotion']
        if 'manipulation' in config:
            caps['manipulation'] = config['manipulation']
        if 'workspace' in config:
            caps['workspace'] = config['workspace']
        if 'battery' in config:
            caps['battery'] = config['battery']
        if 'actuators' in config:
            caps['actuators'] = config['actuators']

    def _detect_from_hevolveai(self, caps: Dict):
        """Query HevolveAI for native capabilities via WorldModelBridge."""
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            bridge = get_world_model_bridge()
            health = bridge.check_health()
            if health and health.get('status') == 'ok':
                skills = health.get('native_skills', [])
                if isinstance(skills, list):
                    caps['native_skills'] = skills
        except Exception as e:
            logger.debug(f"HevolveAI capability query skipped: {e}")


# ── Singleton ─────────────────────────────────────────────────

_advertiser: Optional[RobotCapabilityAdvertiser] = None


def get_capability_advertiser() -> RobotCapabilityAdvertiser:
    """Get or create the singleton RobotCapabilityAdvertiser."""
    global _advertiser
    if _advertiser is None:
        _advertiser = RobotCapabilityAdvertiser()
    return _advertiser
