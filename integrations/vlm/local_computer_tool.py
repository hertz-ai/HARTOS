"""
local_computer_tool.py - Synchronous pyautogui/HTTP wrapper for VLM actions.

Replaces OmniParser's Crossbar RPC-based ComputerTool with direct local execution.
Supports same action types as OmniParser computer.py (key, type, left_click, etc.).

Tier 'inprocess': direct pyautogui calls (no network)
Tier 'http': HTTP to localhost:5001 (omnitool-gui Flask server)
"""

import os
import io
import sys
import time
import base64
import logging

# VLM screenshot long-edge — aspect ratio is PRESERVED during resize.
# Old behavior (1024×576 forced) squished 16:10 screens into 16:9 and the
# VLM's vertical coordinates drifted accordingly. Qwen3-VL handles 1280px
# long edge comfortably; longer is better grounding, shorter is faster.
# HEVOLVE_VLM_IMG_LONG_EDGE lets callers tune this.
VLM_IMG_LONG_EDGE = int(os.environ.get('HEVOLVE_VLM_IMG_LONG_EDGE', '1280'))
# Legacy constants kept for backward compat with existing call sites
# and for tests that reference them. The *real* dimensions are computed
# per-screenshot from the actual screen aspect ratio.
VLM_IMG_W = VLM_IMG_LONG_EDGE
VLM_IMG_H = int(VLM_IMG_LONG_EDGE * 9 / 16)

logger = logging.getLogger('hevolve.vlm.computer_tool')

# Module-level imports for mockability (pyautogui is optional)
try:
    import pyautogui
except ImportError:
    pyautogui = None


