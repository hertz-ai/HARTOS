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
from typing import Optional

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


# Single source of truth for SetProcessDpiAwareness — see
# core/dpi_awareness.py for the rationale (was duplicated in
# remote_desktop/window_capture.py until 2026-05-03 DRY pass).
from core.dpi_awareness import ensure_dpi_aware as _ensure_dpi_aware

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


#: Process-name keyword pairs the reasoning-mismatch detector watches.
#: ``(reasoning_substring, foreground_window_substring)`` — when the
#: VLM's reasoning includes the first but the actual foreground window
#: title doesn't include the second, the action gets flagged.  Order
#: matters: more specific patterns first.  Extend by appending tuples.
_REASONING_MISMATCH_PATTERNS = (
    ('mobaxt', 'mobaxt'),
    ('notepad', 'notepad'),
)

#: Verbs in the VLM's reasoning that hint a window-targeted action.
#: We only run the (slow) get_active_window_info probe when the
#: reasoning suggests the VLM is acting on a specific window, not
#: when it's typing or generic-clicking somewhere mid-screen.
_WINDOW_TARGETED_VERBS = ('minimize', 'close', 'switch to', 'click on')


def _check_reasoning_mismatch(action: dict) -> Optional[str]:
    """Detect when the VLM's stated reasoning contradicts the actual
    foreground window.  Returns a human-readable mismatch description
    or None when there's no detectable disagreement.

    Extracted from execute_action in the SRP cleanup pass — was 14
    lines tangled in the action-dispatch flow alongside per-window
    translation, safety, audit, and verify.  Self-contained now.

    Pattern config in module-level ``_REASONING_MISMATCH_PATTERNS``.
    Adding a new pattern is one tuple append.
    """
    reasoning = action.get('Reasoning', action.get('reasoning', '')).lower()
    if not reasoning:
        return None
    if not any(verb in reasoning for verb in _WINDOW_TARGETED_VERBS):
        return None
    active = get_active_window_info()
    if not active:
        return None
    active_lower = active.lower()
    for reasoning_kw, window_kw in _REASONING_MISMATCH_PATTERNS:
        if reasoning_kw in reasoning and window_kw not in active_lower:
            return (f"VLM thinks {reasoning_kw.title()} but active window "
                    f"is: {active}")
    return None


