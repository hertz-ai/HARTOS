"""
Action Data Model — Universal representation for robot actions.

Actions are the world model's predictions tested against reality.
They flow from LLM-langchain's agentic layer through WorldModelBridge
to HevolveAI's native embodiment where the actual execution happens.

This is a data model only — no intelligence.  The actual motor control,
kinematics, PID loops live in HevolveAI (raw native intelligence).

Action types:
  Low-level actuator (executed directly by HevolveAI's native control):
    motor_velocity, servo_position, gpio_output, gripper,
    navigate_to, speak, emergency_stop
  Embodied / VLA (a Vision-Language-Action model — e.g. Qwen RobotSuite —
  runs INSIDE HevolveAI; HARTOS requests at this high level, HevolveAI's
  policy expands them into the low-level actuator stream above):
    vla_instruct        — language instruction + observation → policy step
                          (the embodied analog of an LLM prompt → reply)
    action_chunk        — execute a chunk of policy-emitted low-level actions
                          at a fixed control rate (VLA action chunking)
    end_effector_delta  — Cartesian end-effector pose delta + gripper
    world_model_rollout — forward-predict future states for an action
                          sequence (planning + early error detection)

`action_type` is a free string by design — the vocabulary is the HARTOS↔
HevolveAI contract; HevolveAI's RobotSuite adapter interprets it.  Add a verb
here (+ a factory below) so callers construct it ONE way, never inline dicts.
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

    # ── Embodied / VLA factories (Qwen-RobotSuite-class policy) ──────────
    # HARTOS issues these high-level verbs; HevolveAI's embodied policy expands
    # them into the low-level actuator stream and tests the prediction against
    # reality.  A failed dispatch propagates through WorldModelBridge
    # ._propagate_embodied_error → the hevolveai hive (ExceptionCollector +
    # gossip), so a stuck/erroring policy is visible to self-healing and peers.

    @classmethod
    def vla_instruct(cls, instruction: str, observation: Optional[Dict] = None,
                     horizon: int = 8, target: str = '*',
                     source: str = 'agent') -> 'RobotAction':
        """Language instruction + current observation → one VLA policy step.

        The embodied analog of an LLM prompt→reply: ``instruction`` is the goal
        in natural language, ``observation`` is the current sensor snapshot (or a
        ref the bridge already ingested), ``horizon`` is how many low-level steps
        the policy may emit before the next observation."""
        return cls(
            action_type='vla_instruct', target=target,
            params={'instruction': instruction,
                    'observation': observation or {}, 'horizon': int(horizon)},
            source=source,
        )

    @classmethod
    def action_chunk(cls, chunk: list, control_hz: float = 10.0,
                     target: str = '*', source: str = 'agent') -> 'RobotAction':
        """Execute a chunk of policy-emitted low-level actions at ``control_hz``.

        ``chunk`` is a list of low-level action dicts (e.g. per-step joint/EE
        commands) the VLA returned for the current observation — sent as one unit
        so the control loop stays smooth (VLA action chunking)."""
        return cls(
            action_type='action_chunk', target=target,
            params={'chunk': list(chunk), 'control_hz': float(control_hz)},
            source=source,
        )

    @classmethod
    def end_effector_delta(cls, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0,
                           droll: float = 0.0, dpitch: float = 0.0, dyaw: float = 0.0,
                           gripper: Optional[float] = None, target: str = 'arm',
                           source: str = 'agent') -> 'RobotAction':
        """Cartesian end-effector pose delta (+ optional gripper 0..1)."""
        params: Dict[str, Any] = {
            'dx': dx, 'dy': dy, 'dz': dz,
            'droll': droll, 'dpitch': dpitch, 'dyaw': dyaw,
        }
        if gripper is not None:
            params['gripper'] = float(gripper)
        return cls(action_type='end_effector_delta', target=target,
                   params=params, source=source)

    @classmethod
    def world_model_rollout(cls, action_sequence: list, horizon: int = 8,
                            target: str = '*', source: str = 'agent') -> 'RobotAction':
        """Forward-predict future states for ``action_sequence`` (no execution).

        Asks HevolveAI's world model to imagine the outcome of a candidate action
        sequence — used for planning and for early error detection (compare the
        predicted vs the observed trajectory; large divergence = a fault to
        propagate through the hive)."""
        return cls(
            action_type='world_model_rollout', target=target,
            params={'action_sequence': list(action_sequence),
                    'horizon': int(horizon)},
            source=source,
        )
