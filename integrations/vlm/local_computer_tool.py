"""
local_computer_tool.py — Synchronous pyautogui/HTTP wrapper for VLM actions.

Replaces OmniParser's Crossbar RPC-based ComputerTool with direct local execution.
Supports same action types as OmniParser computer.py (key, type, left_click, etc.).

Tier 'inprocess': direct pyautogui calls (no network)
Tier 'http': HTTP to localhost:5001 (omnitool-gui Flask server)
"""

import os
import io
import time
import base64
import logging

logger = logging.getLogger('hevolve.vlm.computer_tool')

# Module-level imports for mockability (pyautogui is optional)
try:
    import pyautogui
except ImportError:
    pyautogui = None

try:
    import pyperclip
except ImportError:
    pyperclip = None

import requests

# Action types matching OmniParser computer.py Action literal
SUPPORTED_ACTIONS = {
    'key', 'type', 'mouse_move', 'left_click', 'left_click_drag',
    'right_click', 'middle_click', 'double_click', 'screenshot',
    'cursor_position', 'hover', 'list_folders_and_files',
    'Open_file_and_copy_paste', 'open_file_gui', 'write_file',
    'read_file_and_understand', 'wait', 'hotkey',
}


def take_screenshot(tier: str) -> str:
    """
    Capture screen and return base64 PNG.

    Args:
        tier: 'inprocess' (pyautogui direct) or 'http' (localhost:5001)
    Returns:
        Base64-encoded PNG screenshot string.
    """
    if tier == 'inprocess':
        if pyautogui is None:
            raise ImportError("pyautogui is required for in-process screenshots")
        img = pyautogui.screenshot()
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode('ascii')
    else:
        resp = requests.get('http://localhost:5001/screenshot', timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get('base64_image', data.get('image', ''))


def execute_action(action: dict, tier: str) -> dict:
    """
    Execute a single VLM action (click, type, key, etc.).

    Args:
        action: dict with 'action', optionally 'coordinate', 'text', 'value', 'path'
        tier: 'inprocess' or 'http'
    Returns:
        dict with 'output' and optionally 'error'
    """
    if tier == 'inprocess':
        return _execute_inprocess(action)
    else:
        return _execute_http(action)


def _execute_inprocess(action: dict) -> dict:
    """Execute action via direct pyautogui calls."""
    act = action.get('action', '')
    coord = action.get('coordinate')
    text = action.get('text', action.get('value', ''))

    # File/wait actions don't need pyautogui
    _NO_GUI_ACTIONS = {
        'list_folders_and_files', 'read_file_and_understand', 'write_file',
        'Open_file_and_copy_paste', 'open_file_gui', 'wait',
    }

    if act not in _NO_GUI_ACTIONS and pyautogui is None:
        return {'output': '', 'error': 'pyautogui not installed'}

    try:
        if act == 'left_click':
            if coord:
                pyautogui.click(coord[0], coord[1])
            return {'output': f'Clicked at {coord}'}

        elif act == 'right_click':
            if coord:
                pyautogui.rightClick(coord[0], coord[1])
            return {'output': f'Right-clicked at {coord}'}

        elif act == 'double_click':
            if coord:
                pyautogui.doubleClick(coord[0], coord[1])
            return {'output': f'Double-clicked at {coord}'}

        elif act == 'middle_click':
            if coord:
                pyautogui.middleClick(coord[0], coord[1])
            return {'output': f'Middle-clicked at {coord}'}

        elif act == 'hover' or act == 'mouse_move':
            if coord:
                pyautogui.moveTo(coord[0], coord[1])
            return {'output': f'Moved to {coord}'}

        elif act == 'type':
            if text:
                # Use clipboard for reliability (same as OmniParser)
                if pyperclip is not None:
                    pyperclip.copy(text)
                    pyautogui.hotkey('ctrl', 'v')
                else:
                    pyautogui.typewrite(text, interval=0.012)
            return {'output': f'Typed: {text[:50]}...'}

        elif act == 'key':
            if text:
                pyautogui.press(text)
            return {'output': f'Pressed key: {text}'}

        elif act == 'hotkey':
            if text:
                keys = [k.strip() for k in text.split('+')]
                pyautogui.hotkey(*keys)
            return {'output': f'Hotkey: {text}'}

        elif act == 'left_click_drag':
            start = action.get('startCoordinate', coord)
            end = action.get('endCoordinate', action.get('coordinate_end'))
            if start and end:
                pyautogui.moveTo(start[0], start[1])
                pyautogui.drag(end[0] - start[0], end[1] - start[1], duration=0.5)
            return {'output': f'Dragged from {start} to {end}'}

        elif act == 'screenshot':
            return {'output': 'Screenshot taken', 'base64_image': take_screenshot('inprocess')}

        elif act == 'wait':
            wait_time = action.get('duration', 2)
            time.sleep(wait_time)
            return {'output': f'Waited {wait_time}s'}

        elif act == 'cursor_position':
            pos = pyautogui.position()
            return {'output': f'Cursor at ({pos.x}, {pos.y})'}

        elif act == 'list_folders_and_files':
            path = action.get('path', '.')
            try:
                entries = os.listdir(path)
                return {'output': '\n'.join(entries[:100])}
            except OSError as e:
                return {'output': '', 'error': str(e)}

        elif act == 'read_file_and_understand':
            path = action.get('path', '')
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read(10000)
                return {'output': content}
            except OSError as e:
                return {'output': '', 'error': str(e)}

        elif act == 'write_file':
            path = action.get('path', '')
            content = action.get('content', text)
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return {'output': f'Written to {path}'}
            except OSError as e:
                return {'output': '', 'error': str(e)}

        elif act == 'open_file_gui':
            path = action.get('path', '')
            os.startfile(path)
            return {'output': f'Opened {path}'}

        elif act == 'Open_file_and_copy_paste':
            src = action.get('source_path', '')
            dst = action.get('destination_path', '')
            try:
                with open(src, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                with open(dst, 'w', encoding='utf-8') as f:
                    f.write(content)
                return {'output': f'Copied {src} → {dst}'}
            except OSError as e:
                return {'output': '', 'error': str(e)}

        else:
            return {'output': '', 'error': f'Unknown action: {act}'}

    except Exception as e:
        logger.error(f"Action execution error ({act}): {e}")
        return {'output': '', 'error': str(e)}


def _execute_http(action: dict) -> dict:
    """Execute action via HTTP POST to localhost:5001/execute."""
    try:
        resp = requests.post(
            'http://localhost:5001/execute',
            json=action,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"HTTP action execution error: {e}")
        return {'output': '', 'error': str(e)}
