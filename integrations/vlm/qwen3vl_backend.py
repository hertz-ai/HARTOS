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
        try:
            from core.port_registry import get_port
            _llm_port = get_port('llm')
        except Exception as _port_err:
            # Sensible fallback - port_registry unavailable means we
            # are running outside the bundled Nunba context (test /
            # standalone).  Honour HEVOLVE_LLM_PORT env or default 8080.
            logger.debug(f"port_registry unavailable, using env/8080: {_port_err}")
            _llm_port = int(os.environ.get('HEVOLVE_LLM_PORT', 8080))
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
                    "url": f"data:image/jpeg;base64,{screenshot_b64}"
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
                    "url": f"data:image/jpeg;base64,{screenshot_b64}"
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
        except Exception as e:
            # OS-context probes are nice-to-have - the VLM still
            # works without them.  Log so silent-fallback doesn't
            # mask a broken probe (osascript/wmctrl/PowerShell missing).
            logger.debug(f"_get_os_context probe failed: {e}")
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
        """Parse VLM response into action dict. Returns
        ``(result_dict, nx, ny)`` or ``(result_dict, None, None)``.

        Phase 5: thin shim onto :func:`integrations.vlm.parser.parse_vlm_action`
        with ``expected_shape='point_only'``.  The byte-equivalent
        legacy fields are reproduced via
        :meth:`ParsedAction.to_point_action_dict`.

        ``img_w/img_h`` arg kept for back-compat — historically the
        function fell back to image dims when pyautogui.size() failed.
        Pyautogui screen size is the source of truth (we use it for
        the actual click), so we pass it through to the parser as
        the scaling target.
        """
        from integrations.vlm.parser import parse_vlm_action
        try:
            import pyautogui as _pag
            _screen_w, _screen_h = _pag.size()
        except Exception as _pag_err:
            # Pyautogui can fail when no display is attached (CI /
            # headless).  Fall back to image dims so the parser at
            # least produces stable norm_x/norm_y; downstream callers
            # that need true screen px will see them mismatch.
            logger.debug(f"pyautogui.size() unavailable, using image dims: {_pag_err}")
            _screen_w, _screen_h = img_w, img_h
        pa = parse_vlm_action(
            raw, expected_shape='point_only',
            task=task,
            screen_w=_screen_w, screen_h=_screen_h,
            detect_action_type=self._detect_action_type,
            scroll_down_keywords=self._SCROLL_DOWN_KEYWORDS,
            scroll_up_keywords=self._SCROLL_UP_KEYWORDS,
        )
        return pa.to_point_action_dict(), pa.norm_x, pa.norm_y

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

    # ─── Phase 10: P2P inference resolver ──────────────────────────────
    # Mobile devices can't competitively run a 4B+ multimodal model;
    # they capture, transmit to a paired peer (typically the user's
    # desktop Nunba) for inference, then execute the action locally.
    # This resolver picks where the VLM call goes based on
    # intelligence_preference + reachability.  Plan §8 / §10.

    def dispatch_inference(self, request: dict, *,
                            peer_dispatch=None,
                            intelligence_preference: str = 'hybrid'
                            ) -> dict:
        """Pick the right tier for a VLM inference request and run it.

        Args:
            request: dict with at least
                ``{'method', 'screenshot_b64', 'task'}``.  Optional
                keys: ``history``, ``window_rect``, ``platform``,
                ``request_id``, ``prefer_local``.
            peer_dispatch: optional callable
                ``peer_dispatch(channel, payload, timeout)`` to route
                to a paired peer over PeerLink.  When None, only
                local + cloud tiers are considered.
            intelligence_preference: ``'local_only'`` (default for
                desktop) | ``'hybrid'`` (try local first, peer as
                fallback) | ``'hive'`` (prefer peer/hive when local
                is busy or unreachable).

        Returns:
            dict with grounding result + ``'tier'`` field set to
            whichever path executed: ``'local'`` | ``'paired_peer'``
            | ``'hive'`` | ``'cloud'`` | ``'no_route'``.
        """
        method = request.get('method', 'point_and_act')
        screenshot_b64 = request.get('screenshot_b64', '')
        task = request.get('task', '')
        history = request.get('history')
        prefer_local = request.get('prefer_local', True)

        local_available = self._is_local_vlm_available()

        # Tier orderings per plan §10:
        #   local_only  → local (or no_route)
        #   hybrid      → local → paired_peer → hive → cloud  (always all 4)
        #   hive        → paired_peer → hive → local → cloud
        # Reviewer flagged that the prior 'hybrid' order excluded
        # 'cloud' when local was reachable, which contradicted the
        # plan's "fall through all four tiers" wording.  Now matches.
        if intelligence_preference == 'local_only':
            tiers = ['local'] if local_available else []
        elif intelligence_preference == 'hive':
            tiers = ['paired_peer', 'hive']
            if local_available:
                tiers.append('local')
            tiers.append('cloud')
        else:  # 'hybrid' (default)
            tiers = []
            if local_available and prefer_local:
                tiers.append('local')
            tiers += ['paired_peer', 'hive']
            if local_available and not prefer_local:
                tiers.append('local')
            tiers.append('cloud')

        for tier in tiers:
            try:
                if tier == 'local':
                    result = self._dispatch_local(method, screenshot_b64,
                                                    task, history)
                elif tier == 'paired_peer':
                    if peer_dispatch is None:
                        continue
                    result = self._dispatch_paired_peer(
                        request, peer_dispatch)
                    if result is None:
                        continue
                elif tier == 'hive':
                    if peer_dispatch is None:
                        continue
                    result = self._dispatch_hive(request, peer_dispatch)
                    if result is None:
                        continue
                elif tier == 'cloud':
                    result = self._dispatch_cloud(request)
                    if result is None:
                        continue
                else:
                    continue
                result['tier'] = tier
                return result
            except Exception as e:
                logger.debug(f'tier {tier} failed: {e}')
                continue

        return {'tier': 'no_route',
                'error': f'no inference path available '
                         f'(intelligence_preference={intelligence_preference})'}

    def _is_local_vlm_available(self) -> bool:
        """Quick reachability probe for the local VLM endpoint.

        Uses ``self.base_url`` (constructor attribute) — earlier
        version of this method referenced ``self.api_url`` which
        doesn't exist; reviewer caught the typo before it shipped
        to a real caller.  Llama-server's /health returns 200 OK
        when ready, 503 when warming up, anything else when down.
        """
        try:
            from core.http_pool import pooled_get
            health_url = self.base_url.rstrip('/').replace('/v1', '') + '/health'
            r = pooled_get(health_url, timeout=1)
            return r.status_code == 200
        except Exception as e:
            logger.debug(f'_is_local_vlm_available probe failed: {e}')
            return False

    def _dispatch_local(self, method, screenshot_b64, task, history):
        """Execute the requested method against the local VLM."""
        if method == 'parse_and_reason':
            return self.parse_and_reason(screenshot_b64, task,
                                          history=history)
        if method == 'point_and_act':
            return self.point_and_act(screenshot_b64, task,
                                       history=history)
        # Default to point_and_act for unknown methods.
        return self.point_and_act(screenshot_b64, task, history=history)

    def _dispatch_paired_peer(self, request, peer_dispatch):
        """Route to a paired peer over the PeerLink compute channel.
        Same wire shape both sides agree on (see plan §8 for the
        request/response schemas)."""
        try:
            payload = dict(request, type='vlm_grounding')
            response = peer_dispatch('compute', payload, timeout=60)
            if response and response.get('type') == 'vlm_grounding_result':
                return response
        except Exception as e:
            logger.debug(f'paired peer dispatch failed: {e}')
        return None

    def _dispatch_hive(self, request, peer_dispatch):
        """Same shape as paired_peer but routed via hivemind channel
        for hive-grade VLM nodes (compute-host tier)."""
        try:
            payload = dict(request, type='vlm_grounding')
            response = peer_dispatch('hivemind', payload, timeout=60)
            if response and response.get('type') == 'vlm_grounding_result':
                return response
        except Exception as e:
            logger.debug(f'hive dispatch failed: {e}')
        return None

    def _dispatch_cloud(self, request):
        """Last resort — Hevolve.ai cloud VLM via WorldModelBridge."""
        try:
            from integrations.world_model_bridge import dispatch_to_cloud
        except ImportError:
            return None
        try:
            return dispatch_to_cloud('vlm_grounding', request)
        except Exception as e:
            logger.debug(f'cloud dispatch failed: {e}')
            return None

    # ─── Phase 3.5: Complementary path router ──────────────────────────
    # The keystone of vlm_best_of_all_worlds_plan.md.  The three sibling
    # methods (point_and_act / parse_and_reason / run_local_agentic_loop)
    # aren't competitors — each has a real specialty.  route_task picks
    # the right path per task class instead of always hitting the same
    # primary first.  See plan §13 for the full design rationale.

    # Compiled at module-import time.  Word-boundary anchored so 'list'
    # inside 'specialist' doesn't trip the enumerate route.  Patterns
    # ordered most-specific-first within each list.
    _ENUMERATE_PATTERNS = [
        re.compile(r'\blist (?:all|every|each)\b', re.I),
        re.compile(r"\bwhat(?:\'s| is) on (?:the )?screen\b", re.I),
        re.compile(r'\bshow me (?:all|every|each)\b', re.I),
        re.compile(r'\bfind all\b', re.I),
        re.compile(r'\benumerate\b', re.I),
        re.compile(r'\bevery (?:clickable|button|icon|element|link|item)\b',
                   re.I),
        re.compile(r'\bhow many\b', re.I),
    ]
    _MULTI_STEP_PATTERNS = [
        re.compile(r'\b(?:and then|after that|then click|then type)\b',
                   re.I),
        re.compile(r'\bnavigate to\b', re.I),
        re.compile(r'\bfill (?:in|out)\b', re.I),
        re.compile(
            r'\b(?:open|launch|start|run)\b.+\band\b.+'
            r'\b(?:click|type|select|press|enter|play|search)\b',
            re.I,
        ),
        re.compile(r'\b(?:step \d+|first[,.]?\s+then|step-by-step)\b',
                   re.I),
    ]

    def route_task(self, task: str, context: dict = None) -> str:
        """Pick the best grounding path for *task*.

        Returns one of:
          ``'enumerate'``   — task asks about multiple/all UI elements
                              → use :meth:`parse_and_reason` for SoM
                              bbox view (revives the otherwise-dead path)
          ``'multi_step'``  — task chains multiple actions
                              → caller should drive
                              :func:`integrations.vlm.local_loop.run_local_agentic_loop`
          ``'single_shot'`` — one action on one target (default)
                              → use :meth:`point_and_act`

        Heuristic v1 (this implementation): keyword classifier on
        the task string only.  Fast (microseconds), no VLM call.
        Plan §13 v2: the draft 0.8B can self-classify in the same
        prompt that produces the action — defer until v1 baseline
        is established.

        Empty / None task returns 'single_shot' (the safest default —
        single VLM call, no over-commitment to a multi-iter loop).

        ``context`` reserved for future use (re-dispatch hints from
        prior iterations: e.g. the loop's body sees ``Status: DONE``
        after one click and feeds back ``{'observed_done_after': 1}``
        which would downgrade a multi_step verdict to single_shot).
        Currently ignored.
        """
        if not task:
            return 'single_shot'
        for pat in self._ENUMERATE_PATTERNS:
            if pat.search(task):
                return 'enumerate'
        for pat in self._MULTI_STEP_PATTERNS:
            if pat.search(task):
                return 'multi_step'
        return 'single_shot'

    def dispatch_grounding(self, screenshot_b64, task, *,
                           history=None, prev_screenshot_b64=None,
                           route: str = None):
        """Route *task* to the best grounding method via :meth:`route_task`,
        then call it.  Single entry point so callers don't have to know
        which of the three siblings to invoke for which task class.

        Behavior per route:
          * ``'enumerate'``   → :meth:`parse_and_reason` (SoM result)
          * ``'single_shot'`` → :meth:`point_and_act` (drop-in shape)
          * ``'multi_step'``  → returns a sentinel
            ``{'route': 'multi_step', 'recommend':
            'run_local_agentic_loop', 'reasoning': '...'}``
            so the caller can escalate to the loop dispatcher (which
            lives in local_loop.py and would create a circular import
            if called from inside the backend).

        ``route`` may be passed explicitly to override the heuristic
        (e.g. the loop dispatcher already decided multi_step and is
        calling per-iteration with route='single_shot').

        Every result has ``'route'`` set so the regression gate can
        catch silent routing drift across runs.
        """
        if route is None:
            route = self.route_task(task)

        if route == 'enumerate':
            result = self.parse_and_reason(
                screenshot_b64, task, history=history)
            result.setdefault('route', 'enumerate')
            return result

        if route == 'multi_step':
            # Sentinel — local_loop owns the multi-iter dispatch.
            # Returning instead of importing avoids backend → loop →
            # backend circular dependency.
            return {
                'action': None,
                'route': 'multi_step',
                'recommend': 'run_local_agentic_loop',
                'reasoning': (
                    'task chains multiple actions; caller should '
                    'dispatch to run_local_agentic_loop which calls '
                    'this backend per-iteration with route=single_shot'
                ),
                'latency': 0.0,
            }

        # Default: single_shot via point_and_act.
        result = self.point_and_act(
            screenshot_b64, task,
            history=history, prev_screenshot_b64=prev_screenshot_b64)
        result.setdefault('route', 'single_shot')
        return result

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

        # Screen dimensions for pyautogui coordinate scaling
        try:
            import pyautogui as _pag
            screen_w, screen_h = _pag.size()
        except Exception as _pag_err:
            logger.debug(
                f"pyautogui.size() unavailable, using image dims: {_pag_err}")
            screen_w, screen_h = img_w, img_h

        # --- Strategy 1: Taskbar pre-check via shared helper ---
        # Phase 3 of vlm_best_of_all_worlds_plan.md: replaced inline
        # taskbar_list code with a call to try_taskbar_pre_check (the
        # b7936bf helper).  Behavior is byte-identical to the prior
        # inline implementation — same _is_taskbar_task gate, same
        # _taskbar_list_lookup call, same return-dict shape, same
        # fall-through when no match.  Verified by the existing
        # TestPointAndActBottomEdgeRetry suite.
        taskbar_action = self.try_taskbar_pre_check(
            screenshot_b64, task, screen_w, screen_h, start)
        if taskbar_action is not None:
            return taskbar_action

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

        # --- Strategy 3: bias detection + elimination retry via helpers ---
        # Phase 3 refactor: replaced inline center/bottom/top-edge bias
        # checks + elimination prompt construction with detect_grounding_bias
        # + retry_with_elimination (b7936bf helpers).  Same patterns
        # detected, same retry prompt, same reproduced-bias rejection
        # rule.  Verified by TestPointAndActBottomEdgeRetry.
        bias_kind = self.detect_grounding_bias(nx, ny, result['action'], task)
        if bias_kind is not None:
            retry = self.retry_with_elimination(
                screenshot_b64, task, img_w, img_h, bias_kind)
            if retry is not None:
                # Helper returns (result, nx, ny) — strategy already
                # tagged 'elimination_retry' on the inner result.
                result, nx, ny = retry

        latency = time.time() - start
        result['latency'] = latency
        result.setdefault('strategy', 'describe_first')
        return result

    # ─── Shared grounding-strategy helpers ──────────────────────────────
    # Extracted from point_and_act so the multi-iteration agentic loop
    # (integrations/vlm/local_loop.py) can use them too.  point_and_act
    # was refactored in Phase 3 of vlm_best_of_all_worlds_plan.md to
    # call these helpers instead of maintaining inline copies, so
    # there is now ONE source of truth for taskbar shortcut + bias
    # detection + elimination retry — no parallel paths.
    #
    # Why it matters: commit 8fa6e97 (Apr 10, 2026 — "Single VLM call:
    # plan + ground in one prompt — halves per-step latency") moved the
    # loop OFF point_and_act onto its own inline prompt to halve
    # latency.  That trade-off shipped the latency win but silently
    # dropped point_and_act's smart grounding (taskbar_list shortcut +
    # center/bottom/top-edge bias detection + elimination_retry).
    # These helpers restore those strategies to the loop without
    # paying point_and_act's two-phase latency cost.

    def try_taskbar_pre_check(self, screenshot_b64, task,
                              screen_w, screen_h, started_at):
        """Pre-VLM-call taskbar shortcut.

        When the task targets a taskbar item ("open Chrome", "click
        Start button", etc.), skip the heavy describe_first VLM call
        and use _taskbar_list_lookup directly.  Returns the click
        action dict on a hit, None on a miss (caller falls through to
        its normal VLM grounding path).

        Args:
            screenshot_b64: current screen as base64 (JPEG/PNG)
            task: user instruction
            screen_w, screen_h: physical screen pixel dimensions for
                pyautogui coordinate scaling (norm 0-1000 → screen px)
            started_at: time.time() value from the caller's start —
                used to compute total latency for telemetry parity
                with point_and_act.

        Returns:
            dict (point_and_act-compatible action shape) or None.
        """
        if not self._is_taskbar_task(task):
            return None
        logger.info(f"Using taskbar_list strategy for: {task}")
        match, list_raw = self._taskbar_list_lookup(screenshot_b64, task)
        if not match:
            logger.info("taskbar_list: no match found, falling through")
            return None
        nx, ny, match_line = match
        px = int(nx * screen_w / 1000)
        py = int(ny * screen_h / 1000)
        return {
            'action': 'left_click',
            'screen_x': px, 'screen_y': py,
            'norm_x': nx, 'norm_y': ny,
            'text': '', 'done': False,
            'reasoning': f'taskbar_list: {match_line}',
            'raw': list_raw,
            'latency': time.time() - started_at,
            'strategy': 'taskbar_list',
        }

    def detect_grounding_bias(self, nx, ny, action, task):
        """Pure-function bias detector for VLM-grounded click coords.

        Returns 'center' | 'bottom-edge' | 'top-edge' | None.  Mirrors
        the inline checks in point_and_act so the loop can ask the
        same question on its own grounded coords.  Coordinates are
        in 0-1000 normalized space.
        """
        if nx is None or ny is None or action != 'left_click':
            return None
        is_center = (350 < nx < 650 and 350 < ny < 650)
        task_lower = task.lower()
        task_is_taskbar = self._is_taskbar_task(task) or any(
            kw in task_lower for kw in
            ('taskbar', 'start button', 'system tray')
        )
        is_bottom_edge = (ny > 930 and not task_is_taskbar)
        is_top_edge = (ny < 30)
        if is_bottom_edge:
            return 'bottom-edge'
        if is_top_edge:
            return 'top-edge'
        if is_center:
            return 'center'
        return None

    def retry_with_elimination(self, screenshot_b64, task,
                               img_w, img_h, bias_kind):
        """Elimination-retry VLM call for biased coordinates.

        When detect_grounding_bias flags a coord, this re-asks the VLM
        with a more pointed prompt (top/bottom/left/right thirds,
        avoid taskbar strip).  Returns (result, nx, ny) on a clean
        re-grounding, None when the retry reproduces the same bias
        (caller keeps the original coords).

        bias_kind: one of 'center' | 'bottom-edge' | 'top-edge'.
        """
        logger.info(
            f"{bias_kind}-biased coords for non-taskbar task, "
            f"retrying with elimination strategy"
        )
        elim_prompt = (
            f'I need to find the target for: {task}\n'
            f'Describe its location precisely BEFORE giving coordinates:\n'
            f'  - Top half or bottom half?\n'
            f'  - Left third, middle third, or right third?\n'
            f'  - Is it inside a window, in a menu, or on the taskbar?\n'
            f'If the task asks to open an app and that app is not '
            f'already visible, the correct action is usually NOT a '
            f'click — respond with DONE and I will use a keyboard '
            f'shortcut instead.\n'
            f'Otherwise, give the precise <point>x,y</point> (0-1000 normalized) '
            f'and avoid the taskbar strip (y > 930) unless the target '
            f'is an actual taskbar icon.'
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
        elim_result, enx, eny = self._parse_action_response(
            elim_raw, img_w, img_h, task=task,
        )
        # Reject only if the retry reproduced the original bias.
        if enx is None or eny is None:
            return None
        if bias_kind == 'bottom-edge':
            task_lower = task.lower()
            task_is_taskbar = self._is_taskbar_task(task) or any(
                kw in task_lower for kw in
                ('taskbar', 'start button', 'system tray')
            )
            if eny > 930 and not task_is_taskbar:
                return None
        elif bias_kind == 'top-edge':
            if eny < 30:
                return None
        elif bias_kind == 'center':
            if 350 < enx < 650 and 350 < eny < 650:
                return None
        elim_result['strategy'] = 'elimination_retry'
        logger.info(f"Elimination retry gave ({enx},{eny}) — using it")
        return elim_result, enx, eny

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
                    "url": f"data:image/jpeg;base64,{screenshot_b64}"
                }},
            ]
        }]
        return self._call_api(messages)

    def _call_api(self, messages):
        """Call Qwen3-VL OpenAI-compatible API."""
        from core.http_pool import pooled_post

        try:
            resp = pooled_post(
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
            msg = data['choices'][0]['message']
            # Qwen3.5 thinking mode: content may be None if all output is in
            # reasoning_content. Fall back to reasoning_content if content is empty.
            content = msg.get('content')
            if not content and msg.get('reasoning_content'):
                content = msg['reasoning_content']
            return content or ''
        except Exception as e:
            logger.error(f"Qwen3-VL API call failed: {e}")
            raise

    def _parse_unified_response(self, response_text):
        """Parse Qwen3-VL JSON response, handling markdown blocks and partial JSON.

        Phase 5: thin shim onto :mod:`integrations.vlm.parser`.  Same
        dict shape (UI_Elements + Next Action + Status + Reasoning)
        as the historical inline implementation, but the JSON
        extraction (code-block / raw-brace / depth-counted) lives in
        one canonical place now.
        """
        from integrations.vlm.parser import parse_vlm_action
        pa = parse_vlm_action(
            response_text or '', expected_shape='som_bbox')
        result = pa.to_action_json_dict()
        # Legacy callers expect UI_Elements always present (default to []).
        result.setdefault('UI_Elements', [])
        return result

    @staticmethod
    def _get_image_dimensions(b64_data):
        """Get width, height from base64 PNG/JPEG image."""
        try:
            from PIL import Image
            img_bytes = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(img_bytes))
            return img.width, img.height
        except Exception as e:
            # Fallback to common resolution.  Log because using the
            # wrong resolution causes coord-scaling drift downstream;
            # silent fallback would be diagnosable only via wrong-
            # location-clicks symptoms in production.
            logger.debug(f"_get_image_dimensions failed, using 1920x1080 fallback: {e}")
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