def execute_action(action: dict, tier: str, *,
                   window_handle: int = None,
                   verify: bool = False,
                   if_occluded: str = 'skip',
                   safety: bool = False) -> dict:
    """
    Execute a single VLM action (click, type, key, etc.).

    Includes active window validation — if the VLM's reasoning mentions
    a window name that doesn't match the actual foreground window,
    the action is flagged (prevents clicking the wrong app's taskbar icon).

    Phase 4 of vlm_best_of_all_worlds_plan.md §3 added the per-window
    keyword arguments below.  All are backward-compatible — every
    existing caller passes only ``(action, tier)`` and gets the same
    behaviour as before.

    Args:
        action: dict with 'action', optionally 'coordinate' (in
            window-local 0-1000 norm space when ``window_handle`` is
            set; in screen-pixel space otherwise), 'text', 'value',
            'path', 'reasoning'.
        tier: 'inprocess' or 'http'.
        window_handle: HWND from
            :func:`integrations.remote_desktop.window_capture.list_windows`.
            When set, ``coordinate`` is treated as window-local 0-1000
            normalized space and translated to current screen coords
            via the window's freshly-snapshotted rect (handles windows
            moved between capture and click).
        verify: when True, take a pre/post screenshot diff and retry
            once with a 50-px nudge if no visible change occurred.
        if_occluded: policy for non-foreground / occluded windows:
              ``'skip'`` (default)        — return status='window_occluded'
              ``'foreground'``            — SetForegroundWindow first, then click
              ``'force'``                 — click regardless (PrintWindow-captured
                                            click target may underlie another window)
        safety: opt-in safety layer (Phase 6 of vlm_best_of_all_worlds_plan
            §5).  When True, runs the action through the SessionGuard
            (per-session cap + per-second throttle), the WindowBlocklist
            (refuses lsass / password managers / banking-titled windows),
            and writes a JSONL audit record per attempt.  Existing call
            sites that don't pass safety=True are unchanged.

    Returns:
        dict with 'output' and optionally 'error', 'window_mismatch',
        'status', 'translated_from', 'translated_to', 'verify_diff',
        'safety_block' (when safety=True and a guard refused).
    """
    _mismatch = _check_reasoning_mismatch(action)

    # Phase 4: per-window translation + occlusion handling.  Mutates
    # action['coordinate'] in place when needed; returns an early
    # status dict when the window can't be acted on safely.
    _window_meta = None
    if window_handle is not None:
        _window_meta, _early = _prepare_window_for_action(
            window_handle, action, if_occluded)
        if _early is not None:
            if safety:
                _emit_audit(action, _early, _window_meta, None,
                            block_reason=_early.get('status'))
            return _early

    # Phase 6: safety guards run BEFORE any pyautogui call so a refusal
    # never reaches the user's screen.  Order matters — session-level
    # rate cap is cheapest, run first; window blocklist needs window
    # metadata so runs second.
    if safety:
        _block = _check_safety(_window_meta)
        if _block is not None:
            _result = {
                'output': '', 'status': 'safety_blocked',
                'error': _block, 'safety_block': _block,
            }
            if _window_meta is not None:
                _result['window'] = _window_meta
            _emit_audit(action, _result, _window_meta, None,
                        block_reason=_block)
            return _result

    # Phase 4: pre-action screenshot for verify=True diff.
    _pre_b64 = None
    if verify and tier == 'inprocess':
        try:
            _pre_b64 = take_screenshot('inprocess')
        except Exception as e:
            logger.debug(f"verify pre-screenshot skipped: {e}")

    if tier == 'inprocess':
        result = _execute_inprocess(action)
    else:
        result = _execute_http(action)

    if _mismatch:
        result['window_mismatch'] = _mismatch
        import logging
        logging.getLogger('hevolve.vlm').warning(f"[WINDOW-MISMATCH] {_mismatch}")

    # Phase 4: surface window metadata so the loop's caller can audit.
    if _window_meta is not None:
        result.setdefault('window', _window_meta)

    # Phase 4: post-click verify with one 50-px nudge retry.
    if _pre_b64 is not None and result.get('error') is None:
        result = _post_click_verify(
            action, result, _pre_b64,
            tier=tier, window_meta=_window_meta)

    # Phase 6: record the action in the session guard + audit log.
    # Only record on a successful (non-error) attempt — refusals were
    # logged above and don't count against the session cap.
    if safety and result.get('error') is None:
        try:
            from integrations.vlm.safety import get_session_guard
            get_session_guard().record()
        except Exception as e:
            logger.debug(f"safety: session guard record failed: {e}")
        _emit_audit(action, result, _window_meta, _pre_b64)

    return result


# ─── Phase 6 helper plumbing ──────────────────────────────────────────

def _check_safety(window_meta):
    """Run rate guard + window blocklist.  Returns block-reason
    string when refusing, None when OK."""
    try:
        from integrations.vlm.safety import (
            get_session_guard, is_window_blocked)
    except Exception as e:
        logger.debug(f"safety module unavailable: {e}")
        return None
    reason = get_session_guard().check()
    if reason is not None:
        return reason
    return is_window_blocked(window_meta)


def _emit_audit(action, result, window_meta, screenshot_b64,
                block_reason=None):
    """Best-effort audit log — failures must NOT bubble up and break
    the action path."""
    try:
        from integrations.vlm.safety import get_audit_logger
        get_audit_logger().log(
            action, result, window_meta=window_meta,
            screenshot_b64=screenshot_b64,
            block_reason=block_reason)
    except Exception as e:
        logger.debug(f"audit log failed: {e}")


# ─── Phase 4 helpers (per-window translation + post-click verify) ────


