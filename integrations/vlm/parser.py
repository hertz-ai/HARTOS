"""
integrations.vlm.parser — single source of truth for VLM response parsing.

Phase 5 of memory/vlm_best_of_all_worlds_plan.md §4.  Replaces three
parallel parsers that drifted apart over time:

  * ``local_loop._parse_vlm_response``         — JSON shape, used by
    inline-prompt branch
  * ``qwen3vl_backend._parse_unified_response`` — JSON shape with
    UI_Elements, used by parse_and_reason / parse_screen
  * ``qwen3vl_backend._parse_action_response`` — free-text shape with
    <point>x,y</point> / TYPE: / DONE / scroll, used by point_and_act
    and the taskbar shortcut

The first two duplicated their JSON extraction.  This module
exposes:

  ``extract_json(text)``          — single canonical JSON extractor
                                    (handles ```json blocks, raw {},
                                    depth-counted nested objects)
  ``ParsedAction`` dataclass      — normalized result; same fields
                                    regardless of input shape so
                                    downstream code stops branching
                                    on which parser ran.
  ``parse_vlm_action(raw, ...)``  — single entry point keyed on
                                    ``expected_shape='action_json' |
                                    'som_bbox' | 'point_only'``.

Old parsers are shimmed onto this module — see the docstrings on
each shim for byte-equivalence notes.
"""

import json
import re
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Callable

logger = logging.getLogger('hevolve.vlm.parser')


# ─── Pre-compiled regex (module-load cost is one-time) ────────────────

