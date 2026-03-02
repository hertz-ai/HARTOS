"""
Input Handler — Cross-platform mouse/keyboard/scroll input relay.

Backend 1: pynput (low-level, modifier tracking) — optional
Backend 2: pyautogui (fallback, reuses local_computer_tool.py action format)

Security: runs classify_remote_input() on destructive hotkeys (Alt+F4 etc.)
"""

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve.remote_desktop')

# ── Optional dependencies ───────────────────────────────────────

_pynput_mouse = None
_pynput_keyboard = None
_pyautogui = None

try:
    from pynput.mouse import Controller as MouseController, Button
    from pynput.keyboard import Controller as KeyboardController, Key
    _pynput_mouse = MouseController
    _pynput_keyboard = KeyboardController
except ImportError:
    pass

try:
    import pyautogui as _pyautogui_module
    _pyautogui = _pyautogui_module
    _pyautogui.FAILSAFE = False  # Disable corner failsafe for remote control
except ImportError:
    pass

# ── Key Mapping ─────────────────────────────────────────────────

# Map common key names to pynput Key enums
_PYNPUT_SPECIAL_KEYS = {}
if _pynput_keyboard:
    _PYNPUT_SPECIAL_KEYS = {
        'enter': Key.enter, 'return': Key.enter,
        'tab': Key.tab, 'space': Key.space,
        'backspace': Key.backspace, 'delete': Key.delete,
        'escape': Key.esc, 'esc': Key.esc,
        'up': Key.up, 'down': Key.down, 'left': Key.left, 'right': Key.right,
        'home': Key.home, 'end': Key.end,
        'pageup': Key.page_up, 'pagedown': Key.page_down,
        'insert': Key.insert,
        'f1': Key.f1, 'f2': Key.f2, 'f3': Key.f3, 'f4': Key.f4,
        'f5': Key.f5, 'f6': Key.f6, 'f7': Key.f7, 'f8': Key.f8,
        'f9': Key.f9, 'f10': Key.f10, 'f11': Key.f11, 'f12': Key.f12,
        'shift': Key.shift, 'ctrl': Key.ctrl, 'control': Key.ctrl,
        'alt': Key.alt, 'super': Key.cmd, 'win': Key.cmd, 'cmd': Key.cmd,
        'capslock': Key.caps_lock, 'numlock': Key.num_lock,
        'printscreen': Key.print_screen,
    }