def _prepare_window_for_action(window_handle: int, action: dict,
                                if_occluded: str):
    """Refresh the window's rect, decide if it can be acted on, and
    translate action's window-local 0-1000 coords into screen pixels
    in place.  Returns ``(window_meta, early_result_or_None)``.

    When the second tuple element is non-None, ``execute_action``
    returns it immediately without touching pyautogui — the window
    can't be acted on safely.
    """
    try:
        from integrations.remote_desktop.window_capture import (
            WindowEnumerator, WindowInfo)
    except ImportError as e:
        logger.debug(f"window_capture unavailable: {e}")
        return None, {
            'output': '', 'status': 'window_capture_unavailable',
            'error': f'window_capture import failed: {e}',
        }

    enum = WindowEnumerator()
    fresh = enum.refresh_window_info(WindowInfo(
        hwnd=window_handle, title='', process_name='',
        pid=0, rect=(0, 0, 0, 0)))
    if fresh is None:
        return None, {
            'output': '', 'status': 'window_destroyed',
            'error': f'hwnd={window_handle} no longer exists',
        }
    wx, wy, ww, wh = fresh.rect
    if ww <= 0 or wh <= 0:
        return fresh.to_dict(), {
            'output': '', 'status': 'window_offscreen',
            'error': f'window rect collapsed to {fresh.rect}',
            'window': fresh.to_dict(),
        }
    # Occlusion / minimized handling per policy.
    needs_foreground = fresh.minimized or not fresh.visible
    if needs_foreground:
        if if_occluded == 'skip':
            return fresh.to_dict(), {
                'output': '', 'status': 'window_minimized',
                'error': 'window minimized; pass if_occluded="foreground" '
                         'to bring it forward first',
                'window': fresh.to_dict(),
            }
        if if_occluded in ('foreground', 'force'):
            _bring_foreground(window_handle)
    # Translate window-local 0-1000 normalized coords → screen pixels.
    coord = action.get('coordinate')
    if coord and isinstance(coord, (list, tuple)) and len(coord) >= 2:
        nx, ny = coord[0], coord[1]
        if 0 <= nx <= 1000 and 0 <= ny <= 1000:
            sx = wx + int(nx * ww / 1000)
            sy = wy + int(ny * wh / 1000)
            action['_translated_from'] = (nx, ny)
            action['coordinate'] = [sx, sy]
            action['_translated_to'] = (sx, sy)
        else:
            # Out-of-range norm coords → caller passed screen pixels;
            # leave alone and let the action execute as-is.
            pass
    return fresh.to_dict(), None


