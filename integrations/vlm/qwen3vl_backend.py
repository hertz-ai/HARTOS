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
        self.base_url = base_url or os.environ.get(
            'HEVOLVE_VLM_ENDPOINT_URL',
            os.environ.get('HEVOLVE_LLM_ENDPOINT_URL', 'http://localhost:8000/v1')
        )
        self.model_name = model_name or os.environ.get(
            'HEVOLVE_VLM_MODEL_NAME',
            os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'Qwen3-VL-2B-Instruct')
        )
        self.api_key = os.environ.get(
            'HEVOLVE_VLM_API_KEY',
            os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy')
        )
        self.timeout = int(os.environ.get('HEVOLVE_VLM_TIMEOUT', '60'))

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
