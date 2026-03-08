"""
Recipe Hooks — Capture remote desktop sessions as recipes and replay them.

Extends the Recipe Pattern:
  - Capture: Record a remote desktop action sequence as recipe steps
  - Replay: Execute a saved recipe on a remote device (CREATE once → REUSE forever)

Integration points:
  - create_recipe.py (CREATE mode) — hook into action execution to record remote actions
  - reuse_recipe.py (REUSE mode) — replay recorded remote actions on connected device
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


class RemoteDesktopRecipeBridge:
    """Bridge between remote desktop sessions and the HARTOS recipe system."""

    def __init__(self):
        self._recording = False
        self._recorded_actions: List[Dict[str, Any]] = []
        self._session_id: Optional[str] = None

    def start_recording(self, session_id: str) -> None:
        """Start recording remote desktop actions as recipe steps."""
        self._recording = True
        self._session_id = session_id
        self._recorded_actions = []
        logger.info(f"Recording remote desktop actions for session {session_id[:8]}")

    def stop_recording(self) -> List[Dict[str, Any]]:
        """Stop recording and return captured actions."""
        self._recording = False
        actions = self._recorded_actions.copy()
        logger.info(f"Stopped recording: {len(actions)} actions captured")
        return actions

    def record_action(self, action: Dict[str, Any]) -> None:
        """Record a single remote desktop action (called by input_handler)."""
        if not self._recording:
            return
        step = {
            'type': 'remote_desktop_action',
            'action': action,
            'timestamp': time.time(),
            'session_id': self._session_id,
        }
        self._recorded_actions.append(step)

    def capture_session_as_recipe(self, session_id: str,
                                   actions: List[Dict[str, Any]]) -> dict:
        """Convert a sequence of remote desktop actions into a recipe.

        Args:
            session_id: The remote desktop session that generated these actions
            actions: List of action dicts (from input_handler event format)

        Returns:
            Recipe dict compatible with HARTOS recipe format.
        """
        steps = []
        for i, action in enumerate(actions):
            step = {
                'step_id': i + 1,
                'action_type': action.get('type', 'unknown'),
                'parameters': {k: v for k, v in action.items() if k != 'type'},
                'description': _describe_action(action),
            }
            steps.append(step)

        recipe = {
            'recipe_type': 'remote_desktop',
            'session_id': session_id,
            'steps': steps,
            'step_count': len(steps),
            'created_at': time.time(),
        }
        logger.info(f"Captured {len(steps)} steps as recipe from session {session_id[:8]}")
        return recipe

    def replay_recipe_on_device(self, recipe: dict,
                                 device_id: Optional[str] = None,
                                 password: Optional[str] = None,
                                 delay: float = 0.5) -> dict:
        """Replay a saved recipe — execute steps on this or a remote device.

        Args:
            recipe: Recipe dict from capture_session_as_recipe()
            device_id: Remote device to connect to (None = local)
            password: Password for remote connection
            delay: Delay between steps in seconds

        Returns:
            {'success': bool, 'steps_executed': int, 'errors': list}
        """
        steps = recipe.get('steps', [])
        if not steps:
            return {'success': True, 'steps_executed': 0, 'errors': []}

        errors = []
        executed = 0

        try:
            from integrations.remote_desktop.input_handler import InputHandler
            handler = InputHandler()
        except Exception as e:
            return {'success': False, 'steps_executed': 0,
                    'errors': [f'InputHandler unavailable: {e}']}

        # Connect to remote if device_id provided
        if device_id:
            try:
                from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
                bridge = get_rustdesk_bridge()
                if bridge.available:
                    ok, msg = bridge.connect(device_id, password=password)
                    if not ok:
                        return {'success': False, 'steps_executed': 0,
                                'errors': [f'Connection failed: {msg}']}
                    time.sleep(2)  # Wait for connection
            except Exception as e:
                errors.append(f'Connection warning: {e}')

        for step in steps:
            try:
                action = {
                    'type': step.get('action_type', 'unknown'),
                    **step.get('parameters', {}),
                }
                result = handler.handle_input_event(action)
                executed += 1
                if delay > 0:
                    time.sleep(delay)
            except Exception as e:
                errors.append(f"Step {step.get('step_id', '?')}: {e}")

        return {
            'success': len(errors) == 0,
            'steps_executed': executed,
            'total_steps': len(steps),
            'errors': errors,
        }


def _describe_action(action: dict) -> str:
    """Human-readable description of a remote desktop action."""
    action_type = action.get('type', 'unknown')
    if action_type == 'click':
        return f"Click at ({action.get('x')}, {action.get('y')})"
    elif action_type in ('rightclick', 'doubleclick', 'middleclick'):
        return f"{action_type.title()} at ({action.get('x')}, {action.get('y')})"
    elif action_type == 'type':
        text = action.get('text', '')
        preview = text[:30] + '...' if len(text) > 30 else text
        return f"Type: {preview!r}"
    elif action_type == 'key':
        return f"Key press: {action.get('key')}"
    elif action_type == 'hotkey':
        return f"Hotkey: {action.get('key')}"
    elif action_type == 'scroll':
        return f"Scroll ({action.get('delta_x', 0)}, {action.get('delta_y', 0)})"
    elif action_type == 'move':
        return f"Move cursor to ({action.get('x')}, {action.get('y')})"
    elif action_type == 'drag':
        return f"Drag from ({action.get('x')}, {action.get('y')}) to ({action.get('end_x')}, {action.get('end_y')})"
    return f"Action: {action_type}"


# ── Singleton ──────────────────────────────────────────────────

_bridge: Optional[RemoteDesktopRecipeBridge] = None


def get_recipe_bridge() -> RemoteDesktopRecipeBridge:
    global _bridge
    if _bridge is None:
        _bridge = RemoteDesktopRecipeBridge()
    return _bridge