class InputHandler:
    """Dispatches remote input events to local system.

    Event format: {type, x, y, button, key, text, hotkey, delta_x, delta_y}
    """

    def __init__(self, allow_control: bool = True):
        self._allow_control = allow_control
        self._lock = threading.Lock()
        self._mouse = None
        self._keyboard = None
        self._event_count = 0

        # Initialize backends
        if _pynput_mouse:
            try:
                self._mouse = _pynput_mouse()
            except Exception:
                pass
        if _pynput_keyboard:
            try:
                self._keyboard = _pynput_keyboard()
            except Exception:
                pass

    def set_control_mode(self, allow: bool) -> None:
        """Toggle view-only mode."""
        self._allow_control = allow
        logger.info(f"Control mode: {'enabled' if allow else 'view-only'}")

    @property
    def control_enabled(self) -> bool:
        return self._allow_control

    def handle_input_event(self, event: dict) -> dict:
        """Dispatch a remote input event.

        Args:
            event: {type, x, y, button, key, text, hotkey, delta_x, delta_y}

        Returns:
            {success: bool, error: str|None, classification: str}
        """
        if not self._allow_control:
            return {'success': False, 'error': 'view_only_mode',
                    'classification': 'blocked'}

        event_type = event.get('type', '')

        # Security classification
        from integrations.remote_desktop.security import classify_remote_input
        classification = classify_remote_input(event)
        if classification == 'destructive':
            logger.warning(f"Destructive input blocked: {event}")
            return {'success': False, 'error': 'destructive_action_blocked',
                    'classification': classification}

        self._event_count += 1

        try:
            handler = self._get_handler(event_type)
            if handler:
                handler(event)
                return {'success': True, 'error': None,
                        'classification': classification}
            else:
                return {'success': False, 'error': f'unknown_event_type: {event_type}',
                        'classification': classification}
        except Exception as e:
            logger.error(f"Input event failed: {e}")
            return {'success': False, 'error': str(e),
                    'classification': classification}

    def _get_handler(self, event_type: str):
        """Map event type to handler method."""
        handlers = {
            'click': self._handle_click,
            'rightclick': self._handle_rightclick,
            'doubleclick': self._handle_doubleclick,
            'middleclick': self._handle_middleclick,
            'move': self._handle_move,
            'mouse_move': self._handle_move,
            'drag': self._handle_drag,
            'scroll': self._handle_scroll,
            'key': self._handle_key,
            'type': self._handle_type,
            'hotkey': self._handle_hotkey,
            'mouse_down': self._handle_mouse_down,
            'mouse_up': self._handle_mouse_up,
        }
        return handlers.get(event_type)

    # ── Mouse Events ────────────────────────────────────────────

    def _handle_click(self, event: dict) -> None:
        x, y = event.get('x', 0), event.get('y', 0)
        if self._mouse:
            self._mouse.position = (x, y)
            self._mouse.click(Button.left)
        elif _pyautogui:
            _pyautogui.click(x, y)

    def _handle_rightclick(self, event: dict) -> None:
        x, y = event.get('x', 0), event.get('y', 0)
        if self._mouse:
            self._mouse.position = (x, y)
            self._mouse.click(Button.right)
        elif _pyautogui:
            _pyautogui.rightClick(x, y)

    def _handle_doubleclick(self, event: dict) -> None:
        x, y = event.get('x', 0), event.get('y', 0)
        if self._mouse:
            self._mouse.position = (x, y)
            self._mouse.click(Button.left, 2)
        elif _pyautogui:
            _pyautogui.doubleClick(x, y)

    def _handle_middleclick(self, event: dict) -> None:
        x, y = event.get('x', 0), event.get('y', 0)
        if self._mouse:
            self._mouse.position = (x, y)
            self._mouse.click(Button.middle)
        elif _pyautogui:
            _pyautogui.middleClick(x, y)

    def _handle_move(self, event: dict) -> None:
        x, y = event.get('x', 0), event.get('y', 0)
        if self._mouse:
            self._mouse.position = (x, y)
        elif _pyautogui:
            _pyautogui.moveTo(x, y, _pause=False)

    def _handle_drag(self, event: dict) -> None:
        start_x = event.get('start_x', event.get('x', 0))
        start_y = event.get('start_y', event.get('y', 0))
        end_x = event.get('end_x', event.get('x', 0))
        end_y = event.get('end_y', event.get('y', 0))
        if _pyautogui:
            _pyautogui.moveTo(start_x, start_y)
            _pyautogui.drag(end_x - start_x, end_y - start_y)
        elif self._mouse:
            self._mouse.position = (start_x, start_y)
            self._mouse.press(Button.left)
            self._mouse.position = (end_x, end_y)
            self._mouse.release(Button.left)

    def _handle_scroll(self, event: dict) -> None:
        delta_y = event.get('delta_y', 0)
        delta_x = event.get('delta_x', 0)
        x, y = event.get('x'), event.get('y')
        if self._mouse:
            if x is not None and y is not None:
                self._mouse.position = (x, y)
            self._mouse.scroll(delta_x, delta_y)
        elif _pyautogui:
            if x is not None and y is not None:
                _pyautogui.moveTo(x, y, _pause=False)
            _pyautogui.scroll(delta_y)

    def _handle_mouse_down(self, event: dict) -> None:
        x, y = event.get('x', 0), event.get('y', 0)
        button_name = event.get('button', 'left')
        if self._mouse:
            self._mouse.position = (x, y)
            button = {'left': Button.left, 'right': Button.right,
                      'middle': Button.middle}.get(button_name, Button.left)
            self._mouse.press(button)
        elif _pyautogui:
            _pyautogui.mouseDown(x, y, button=button_name)

    def _handle_mouse_up(self, event: dict) -> None:
        x, y = event.get('x', 0), event.get('y', 0)
        button_name = event.get('button', 'left')
        if self._mouse:
            self._mouse.position = (x, y)
            button = {'left': Button.left, 'right': Button.right,
                      'middle': Button.middle}.get(button_name, Button.left)
            self._mouse.release(button)
        elif _pyautogui:
            _pyautogui.mouseUp(x, y, button=button_name)

    # ── Keyboard Events ─────────────────────────────────────────

    def _handle_key(self, event: dict) -> None:
        """Single key press+release."""
        key = event.get('key', '')
        if not key:
            return
        if self._keyboard:
            pynput_key = _PYNPUT_SPECIAL_KEYS.get(key.lower())
            if pynput_key:
                self._keyboard.press(pynput_key)
                self._keyboard.release(pynput_key)
            else:
                self._keyboard.press(key)
                self._keyboard.release(key)
        elif _pyautogui:
            _pyautogui.press(key)

    def _handle_type(self, event: dict) -> None:
        """Type text string."""
        text = event.get('text', '')
        if not text:
            return
        if self._keyboard:
            self._keyboard.type(text)
        elif _pyautogui:
            _pyautogui.typewrite(text, interval=0.02)

    def _handle_hotkey(self, event: dict) -> None:
        """Execute hotkey combo (e.g., 'ctrl+c')."""
        hotkey = event.get('hotkey', '')
        if not hotkey:
            return
        keys = [k.strip() for k in hotkey.split('+')]
        if self._keyboard:
            pressed = []
            try:
                for k in keys:
                    pynput_key = _PYNPUT_SPECIAL_KEYS.get(k.lower(), k)
                    self._keyboard.press(pynput_key)
                    pressed.append(pynput_key)
            finally:
                for k in reversed(pressed):
                    try:
                        self._keyboard.release(k)
                    except Exception:
                        pass
        elif _pyautogui:
            _pyautogui.hotkey(*keys)

    # ── Stats ───────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            'control_enabled': self._allow_control,
            'event_count': self._event_count,
            'backends': {
                'pynput_mouse': self._mouse is not None,
                'pynput_keyboard': self._keyboard is not None,
                'pyautogui': _pyautogui is not None,
            },
        }
