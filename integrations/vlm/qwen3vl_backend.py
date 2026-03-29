"""
qwen3vl_backend.py - Unified Qwen3-VL backend for Computer Use.

Replaces the 3-model pipeline (OmniParser + MiniCPM + separate LLM) with a
single Qwen3-VL call that handles screen parsing, bbox grounding, scene
description, and action reasoning in one pass.

Qwen3-VL returns bounding boxes in normalized [0, 1000] coordinates.
This module converts them to pixel coordinates for pyautogui consumption.

Usage:
    backend = get_qwen3vl_backend()
    result = backend.parse_and_reason(screenshot_b64, "Click the Save button")
    # result = {screen_info, parsed_content_list, action_json, reasoning}
"""

import os
import io
import json
import re
import base64
import logging
import time

logger = logging.getLogger('hevolve.vlm.qwen3vl_backend')

_instance = None

# Prompt for unified screen parsing + action reasoning
UNIFIED_PROMPT = """You are a computer use agent analyzing a screenshot.

Task: {instruction}

Analyze the screenshot and:
1. Identify all visible UI elements (buttons, text fields, links, menus, icons, checkboxes, tabs).
2. For each element, provide its bounding box as [x1, y1, x2, y2] in pixel coordinates.
3. Given the task, decide the next action.

Output ONLY valid JSON:
{{
  "UI_Elements": [
    {{"id": 1, "type": "button", "label": "element text", "bbox": [x1, y1, x2, y2]}},
    ...
  ],
  "Reasoning": "Brief explanation of current screen state and why this action is needed",
  "Next Action": "left_click | right_click | double_click | type | key | hotkey | scroll_up | scroll_down | wait | None",
  "Box ID": null,
  "coordinate": [x, y],
  "value": "text to type or key to press (if applicable)",
  "Status": "IN_PROGRESS | DONE"
}}

When the task is complete, set "Next Action": "None" and "Status": "DONE".
If clicking a UI element, set "Box ID" to the element's id and "coordinate" to its center."""

# Prompt for screen parsing only (drop-in replacement for OmniParser)
PARSE_ONLY_PROMPT = """Analyze this screenshot. List every visible UI element.

For each element provide:
- Sequential ID number
- Element type (button, textfield, link, icon, menu, tab, checkbox, label, image, dropdown)
- Label or text content
- Bounding box as [x1, y1, x2, y2] in pixel coordinates

Output ONLY valid JSON:
{{
  "UI_Elements": [
    {{"id": 1, "type": "button", "label": "Save", "bbox": [100, 50, 200, 80]}},
    {{"id": 2, "type": "textfield", "label": "filename", "bbox": [210, 50, 400, 80]}}
  ]
}}"""


def get_qwen3vl_backend():
    """Get singleton Qwen3VLBackend instance."""
    global _instance
    if _instance is None:
        _instance = Qwen3VLBackend()
    return _instance


