"""
Action Data Model — Universal representation for robot actions.

Actions are the world model's predictions tested against reality.
They flow from LLM-langchain's agentic layer through WorldModelBridge
to HevolveAI's native embodiment where the actual execution happens.

This is a data model only — no intelligence.  The actual motor control,
kinematics, PID loops live in HevolveAI (raw native intelligence).

Action types:
    motor_velocity, servo_position, gpio_output, gripper,
    navigate_to, speak, emergency_stop
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class RobotAction:
    """Universal action format.

    Flows: Agent goal → dispatch → RobotAction → WorldModelBridge → HevolveAI
    """
    action_type: str            # motor_velocity, servo_position, gpio_output, etc.
    target: str                 # Actuator identifier (e.g., 'left_wheel', 'gripper_0')
    params: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    priority: int = 0           # Higher = more urgent (safety actions get 999)
    timeout_ms: float = 0       # 0 = no timeout
    source: str = 'agent'       # 'agent', 'recipe', 'safety', 'fleet_command'

    def to_dict(self) -> Dict:
        """Serialize for transport to HevolveAI via WorldModelBridge."""
        return {
            'type': self.action_type,
            'target': self.target,
            'params': self.params,
            'timestamp': self.timestamp,
            'priority': self.priority,
            'timeout_ms': self.timeout_ms,
            'source': self.source,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> 'RobotAction':
        """Deserialize from dict."""
        return cls(
            action_type=d.get('type', d.get('action_type', '')),
            target=d.get('target', ''),
            params=d.get('params', {}),
            timestamp=d.get('timestamp', time.time()),
            priority=d.get('priority', 0),
            timeout_ms=d.get('timeout_ms', 0),
            source=d.get('source', 'agent'),
        )

    @classmethod
    def emergency_stop_action(cls) -> 'RobotAction':
        """Create an emergency stop action (highest priority)."""
        return cls(
            action_type='emergency_stop',
            target='*',
            params={'velocity': 0, 'force': 0},
            priority=999,
            source='safety',
        )