def _bring_foreground(hwnd: int) -> None:
    """SetForegroundWindow + ShowWindow(SW_RESTORE) so a minimized /
    backgrounded window becomes the click target.  Best-effort —
    Windows blocks SetForegroundWindow from non-foreground processes
    in many cases, so callers shouldn't assume it always works."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        SW_RESTORE = 9
        ctypes.windll.user32.ShowWindow(int(hwnd), SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(int(hwnd))
        # Brief sleep — SetForegroundWindow is async, the click can
        # arrive before the new foreground window is composited.
        time.sleep(0.10)
    except Exception as e:
        logger.debug(f"bring-foreground hwnd={hwnd} failed: {e}")


# Diff thresholds for _post_click_verify.  Named so reviewers (and
# tests) don't have to guess what 0.005 / 16 mean.
#: Fraction-of-changed-pixels below which we consider the screen
#: "unchanged" → triggers a 50-px nudge retry.  0.5% covers JPEG
#: noise on a static frame and small cursor sprites without false-
#: triggering on real UI updates (button press → dialog → > 5%).
VERIFY_DIFF_THRESHOLD: float = 0.005

#: Per-pixel grayscale delta above which a pixel counts as "changed".
#: Set to absorb JPEG-quality-70 quantization noise (typically < 8).
VERIFY_PIXEL_NOISE_FLOOR: int = 16

#: How far to nudge the click on a no-change retry (screen px).
#: Half a typical button width — high enough to escape a missed edge,
#: low enough to stay inside the same UI element.
VERIFY_NUDGE_PX: int = 50


def _post_click_verify(action: dict, result: dict, pre_b64: str, *,
                       tier: str, window_meta: dict = None) -> dict:
    """Take a post-action screenshot, diff against pre, and if no
    visible change occurred, retry the action once with a 50-px
    nudge.  Annotates the result with 'verify_diff' (0.0–1.0) and
    'verify_retried' so callers can see what happened.
    """
    try:
        time.sleep(0.20)  # let the GUI settle before re-snapshot
        post_b64 = take_screenshot(tier)
    except Exception as e:
        # Surface the failure loudly — verification is a contract,
        # not a courtesy.  WARNING (not debug) so users notice when
        # the screenshot path is broken; downstream callers can read
        # verify_error and decide whether to trust the action result.
        logger.warning(
            f"verify post-screenshot failed - cannot detect no-op clicks "
            f"this iteration: {e}")
        result['verify_diff'] = None
        result['verify_error'] = f'post-screenshot failed: {e}'
        result['verify_retried'] = False
        return result
    diff = _quick_image_diff(pre_b64, post_b64)
    result['verify_diff'] = round(diff, 3)
    if diff < VERIFY_DIFF_THRESHOLD:
        # No visible change — try one nudge.  Only meaningful for
        # click-type actions with a coordinate.
        coord = action.get('coordinate')
        if coord and isinstance(coord, (list, tuple)) and len(coord) >= 2:
            nudged = [int(coord[0]) + VERIFY_NUDGE_PX, int(coord[1])]
            nudged_action = dict(action, coordinate=nudged)
            logger.info(
                f"verify: no visible change after click @ {coord}; "
                f"retrying with 50-px nudge → {nudged}")
            try:
                if tier == 'inprocess':
                    _ = _execute_inprocess(nudged_action)
                else:
                    _ = _execute_http(nudged_action)
            except Exception as e:
                logger.debug(f"verify-retry failed: {e}")
            result['verify_retried'] = True
            result['verify_nudge_to'] = nudged
        else:
            result['verify_retried'] = False
    else:
        result['verify_retried'] = False
    return result


def _quick_image_diff(b64_a: str, b64_b: str) -> float:
    """Fraction of significantly-changed pixels between two base64
    JPEGs.  Downsizes to 64×64 grayscale for speed (each image →
    4096 bytes → 4096 cheap subtractions).  Returns 0.0 (identical)
    to 1.0 (every pixel differs by > 16).
    """
    try:
        from PIL import Image
        import base64 as _b64
        ima = Image.open(io.BytesIO(_b64.b64decode(b64_a))).convert('L').resize((64, 64))
        imb = Image.open(io.BytesIO(_b64.b64decode(b64_b))).convert('L').resize((64, 64))
        ba = ima.tobytes()
        bb = imb.tobytes()
        n = len(ba)
        if n == 0:
            return 0.0
        # Per-pixel noise floor absorbs JPEG-compression noise on
        # unchanged regions (see VERIFY_PIXEL_NOISE_FLOOR docstring).
        changed = sum(1 for a, b in zip(ba, bb)
                       if abs(a - b) > VERIFY_PIXEL_NOISE_FLOOR)
        return changed / n
    except Exception:
        # Conservative: report no diff so we don't trigger spurious nudges.
        return 0.0


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
            from core.safe_hartos_attr import safe_hartos_attr
            _handle_shell_command_tool = safe_hartos_attr(
                '_handle_shell_command_tool')
            if _handle_shell_command_tool is None:
                logger.info(
                    "open_file_gui blocked: HARTOS _handle_shell_command_tool "
                    "not yet resolvable (loader still init). Failing closed "
                    "to preserve denylist guarantees.",
                )
                return {
                    'output': '',
                    'error': 'open_file_gui unavailable: HARTOS still loading',
                    'status': 'error',
                }
            result_text = _handle_shell_command_tool(shell_cmd)
            logger.info(
                "open_file_gui dispatched: cmd=%r exit_signature=%r",
                shell_cmd, (result_text or '')[:40],
            )
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
            from core.safe_hartos_attr import safe_hartos_attr
            _handle_shell_command_tool = safe_hartos_attr(
                '_handle_shell_command_tool')
            if _handle_shell_command_tool is None:
                logger.info(
                    "VLM shell action blocked: HARTOS "
                    "_handle_shell_command_tool not yet resolvable. "
                    "Failing closed (denylist unavailable) — cmd=%r",
                    (cmd or '')[:80],
                )
                return {
                    'output': '',
                    'error': (
                        "shell action unavailable: HARTOS still loading. "
                        "Refusing to run without the shared denylist."
                    ),
                    'status': 'error',
                }
            logger.info(
                "VLM shell action dispatching: cmd=%r",
                (cmd or '')[:80],
            )
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