class Qwen3VLBackend:
    """Unified screen parsing + action reasoning via Qwen3-VL."""

    def __init__(self, base_url=None, model_name=None):
        _llm_port = os.environ.get('HEVOLVE_LLM_PORT', '8080')
        self.base_url = base_url or os.environ.get(
            'HEVOLVE_VLM_ENDPOINT_URL',
            os.environ.get('HEVOLVE_LLM_ENDPOINT_URL', f'http://127.0.0.1:{_llm_port}/v1')
        )
        self.model_name = model_name or os.environ.get(
            'HEVOLVE_VLM_MODEL_NAME',
            os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'local')
        )
        self.api_key = os.environ.get(
            'HEVOLVE_VLM_API_KEY',
            os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy')
        )
        self.timeout = int(os.environ.get('HEVOLVE_VLM_TIMEOUT', '90'))

    def parse_and_reason(self, screenshot_b64, task_instruction, history=None):
        """
        Single call: screenshot → UI elements + bbox + action decision.

        Args:
            screenshot_b64: Base64-encoded PNG screenshot
            task_instruction: What the user wants done
            history: Optional conversation history (list of message dicts)
        Returns:
            dict with keys:
            - screen_info: str (ID→label text for display)
            - parsed_content_list: list of {id, type, label, bbox}
            - action_json: dict with Next Action, coordinate, value, Status
            - reasoning: str
            - latency: float
        """
        prompt_text = UNIFIED_PROMPT.format(instruction=task_instruction)
        start = time.time()

        messages = list(history) if history else []
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}"
                }},
            ]
        })

        raw = self._call_api(messages)
        latency = time.time() - start

        parsed = self._parse_unified_response(raw)

        # Get image dimensions for coordinate normalization
        img_w, img_h = self._get_image_dimensions(screenshot_b64)

        # Build OmniParser-compatible output
        ui_elements = parsed.get('UI_Elements', [])
        normalized_elements = []
        screen_info_lines = []

        for elem in ui_elements:
            bbox = elem.get('bbox', [])
            if len(bbox) == 4 and self._is_normalized_1000(bbox, img_w, img_h):
                bbox = self._normalize_bbox(bbox, img_w, img_h)

            normalized_elements.append({
                'idx': elem.get('id', 0),
                'type': elem.get('type', 'unknown'),
                'content': elem.get('label', ''),
                'bbox': bbox,
            })
            screen_info_lines.append(
                f"{elem.get('id', 0)}: {elem.get('type', '')} \"{elem.get('label', '')}\""
            )

        # Resolve Box ID → coordinate if needed
        action_json = {
            'Reasoning': parsed.get('Reasoning', ''),
            'Next Action': parsed.get('Next Action', 'None'),
            'Box ID': parsed.get('Box ID'),
            'coordinate': parsed.get('coordinate'),
            'value': parsed.get('value', ''),
            'Status': parsed.get('Status', 'IN_PROGRESS'),
        }

        if action_json['coordinate'] is None and action_json['Box ID'] is not None:
            for elem in normalized_elements:
                if elem['idx'] == action_json['Box ID']:
                    bbox = elem['bbox']
                    if len(bbox) == 4:
                        action_json['coordinate'] = [
                            int((bbox[0] + bbox[2]) / 2),
                            int((bbox[1] + bbox[3]) / 2),
                        ]
                    break

        return {
            'screen_info': '\n'.join(screen_info_lines),
            'parsed_content_list': normalized_elements,
            'action_json': action_json,
            'reasoning': parsed.get('Reasoning', ''),
            'latency': latency,
        }

    def parse_screen(self, screenshot_b64):
        """
        Screen parsing only — drop-in replacement for local_omniparser.parse_screen.

        Returns same dict format as OmniParser for backward compatibility.
        """
        start = time.time()

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": PARSE_ONLY_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}"
                }},
            ]
        }]

        raw = self._call_api(messages)
        latency = time.time() - start

        parsed = self._parse_unified_response(raw)
        img_w, img_h = self._get_image_dimensions(screenshot_b64)

        ui_elements = parsed.get('UI_Elements', [])
        content_list = []
        screen_info_lines = []

        for elem in ui_elements:
            bbox = elem.get('bbox', [])
            if len(bbox) == 4 and self._is_normalized_1000(bbox, img_w, img_h):
                bbox = self._normalize_bbox(bbox, img_w, img_h)

            content_list.append({
                'idx': elem.get('id', 0),
                'type': elem.get('type', 'unknown'),
                'content': elem.get('label', ''),
                'bbox': bbox,
            })
            screen_info_lines.append(
                f"{elem.get('id', 0)}: {elem.get('type', '')} \"{elem.get('label', '')}\""
            )

        return {
            'screen_info': '\n'.join(screen_info_lines),
            'parsed_content_list': content_list,
            'som_image_base64': screenshot_b64,
            'original_screenshot_base64': screenshot_b64,
            'width': img_w,
            'height': img_h,
            'latency': latency,
        }

    # Taskbar keywords — if task mentions any of these, use taskbar_list strategy
    _TASKBAR_KEYWORDS = {
        'taskbar', 'start button', 'start menu', 'search icon', 'search bar',
        'chrome', 'edge', 'firefox', 'file explorer', 'explorer icon',
        'clock', 'time display', 'system tray', 'notification', 'volume',
        'wifi', 'network', 'battery', 'spotify', 'discord', 'teams',
        'pinned', 'xbox', 'game bar',
        # App names that are typically in the taskbar
        'open chrome', 'open edge', 'open firefox', 'open explorer',
        'open spotify', 'open discord', 'open teams', 'open steam',
        'launch chrome', 'launch edge', 'launch firefox',
    }

    # Action keywords for detecting non-click actions from task text
    _RIGHT_CLICK_KEYWORDS = {'right-click', 'right click', 'context menu', 'rightclick'}
    _DOUBLE_CLICK_KEYWORDS = {'double-click', 'double click', 'doubleclick'}
    _SCROLL_DOWN_KEYWORDS = {'scroll down', 'scroll below', 'page down'}
    _SCROLL_UP_KEYWORDS = {'scroll up', 'scroll above', 'page up'}

    def _get_os_context(self):
        """Get OS window list with foreground/z-index info for grounding context."""
        try:
            import subprocess, platform
            _os = platform.system()
            if _os == 'Windows':
                # Get foreground window title via PowerShell
                _fg = subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     'Add-Type @"\nusing System;\nusing System.Runtime.InteropServices;\n'
                     'public class FG { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); '
                     '[DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder t, int c); }\n"@; '
                     '$h=[FG]::GetForegroundWindow(); $sb=New-Object System.Text.StringBuilder 256; '
                     '[void][FG]::GetWindowText($h,$sb,256); $sb.ToString()'],
                    capture_output=True, text=True, timeout=5)
                fg_title = _fg.stdout.strip() if _fg.returncode == 0 else ''

                # Get all windows
                _r = subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     'Get-Process | Where-Object {$_.MainWindowTitle -ne ""} | '
                     'Select-Object ProcessName, MainWindowTitle | ConvertTo-Json'],
                    capture_output=True, text=True, timeout=5)
                if _r.returncode == 0:
                    _wins = json.loads(_r.stdout)
                    if isinstance(_wins, dict):
                        _wins = [_wins]
                    _win_list = ', '.join(f'{w["ProcessName"]}:{w["MainWindowTitle"]}'
                                          for w in _wins if w.get('MainWindowTitle'))
                    fg_info = f' FOREGROUND (topmost): "{fg_title}".' if fg_title else ''
                    return f'OS: Windows.{fg_info} Open windows: [{_win_list}]\n'
            elif _os == 'Linux':
                # Get foreground window
                _fg = subprocess.run(['xdotool', 'getactivewindow', 'getwindowname'],
                                     capture_output=True, text=True, timeout=3)
                fg_title = _fg.stdout.strip() if _fg.returncode == 0 else ''
                _r = subprocess.run(['wmctrl', '-l'], capture_output=True, text=True, timeout=3)
                if _r.returncode == 0:
                    fg_info = f' FOREGROUND: "{fg_title}".' if fg_title else ''
                    return f'OS: Linux.{fg_info} Open windows: [{_r.stdout.strip()}]\n'
            elif _os == 'Darwin':
                # Get frontmost app
                _fg = subprocess.run(
                    ['osascript', '-e',
                     'tell application "System Events" to get name of first process whose frontmost is true'],
                    capture_output=True, text=True, timeout=3)
                fg_title = _fg.stdout.strip() if _fg.returncode == 0 else ''
                _r = subprocess.run(
                    ['osascript', '-e',
                     'tell application "System Events" to get name of every process whose visible is true'],
                    capture_output=True, text=True, timeout=3)
                if _r.returncode == 0:
                    fg_info = f' FOREGROUND: "{fg_title}".' if fg_title else ''
                    return f'OS: macOS.{fg_info} Visible apps: [{_r.stdout.strip()}]\n'
        except Exception:
            pass
        return ''

    def _detect_action_type(self, task, raw_response=''):
        """Detect action type from task text and VLM response.

        Returns one of: left_click, right_click, double_click, scroll_up, scroll_down, type, done
        """
        task_lower = task.lower()
        raw_lower = raw_response.lower()
        combined = task_lower + ' ' + raw_lower

        if any(kw in combined for kw in self._RIGHT_CLICK_KEYWORDS):
            return 'right_click'
        if any(kw in combined for kw in self._DOUBLE_CLICK_KEYWORDS):
            return 'double_click'
        if any(kw in combined for kw in self._SCROLL_DOWN_KEYWORDS):
            return 'scroll_down'
        if any(kw in combined for kw in self._SCROLL_UP_KEYWORDS):
            return 'scroll_up'
        return 'left_click'

    def _parse_action_response(self, raw, img_w, img_h, task=''):
        """Parse VLM response into action dict. Returns (result_dict, nx, ny) or (result_dict, None, None)."""
        raw = raw.strip()

        if 'DONE' in raw.upper():
            return {'action': 'done', 'screen_x': 0, 'screen_y': 0,
                    'text': '', 'done': True, 'reasoning': raw, 'raw': raw}, None, None

        # Detect TYPE: in response
        if raw.upper().startswith('TYPE:'):
            text = raw.split(':', 1)[1].strip()
            return {'action': 'type', 'screen_x': 0, 'screen_y': 0,
                    'text': text, 'done': False, 'reasoning': f'type "{text}"',
                    'raw': raw}, None, None

        # Also detect "type" action from VLM free-text responses
        type_match = re.search(r'(?:type|enter|input)\s*[:\-"\']+\s*(.+?)(?:\s*<|$)', raw, re.IGNORECASE)
        if type_match and '<point>' not in raw:
            text = type_match.group(1).strip().strip('"\'')
            return {'action': 'type', 'screen_x': 0, 'screen_y': 0,
                    'text': text, 'done': False, 'reasoning': f'type "{text}"',
                    'raw': raw}, None, None

        # Detect scroll actions (no coords needed)
        raw_lower = raw.lower()
        task_lower = task.lower() if task else ''
        if any(kw in task_lower or kw in raw_lower for kw in self._SCROLL_DOWN_KEYWORDS):
            return {'action': 'scroll_down', 'screen_x': 0, 'screen_y': 0,
                    'text': '', 'done': False, 'reasoning': 'scroll down',
                    'raw': raw}, None, None
        if any(kw in task_lower or kw in raw_lower for kw in self._SCROLL_UP_KEYWORDS):
            return {'action': 'scroll_up', 'screen_x': 0, 'screen_y': 0,
                    'text': '', 'done': False, 'reasoning': 'scroll up',
                    'raw': raw}, None, None

        # Detect action type from task context
        action_type = self._detect_action_type(task, raw)

        # Parse <point>x,y</point>
        m = re.search(r'<point>\s*(\d+)\s*,\s*(\d+)\s*</point>', raw)
        if m:
            nx, ny = int(m.group(1)), int(m.group(2))
            px = int(nx * img_w / 1000)
            py = int(ny * img_h / 1000)
            return {'action': action_type, 'screen_x': px, 'screen_y': py,
                    'norm_x': nx, 'norm_y': ny,
                    'text': '', 'done': False,
                    'reasoning': f'{action_type} at ({nx},{ny}) normalized',
                    'raw': raw}, nx, ny

        # Fallback: extract number pairs in 0-1000 range
        nums = re.findall(r'\d+', raw)
        if len(nums) >= 2:
            nx, ny = int(nums[0]), int(nums[1])
            if 0 <= nx <= 1000 and 0 <= ny <= 1000:
                px = int(nx * img_w / 1000)
                py = int(ny * img_h / 1000)
                return {'action': action_type, 'screen_x': px, 'screen_y': py,
                        'norm_x': nx, 'norm_y': ny,
                        'text': '', 'done': False,
                        'reasoning': f'fallback {action_type} ({nx},{ny})',
                        'raw': raw}, nx, ny

        logger.warning(f"Could not parse point_and_act response: {raw[:100]}")
        return {'action': 'none', 'screen_x': 0, 'screen_y': 0,
                'text': '', 'done': False, 'reasoning': raw,
                'raw': raw}, None, None

    def _is_taskbar_task(self, task):
        """Check if task involves taskbar elements."""
        task_lower = task.lower()
        return any(kw in task_lower for kw in self._TASKBAR_KEYWORDS)

    def _taskbar_list_lookup(self, screenshot_b64, target_name):
        """
        Taskbar list strategy: ask model to list ALL taskbar icons with coords,
        then find the target by name. Avg error=50, best for taskbar targets.

        Two-pass matching: first ask for the full list, then ask the model
        which item matches the target (avoids naive keyword matching).
        """
        list_raw = self._call_api([{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    'List every icon in the taskbar at the bottom of the screen, from LEFT to RIGHT. '
                    'For each icon give its <point>x,y</point> location. Format:\n'
                    '1. [icon name] <point>x,y</point>\n'
                    '2. [icon name] <point>x,y</point>\n...'
                )},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{screenshot_b64}"
                }},
            ]
        }])

        # Extract all items with coords from the list
        items = []
        for line in list_raw.split('\n'):
            m = re.search(r'<point>\s*(\d+)\s*,\s*(\d+)\s*</point>', line)
            if m:
                items.append((int(m.group(1)), int(m.group(2)), line.strip()))

        if not items:
            return None, list_raw

        # Smart matching: extract target keywords and score each item
        # Map common task phrases to icon names
        _ALIASES = {
            'start': ['start', 'windows', 'menu'],
            'search': ['search', 'magnif'],
            'chrome': ['chrome', 'google'],
            'edge': ['edge', 'microsoft edge'],
            'explorer': ['explorer', 'file', 'folder'],
            'clock': ['clock', 'time', 'date'],
            'volume': ['volume', 'sound', 'speaker'],
            'network': ['network', 'wifi', 'internet'],
        }

        task_lower = target_name.lower()
        search_terms = []
        for key, aliases in _ALIASES.items():
            if key in task_lower:
                search_terms.extend(aliases)
        if not search_terms:
            # Fallback: use significant words from task
            search_terms = [w for w in task_lower.split() if len(w) > 2
                           and w not in ('the', 'click', 'open', 'icon', 'button', 'taskbar')]

        best_match = None
        best_score = 0
        for nx, ny, line_text in items:
            line_lower = line_text.lower()
            score = sum(1 for term in search_terms if term in line_lower)
            if score > best_score:
                best_score = score
                best_match = (nx, ny, line_text)

        return best_match, list_raw

    def point_and_act(self, screenshot_b64, task, history=None, prev_screenshot_b64=None):
        """
        Optimized hybrid grounding strategy based on benchmark results.

        Strategy selection (benchmark-driven):
        1. Taskbar targets → taskbar_list (list all icons, pick by name) avg=50
        2. All targets → describe_first (describe position, then point) avg=78
        3. Suspicious center coords → elimination retry (halving search)

        Args:
            screenshot_b64: Current screenshot (base64 JPEG/PNG)
            task: What to accomplish (e.g. "Click the Start button")
            history: List of previous action strings for context
            prev_screenshot_b64: Previous screenshot for state change detection

        Returns:
            dict with: action, screen_x, screen_y, text, done, reasoning, raw
        """
        start = time.time()
        hist_text = ' → '.join(history[-3:]) if history else 'None'
        os_context = self._get_os_context()
        img_w, img_h = self._get_image_dimensions(screenshot_b64)

        # --- Strategy 1: Taskbar list for taskbar targets ---
        if self._is_taskbar_task(task):
            logger.info(f"Using taskbar_list strategy for: {task}")
            match, list_raw = self._taskbar_list_lookup(screenshot_b64, task)
            if match:
                nx, ny, match_line = match
                px = int(nx * img_w / 1000)
                py = int(ny * img_h / 1000)
                latency = time.time() - start
                return {'action': 'left_click', 'screen_x': px, 'screen_y': py,
                        'norm_x': nx, 'norm_y': ny,
                        'text': '', 'done': False,
                        'reasoning': f'taskbar_list: {match_line}',
                        'raw': list_raw, 'latency': latency,
                        'strategy': 'taskbar_list'}
            logger.info("taskbar_list: no match found, falling through to describe_first")

        # --- Strategy 2: describe_first (primary, avg=78) ---
        state_hint = ''
        if prev_screenshot_b64:
            state_hint = (
                'Compare this screenshot with the previous one. '
                'Did the screen change from the last action? '
                'If so, proceed to the next step. If not, the last action may have missed its target.\n\n'
            )

        prompt_text = (
            f'{os_context}'
            f'{state_hint}'
            f'Task: {task}\n'
            f'Previous actions: {hist_text}\n\n'
            f'What is the single next action? Do NOT repeat previous actions.\n\n'
            f'- To click: first describe WHERE the target is on screen '
            f'(which edge, which corner, left/right side), '
            f'then give <point>x,y</point> (0-1000 normalized).\n'
            f'- To right-click: describe WHERE, then give <point>x,y</point>\n'
            f'- To double-click: describe WHERE, then give <point>x,y</point>\n'
            f'- To type text: reply TYPE:the text here\n'
            f'- To scroll: reply SCROLL_UP or SCROLL_DOWN\n'
            f'- If task is complete: reply DONE'
        )

        messages = []
        if prev_screenshot_b64:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "Previous screenshot (before last action):"},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{prev_screenshot_b64}"
                    }},
                ]
            })
            messages.append({
                "role": "assistant",
                "content": f"Previous action: {history[-1] if history else 'none'}"
            })
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{screenshot_b64}"
                }},
            ]
        })

        raw = self._call_api(messages)
        result, nx, ny = self._parse_action_response(raw, img_w, img_h, task=task)

        # --- Strategy 3: elimination retry if coords look suspicious ---
        # "Suspicious" = near dead center (400-600, 400-600) which usually means
        # the model defaulted rather than actually grounding
        if nx is not None and ny is not None and result['action'] == 'left_click':
            is_center_biased = (350 < nx < 650 and 350 < ny < 650)
            if is_center_biased:
                logger.info(f"Center-biased coords ({nx},{ny}), retrying with elimination strategy")
                elim_prompt = (
                    f'I need to find the target for: {task}\n'
                    f'Is it in the top half or bottom half of the screen? '
                    f'Is it in the left third, middle third, or right third? '
                    f'Now give the precise <point>x,y</point> (0-1000 normalized).'
                )
                elim_raw = self._call_api([{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": elim_prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{screenshot_b64}"
                        }},
                    ]
                }])
                elim_result, enx, eny = self._parse_action_response(elim_raw, img_w, img_h, task=task)
                if enx is not None and not (350 < enx < 650 and 350 < eny < 650):
                    # Elimination gave non-center coords — trust it
                    result = elim_result
                    result['strategy'] = 'elimination_retry'
                    logger.info(f"Elimination retry gave ({enx},{eny}) — using it")

        latency = time.time() - start
        result['latency'] = latency
        result.setdefault('strategy', 'describe_first')
        return result

    def verify_goal(self, screenshot_b64, goal):
        """Check if the goal is achieved by looking at the current screenshot.

        Returns: (bool, str) — (achieved, explanation)
        """
        raw = self._call_api([{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    f'Is this goal achieved? Goal: "{goal}"\n'
                    f'Reply YES or NO and one sentence why.'
                )},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{screenshot_b64}"
                }},
            ]
        }])
        achieved = 'YES' in raw.upper().split('.')[0]
        return achieved, raw.strip()

    def describe_scene(self, screenshot_b64, prompt='Describe what you see in this image'):
        """Scene description — drop-in replacement for MiniCPM backend."""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}"
                }},
            ]
        }]
        return self._call_api(messages)

    def _call_api(self, messages):
        """Call Qwen3-VL OpenAI-compatible API."""
        import requests as _req

        try:
            resp = _req.post(
                f'{self.base_url.rstrip("/")}/chat/completions',
                json={
                    'model': self.model_name,
                    'messages': messages,
                    'max_tokens': 4096,
                    'temperature': 0.0,
                },
                headers={'Authorization': f'Bearer {self.api_key}'},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"Qwen3-VL API call failed: {e}")
            raise

    def _parse_unified_response(self, response_text):
        """Parse Qwen3-VL JSON response, handling markdown blocks and partial JSON."""
        # Try code block extraction first
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try raw JSON object
        brace_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response_text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        # Try more aggressive nested JSON extraction
        depth = 0
        start_idx = None
        for i, ch in enumerate(response_text):
            if ch == '{':
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start_idx is not None:
                    try:
                        return json.loads(response_text[start_idx:i + 1])
                    except json.JSONDecodeError:
                        start_idx = None

        logger.warning(f"Could not parse Qwen3-VL response as JSON: {response_text[:200]}")
        return {
            'UI_Elements': [],
            'Next Action': 'None',
            'Status': 'DONE',
            'Reasoning': response_text[:500],
        }

    @staticmethod
    def _get_image_dimensions(b64_data):
        """Get width, height from base64 PNG/JPEG image."""
        try:
            from PIL import Image
            img_bytes = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(img_bytes))
            return img.width, img.height
        except Exception:
            # Fallback to common resolution
            return 1920, 1080

    @staticmethod
    def _is_normalized_1000(bbox, img_w, img_h):
        """Check if bbox values are in Qwen3-VL's [0, 1000] normalized range."""
        if not bbox or len(bbox) != 4:
            return False
        # If all values are <=1000 and the image is larger than 1000px,
        # these are probably normalized coordinates
        max_val = max(bbox)
        return max_val <= 1000 and (img_w > 1000 or img_h > 1000)

    @staticmethod
    def _normalize_bbox(bbox_1000, img_w, img_h):
        """Convert Qwen3-VL [0, 1000] normalized bbox to pixel coordinates."""
        return [
            int(bbox_1000[0] * img_w / 1000),
            int(bbox_1000[1] * img_h / 1000),
            int(bbox_1000[2] * img_w / 1000),
            int(bbox_1000[3] * img_h / 1000),
        ]