_CODE_BLOCK_RE = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)
_RAW_BRACE_RE = re.compile(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', re.DOTALL)
_POINT_RE = re.compile(r'<point>\s*(\d+)\s*,\s*(\d+)\s*</point>')
_TYPE_PREFIX_RE = re.compile(r'^TYPE:\s*(.+)$', re.IGNORECASE)
_TYPE_FREETEXT_RE = re.compile(
    r'(?:type|enter|input)\s*[:\-"\']+\s*(.+?)(?:\s*<|$)',
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r'\d+')


# ─── ParsedAction dataclass ──────────────────────────────────────────

@dataclass
class ParsedAction:
    """Normalized result of parsing any of the three VLM response
    shapes.  Fields not relevant to a given shape stay at their
    default values; consumers read whichever fields apply.

    Back-compat conversion methods:
      ``to_action_json_dict()`` reproduces what ``_parse_vlm_response``
        and ``_parse_unified_response`` historically returned.
      ``to_point_action_dict()`` reproduces the ``_parse_action_response``
        shape (the dict point_and_act builds its result from).
    """
    raw: str = ''
    action: str = 'none'           # 'left_click', 'type', 'scroll_down', 'done', 'none', ...
    reasoning: str = ''
    done: bool = False
    status: str = ''               # 'IN_PROGRESS', 'DONE'  (JSON shapes)
    text: str = ''                 # for 'type' actions
    norm_x: Optional[int] = None   # 0-1000 normalized
    norm_y: Optional[int] = None
    screen_x: int = 0
    screen_y: int = 0
    box_id: Optional[int] = None
    coordinate: Optional[List[int]] = None
    next_action: str = ''          # original 'Next Action' string
    ui_elements: List[dict] = field(default_factory=list)
    parsed_content_list: List[dict] = field(default_factory=list)

    def to_action_json_dict(self) -> dict:
        """Convert to the dict legacy ``_parse_vlm_response`` /
        ``_parse_unified_response`` callers consume.

        Keeps original casing of 'Next Action' / 'Status' / 'Reasoning'
        + 'Box ID' to avoid breaking downstream key access.
        """
        out = {
            'Next Action': self.next_action or 'None',
            'Status': self.status or ('DONE' if self.done else 'IN_PROGRESS'),
            'Reasoning': self.reasoning or self.raw[:500],
        }
        if self.text:
            out['value'] = self.text
        if self.coordinate is not None:
            out['coordinate'] = self.coordinate
        if self.box_id is not None:
            out['Box ID'] = self.box_id
        if self.ui_elements:
            out['UI_Elements'] = self.ui_elements
        if self.parsed_content_list:
            out['parsed_content_list'] = self.parsed_content_list
        return out

    def to_point_action_dict(self) -> dict:
        """Convert to the dict shape ``_parse_action_response`` returns
        for point_and_act.  Includes only keys the legacy code populated.
        """
        result = {
            'action': self.action,
            'screen_x': self.screen_x,
            'screen_y': self.screen_y,
            'text': self.text,
            'done': self.done,
            'reasoning': self.reasoning,
            'raw': self.raw,
        }
        if self.norm_x is not None:
            result['norm_x'] = self.norm_x
        if self.norm_y is not None:
            result['norm_y'] = self.norm_y
        return result


# ─── Extract JSON ─────────────────────────────────────────────────────

def extract_json(text: str) -> Optional[dict]:
    """Extract a JSON object from VLM text.

    Tries in order:
      1. Markdown `````json``... fenced block (most reliable —
         models trained on instruction data tend to fence JSON).
      2. Depth-counted brace walk (correctly handles nested objects;
         returns the OUTER object).
      3. Simple raw ``{...}`` match (last-resort cheap path; only
         reached when the depth-counted walk found nothing
         JSON-parseable).

    The legacy ``_parse_unified_response`` had raw-brace BEFORE
    depth-counted, which on nested input like
    ``{"outer": {"inner": [{...}]}}`` returned the innermost
    ``{...}`` instead of the full object — a partial extraction
    bug that silently lost UI_Elements / Reasoning fields.  This
    implementation fixes that by trying depth-counted first.

    Returns ``None`` when nothing parseable was found.
    """
    if not text:
        return None
    m = _CODE_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Depth-counted nested-brace walk — returns OUTER object on success.
    depth = 0
    start_idx = None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start_idx is not None:
                try:
                    return json.loads(text[start_idx:i + 1])
                except json.JSONDecodeError:
                    start_idx = None
    # Last resort: simple raw-brace.  Only reached when depth-counted
    # found no balanced top-level object (e.g. truncated mid-stream).
    m = _RAW_BRACE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ─── parse_vlm_action: single entry point ─────────────────────────────

def parse_vlm_action(
    raw: str,
    *,
    expected_shape: str = 'action_json',
    task: str = '',
    screen_w: Optional[int] = None,
    screen_h: Optional[int] = None,
    detect_action_type: Optional[Callable[[str, str], str]] = None,
    scroll_down_keywords: tuple = (),
    scroll_up_keywords: tuple = (),
) -> ParsedAction:
    """Parse *raw* VLM response into a normalized :class:`ParsedAction`.

    Args:
        raw: VLM response string.
        expected_shape: Which schema to expect.
            ``'action_json'`` — Single-action JSON dict with keys
              ``Next Action``, ``Status``, ``Reasoning``, ``coordinate``,
              ``value``, ``Box ID`` (optional).  Local-loop inline branch
              + parse_and_reason action_json.
            ``'som_bbox'``    — Same as action_json but additionally
              extracts ``UI_Elements`` / ``parsed_content_list`` for
              the SoM-bbox view.
            ``'point_only'``  — Free-text response with
              ``<point>x,y</point>`` markers, ``TYPE:`` prefix,
              ``DONE``, or scroll keywords.  Used by ``point_and_act``
              and the taskbar shortcut.
        task: Original task string (only used by ``'point_only'`` for
              action-type detection).
        screen_w, screen_h: Screen dimensions for norm→screen-px
              scaling on ``'point_only'`` (caller passes
              ``pyautogui.size()``; falls back to no scaling if None).
        detect_action_type: Callable ``(task, raw) -> action_type``
              injected by ``Qwen3VLBackend`` so this parser doesn't
              need to know about the backend's keyword tables.
        scroll_down_keywords, scroll_up_keywords: Tuples of substrings
              the ``'point_only'`` parser checks against task+raw to
              detect scroll intent.
    """
    raw = (raw or '').strip()

    if expected_shape in ('action_json', 'som_bbox'):
        return _parse_json_shape(raw, include_som=(expected_shape == 'som_bbox'))

    if expected_shape == 'point_only':
        return _parse_point_shape(
            raw, task=task,
            screen_w=screen_w, screen_h=screen_h,
            detect_action_type=detect_action_type,
            scroll_down_keywords=scroll_down_keywords,
            scroll_up_keywords=scroll_up_keywords,
        )

    raise ValueError(f"Unknown expected_shape: {expected_shape!r}")


def _parse_json_shape(raw: str, *, include_som: bool) -> ParsedAction:
    """Common JSON-shape parser used by both 'action_json' and
    'som_bbox'.  ``include_som`` adds UI_Elements + parsed_content_list
    population from the parsed dict."""
    pa = ParsedAction(raw=raw)
    parsed = extract_json(raw)
    if parsed is None:
        # Fallback shape — treat as DONE so the loop terminates safely.
        pa.next_action = 'None'
        pa.status = 'DONE'
        pa.done = True
        pa.action = 'none'
        pa.reasoning = raw[:500] or 'Empty / unparseable VLM response'
        if not raw:
            pa.reasoning = 'Empty VLM response'
        return pa

    pa.next_action = parsed.get('Next Action', '') or ''
    pa.status = parsed.get('Status', '') or ''
    pa.reasoning = parsed.get('Reasoning', '') or raw[:500]
    pa.text = parsed.get('value', '') or ''
    pa.coordinate = parsed.get('coordinate')
    pa.box_id = parsed.get('Box ID')
    pa.done = pa.status.upper() == 'DONE'
    # Normalize action: 'left_click' → 'left_click', 'Left Click' → 'left_click'
    pa.action = (pa.next_action or 'none').lower().replace(' ', '_')
    if include_som:
        pa.ui_elements = parsed.get('UI_Elements', []) or []
        pa.parsed_content_list = parsed.get('parsed_content_list', []) or []
    return pa


def _parse_point_shape(
    raw: str, *,
    task: str,
    screen_w: Optional[int],
    screen_h: Optional[int],
    detect_action_type: Optional[Callable[[str, str], str]],
    scroll_down_keywords: tuple,
    scroll_up_keywords: tuple,
) -> ParsedAction:
    """Free-text shape parser — extracts <point>x,y</point>, TYPE:,
    DONE, scroll keywords.  Mirrors the legacy
    ``_parse_action_response`` behaviour byte-for-byte except it
    returns a ParsedAction instead of a 3-tuple (the shim adapts)."""
    pa = ParsedAction(raw=raw)

    # DONE — task complete signal.
    if 'DONE' in raw.upper():
        pa.action = 'done'
        pa.done = True
        pa.reasoning = raw
        return pa

    # TYPE: prefix variant (most reliable when present).
    m = _TYPE_PREFIX_RE.match(raw)
    if m:
        text = m.group(1).strip()
        pa.action = 'type'
        pa.text = text
        pa.reasoning = f'type "{text}"'
        return pa

    # Free-text "type X" variant — only when NO point marker is present
    # (otherwise the point should win).
    if '<point>' not in raw:
        m = _TYPE_FREETEXT_RE.search(raw)
        if m:
            text = m.group(1).strip().strip('"\'')
            pa.action = 'type'
            pa.text = text
            pa.reasoning = f'type "{text}"'
            return pa

    # Scroll keywords (task or raw).
    raw_lower = raw.lower()
    task_lower = task.lower() if task else ''
    if any(kw in task_lower or kw in raw_lower for kw in scroll_down_keywords):
        pa.action = 'scroll_down'
        pa.reasoning = 'scroll down'
        return pa
    if any(kw in task_lower or kw in raw_lower for kw in scroll_up_keywords):
        pa.action = 'scroll_up'
        pa.reasoning = 'scroll up'
        return pa

    # Coordinate extraction — <point> first, then number-pair fallback.
    action_type = (detect_action_type(task, raw)
                   if detect_action_type else 'left_click')
    m = _POINT_RE.search(raw)
    if m:
        nx, ny = int(m.group(1)), int(m.group(2))
        pa.action = action_type
        pa.norm_x = nx
        pa.norm_y = ny
        if screen_w and screen_h:
            pa.screen_x = int(nx * screen_w / 1000)
            pa.screen_y = int(ny * screen_h / 1000)
        pa.reasoning = f'{action_type} at ({nx},{ny}) normalized'
        return pa

    nums = _NUMBER_RE.findall(raw)
    if len(nums) >= 2:
        nx, ny = int(nums[0]), int(nums[1])
        if 0 <= nx <= 1000 and 0 <= ny <= 1000:
            pa.action = action_type
            pa.norm_x = nx
            pa.norm_y = ny
            if screen_w and screen_h:
                pa.screen_x = int(nx * screen_w / 1000)
                pa.screen_y = int(ny * screen_h / 1000)
            pa.reasoning = f'fallback {action_type} ({nx},{ny})'
            return pa

    # Couldn't extract anything actionable.
    logger.warning(f"Could not parse point_only response: {raw[:100]}")
    pa.action = 'none'
    pa.reasoning = raw[:500] or 'unparseable point_only response'
    return pa