def _ensure_dpi_aware():
    """Make this process DPI-aware on Windows so screenshot pixels and
    pyautogui click coordinates live in the same physical space.

    Without this, on a 150%-scaled 2560×1440 display, pyautogui.size() returns
    (1707, 960) while pyautogui.screenshot() captures the full 2560×1440 physical
    pixels — so VLM-derived coordinates land in the wrong spot and frequently
    miss Start menu / taskbar targets. Idempotent; safe to call repeatedly."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win 8.1+). Falls back to
        # SetProcessDPIAware on older versions. Both are no-ops if already set.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        logger.debug(f"DPI awareness setup skipped: {e}")


# Call at import time so every screenshot/click path is DPI-consistent
_ensure_dpi_aware()

try:
    import pyperclip
except ImportError:
    pyperclip = None

from core.http_pool import pooled_get, pooled_post

# Action types matching OmniParser computer.py Action literal.
# 'shell' is a Nunba extension — lets the VLM loop run deterministic commands
# instead of GUI grounding for tasks that can be done programmatically
# (e.g., launching an app, opening a file in its default handler).
SUPPORTED_ACTIONS = {
    'key', 'type', 'mouse_move', 'left_click', 'left_click_drag',
    'right_click', 'middle_click', 'double_click', 'screenshot',
    'cursor_position', 'hover', 'list_folders_and_files',
    'Open_file_and_copy_paste', 'open_file_gui', 'write_file',
    'read_file_and_understand', 'wait', 'hotkey', 'shell',
}


def take_screenshot(tier: str) -> str:
    """
    Capture screen and return base64 JPEG.

    The image is resized to a long-edge of VLM_IMG_LONG_EDGE while
    PRESERVING aspect ratio, so the VLM's normalized coordinates map
    back to the physical screen without distortion. Screen DPI awareness
    is enabled at import (see _ensure_dpi_aware()).

    Args:
        tier: 'inprocess' (pyautogui direct) or 'http' (localhost:5001)
    Returns:
        Base64-encoded JPEG screenshot string.
    """
    if tier == 'inprocess':
        if pyautogui is None:
            raise ImportError("pyautogui is required for in-process screenshots")
        img = pyautogui.screenshot()
        from PIL import Image

        w, h = img.size
        long_edge = max(w, h)
        if long_edge > VLM_IMG_LONG_EDGE:
            scale = VLM_IMG_LONG_EDGE / long_edge
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=70)
        return base64.b64encode(buf.getvalue()).decode('ascii')
    else:
        resp = pooled_get('http://localhost:5001/screenshot', timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get('base64_image', data.get('image', ''))


def get_active_window_info():
    """Get the actual foreground window title + process name from the OS.
    Used to prevent VLM misidentifying windows (e.g. Claude Code as MobaXterm)."""
    try:
        import platform, subprocess, json
        _os = platform.system()
        if _os == 'Windows':
            r = subprocess.run(
                ['powershell', '-Command',
                 '(Get-Process | Where-Object {$_.MainWindowHandle -eq '
                 '(Add-Type -MemberDefinition \'[DllImport("user32.dll")] '
                 'public static extern IntPtr GetForegroundWindow();\' '
                 '-Name W -PassThru)::GetForegroundWindow()}).ProcessName + '
                 '": " + (Get-Process | Where-Object {$_.MainWindowHandle -eq '
                 '(Add-Type -MemberDefinition \'[DllImport("user32.dll")] '
                 'public static extern IntPtr GetForegroundWindow();\' '
                 '-Name W2 -PassThru)::GetForegroundWindow()}).MainWindowTitle'],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        elif _os == 'Linux':
            r = subprocess.run(['xdotool', 'getactivewindow', 'getwindowname'],
                             capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                return r.stdout.strip()
        elif _os == 'Darwin':
            r = subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                return r.stdout.strip()
    except Exception:
        pass
    return None


def execute_action(action: dict, tier: str) -> dict:
    """
    Execute a single VLM action (click, type, key, etc.).

    Includes active window validation — if the VLM's reasoning mentions
    a window name that doesn't match the actual foreground window,
    the action is flagged (prevents clicking the wrong app's taskbar icon).

    Args:
        action: dict with 'action', optionally 'coordinate', 'text', 'value', 'path', 'reasoning'
        tier: 'inprocess' or 'http'
    Returns:
        dict with 'output' and optionally 'error', 'window_mismatch'
    """
    # Validate: if reasoning mentions a specific app, check it matches reality
    reasoning = action.get('Reasoning', action.get('reasoning', '')).lower()
    _mismatch = None
    if any(app in reasoning for app in ['minimize', 'close', 'switch to', 'click on']):
        active = get_active_window_info()
        if active:
            # Check for common misidentifications
            if 'mobaxt' in reasoning and 'mobaxt' not in active.lower():
                _mismatch = f"VLM thinks MobaXterm but active window is: {active}"
            elif 'notepad' in reasoning and 'notepad' not in active.lower():
                _mismatch = f"VLM thinks Notepad but active window is: {active}"

    if tier == 'inprocess':
        result = _execute_inprocess(action)
    else:
        result = _execute_http(action)

    if _mismatch:
        result['window_mismatch'] = _mismatch
        import logging
        logging.getLogger('hevolve.vlm').warning(f"[WINDOW-MISMATCH] {_mismatch}")

    return result


def _execute_inprocess(action: dict) -> dict:
    """Execute action via direct pyautogui calls."""
    act = action.get('action', '')
    coord = action.get('coordinate')
    text = action.get('text', action.get('value', ''))

    # Validate coordinate format (VLM output can be malformed)
    if coord is not None:
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            return {'output': '', 'error': f'Invalid coordinate format: {coord}'}

    # File/wait/shell actions don't need pyautogui
    _NO_GUI_ACTIONS = {
        'list_folders_and_files', 'read_file_and_understand', 'write_file',
        'Open_file_and_copy_paste', 'open_file_gui', 'wait', 'shell',
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
                if isinstance(text, list):
                    keys = [str(k).strip() for k in text]
                else:
                    keys = [k.strip() for k in str(text).split('+')]
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
            # Open a file / app in the OS default handler. On Windows this is
            # os.startfile (uses ShellExecute). On Linux/Mac the equivalent is
            # `xdg-open` / `open`, which aren't available as a Python API —
            # route through the shell handler so the same denylist applies.
            path = action.get('path', '') or text
            if not path:
                return {'output': '', 'error': 'open_file_gui needs a path'}
            if sys.platform == 'win32':
                try:
                    os.startfile(path)  # type: ignore[attr-defined]
                    return {'output': f'Opened {path}'}
                except OSError as e:
                    return {'output': '', 'error': f'open_file_gui failed: {e}'}
            # Non-Windows: delegate to shell so we reuse the denylist
            shell_cmd = (
                f'open {path}' if sys.platform == 'darwin' else f'xdg-open {path}'
            )
            try:
                from hart_intelligence_entry import _handle_shell_command_tool
            except ImportError as e:
                return {
                    'output': '',
                    'error': f'open_file_gui unavailable: {e}',
                    'status': 'error',
                }
            result_text = _handle_shell_command_tool(shell_cmd)
            ok = isinstance(result_text, str) and result_text.startswith('Exit code: 0')
            return {
                'output': result_text,
                'status': 'ok' if ok else 'error',
            }

        elif act == 'shell':
            # Deterministic command execution inside the VLM loop. The ONLY
            # implementation lives in hart_intelligence_entry._handle_shell_command_tool
            # so the denylist + timeout + truncation + shell-selector parsing all
            # apply identically to Shell_Command and this VLM-emitted action. If
            # that import fails (stripped frozen build / circular import), we
            # fail CLOSED rather than falling back to a bare subprocess.run —
            # a bare fallback would skip the denylist and expose a command
            # injection channel that silently weakens safety posture.
            cmd = action.get('command', text)
            if not cmd:
                return {'output': '', 'error': 'shell action needs command string'}
            try:
                from hart_intelligence_entry import _handle_shell_command_tool
            except ImportError as e:
                return {
                    'output': '',
                    'error': (
                        f"shell action unavailable: {e}. Refusing to run "
                        "without the shared denylist."
                    ),
                    'status': 'error',
                }
            result_text = _handle_shell_command_tool(cmd)
            # _handle_shell_command_tool returns 'Exit code: N\n<body>' on
            # success and 'Shell_Command refused: ...' / 'Shell_Command error: ...'
            # on refusal or failure. Classify anything other than a clean
            # 'Exit code: 0' prefix as a non-success so the VLM loop's
            # consecutive-action-error counter can back off.
            ok = isinstance(result_text, str) and result_text.startswith('Exit code: 0')
            return {
                'output': result_text,
                'status': 'ok' if ok else 'error',
            }

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
        resp = pooled_post(
            'http://localhost:5001/execute',
            json=action,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"HTTP action execution error: {e}")
        return {'output': '', 'error': str(e)}
