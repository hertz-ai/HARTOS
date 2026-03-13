"""
Robot Recipe Adapter — Physical action ↔ recipe step conversion.

Bridges the CREATE/REUSE recipe system with physical robot actions:
  - CREATE mode: records motor sequences with sensor context → recipe steps
  - REUSE mode: replays recipe steps → RobotAction commands via WorldModelBridge

HevolveAI handles real-time adaptation during replay (pause on obstacle,
adjust trajectory).  This adapter just converts the data format.

NO intelligence here.  Just format conversion between:
  Recipe step (JSON in prompts/{id}_recipe.json)
  ↔ RobotAction (integrations/robotics/action_model.py)
  + sensor context from SensorStore
"""
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve_robotics')


class RobotRecipeAdapter:
    """Converts between physical actions and recipe steps.

    Recipe steps for physical actions include:
      - The action command (RobotAction.to_dict())
      - Sensor context at action time (what the robot "saw/felt")
      - Outcome (did it work? how far off was it?)

    This lets REUSE mode replay physical sequences, with HevolveAI
    providing real-time adaptation via its native intelligence.
    """

    @staticmethod
    def action_to_recipe_step(
        action: Dict,
        sensor_context: Optional[Dict] = None,
        outcome: Optional[Dict] = None,
    ) -> Dict:
        """Convert a physical action + context into a recipe step.

        Args:
            action: RobotAction.to_dict() or raw action dict
            sensor_context: Sensor readings at action time
            outcome: Result of the action (success, error, distance_error, etc.)

        Returns:
            Recipe step dict ready for storage in recipe JSON.
        """
        step = {
            'step_type': 'robot_action',
            'action': action,
            'sensor_context': sensor_context or {},
            'outcome': outcome or {},
            'timestamp': time.time(),
            'step_id': str(uuid.uuid4())[:8],
        }
        return step

    @staticmethod
    def recipe_step_to_action(step: Dict) -> Optional[Dict]:
        """Convert a recipe step back into an action dict for replay.

        Args:
            step: Recipe step dict (from action_to_recipe_step)

        Returns:
            Action dict suitable for WorldModelBridge.send_action(),
            or None if step is not a robot_action.
        """
        if step.get('step_type') != 'robot_action':
            return None

        action = step.get('action')
        if not action or not isinstance(action, dict):
            return None

        # Ensure required fields
        if 'type' not in action:
            return None

        return action

    @staticmethod
    def record_motion_sequence(
        actions: List[Dict],
        sensor_log: Optional[List[Dict]] = None,
    ) -> Dict:
        """Record a full motion sequence as a recipe.

        Args:
            actions: List of (action_dict, sensor_context, outcome) tuples
                     or plain action dicts
            sensor_log: Optional separate sensor log (parallel to actions)

        Returns:
            Recipe dict with steps and metadata.
        """
        steps = []
        for i, entry in enumerate(actions):
            if isinstance(entry, dict) and 'action' in entry:
                # Already has action/sensor_context/outcome structure
                step = RobotRecipeAdapter.action_to_recipe_step(
                    action=entry['action'],
                    sensor_context=entry.get('sensor_context'),
                    outcome=entry.get('outcome'),
                )
            elif isinstance(entry, dict):
                # Plain action dict
                sensor_ctx = {}
                if sensor_log and i < len(sensor_log):
                    sensor_ctx = sensor_log[i]
                step = RobotRecipeAdapter.action_to_recipe_step(
                    action=entry,
                    sensor_context=sensor_ctx,
                )
            else:
                continue
            steps.append(step)

        recipe_id = f"robot_sequence_{uuid.uuid4().hex[:8]}"
        return {
            'recipe_id': recipe_id,
            'recipe_type': 'robot_motion_sequence',
            'steps': steps,
            'step_count': len(steps),
            'created_at': time.time(),
        }

    @staticmethod
    def replay_motion_recipe(recipe: Dict) -> List[Dict]:
        """Extract action sequence from a recipe for replay.

        The caller (or WorldModelBridge) sends each action in order.
        HevolveAI handles real-time adaptation during execution.

        Args:
            recipe: Recipe dict from record_motion_sequence()

        Returns:
            List of action dicts for send_action()
        """
        actions = []
        for step in recipe.get('steps', []):
            action = RobotRecipeAdapter.recipe_step_to_action(step)
            if action is not None:
                actions.append(action)
        return actions
