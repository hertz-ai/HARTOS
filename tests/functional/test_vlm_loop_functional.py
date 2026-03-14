"""
Functional tests for VLM computer-use loop CONTROL FLOW.

Exercises the real run_local_agentic_loop() logic and helper functions
with only the boundary mocked (screenshot capture + LLM HTTP call).

Run: pytest tests/functional/test_vlm_loop_functional.py -v --noconftest
"""
import json
import os
import sys
import logging

import pytest

# --noconftest compatibility
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import patch, MagicMock

from integrations.vlm.local_loop import (
    _parse_vlm_response,
    _build_action_payload,
    run_local_agentic_loop,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_SCREENSHOT_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQIHWNgAAIABQABNjN9GQ=="

_DEFAULT_MESSAGE = {
    "instruction_to_vlm_agent": "Click the OK button",
    "enhanced_instruction": "Click the OK button",
    "user_id": "test_user",
    "prompt_id": "test_prompt",
    "os_to_control": "windows",
    "max_ETA_in_seconds": 600,
}


def _vlm_json(status="IN_PROGRESS", action="left_click", reasoning="doing stuff",
               coordinate=None, value=None):
    """Build a VLM-style JSON response string."""
    obj = {
        "Reasoning": reasoning,
        "Next Action": action if status != "DONE" else "None",
        "coordinate": coordinate or [100, 200],
        "Status": status,
    }
    if value:
        obj["value"] = value
    return json.dumps(obj)


def _make_llm_responses(*specs):
    """
    Create a side_effect callable for _call_local_llm.

    Each spec is either a string (returned directly), a dict passed to
    _vlm_json(), or an Exception instance (raised).
    """
    responses = []
    for s in specs:
        if isinstance(s, Exception):
            responses.append(s)
        elif isinstance(s, dict):
            responses.append(_vlm_json(**s))
        else:
            responses.append(s)

    call_idx = {"i": 0}

    def side_effect(messages):
        idx = call_idx["i"]
        call_idx["i"] += 1
        if idx < len(responses):
            item = responses[idx]
        else:
            # Default to DONE if we run out of scripted responses
            item = _vlm_json(status="DONE", reasoning="auto-done fallback")
        if isinstance(item, Exception):
            raise item
        return item

    return side_effect


# Patch targets: take_screenshot and execute_action are imported locally inside
# run_local_agentic_loop, so we patch them at the source module.
# parse_screen is also a local import from local_omniparser.
_PATCH_SCREENSHOT = "integrations.vlm.local_computer_tool.take_screenshot"
_PATCH_EXECUTE = "integrations.vlm.local_computer_tool.execute_action"
_PATCH_PARSE_SCREEN = "integrations.vlm.local_omniparser.parse_screen"
_PATCH_LLM = "integrations.vlm.local_loop._call_local_llm"
_PATCH_SLEEP = "integrations.vlm.local_loop.time.sleep"

_FAKE_PARSED = {"screen_info": "Button[OK] at (100,200)", "parsed_content_list": []}


# ---------------------------------------------------------------------------
# 1. Loop exits on DONE
# ---------------------------------------------------------------------------

class TestLoopExitsOnDone:
    def test_loop_exits_on_done(self):
        """VLM returns DONE on first call -- loop should exit with 1 iteration."""
        llm_side = _make_llm_responses({"status": "DONE", "reasoning": "Task complete"})

        with patch(_PATCH_SCREENSHOT, return_value=FAKE_SCREENSHOT_B64), \
             patch(_PATCH_EXECUTE, return_value={"output": "ok"}), \
             patch(_PATCH_PARSE_SCREEN, return_value=_FAKE_PARSED), \
             patch(_PATCH_SLEEP), \
             patch(_PATCH_LLM, side_effect=llm_side):
            result = run_local_agentic_loop(_DEFAULT_MESSAGE, tier="inprocess", max_iterations=10)

        assert result["status"] == "success"
        responses = result["extracted_responses"]
        assert len(responses) == 1
        assert responses[0]["type"] == "completion"
        assert responses[0]["iteration"] == 1


# ---------------------------------------------------------------------------
# 2. Loop runs 3 iterations (IN_PROGRESS, IN_PROGRESS, DONE)
# ---------------------------------------------------------------------------

class TestLoop3Iterations:
    def test_loop_3_iterations(self):
        """VLM returns IN_PROGRESS twice then DONE -- 3 iterations total."""
        llm_side = _make_llm_responses(
            {"status": "IN_PROGRESS", "action": "left_click", "reasoning": "Step 1"},
            {"status": "IN_PROGRESS", "action": "type", "reasoning": "Step 2", "value": "hello"},
            {"status": "DONE", "reasoning": "Finished"},
        )

        with patch(_PATCH_SCREENSHOT, return_value=FAKE_SCREENSHOT_B64), \
             patch(_PATCH_EXECUTE, return_value={"output": "ok"}), \
             patch(_PATCH_PARSE_SCREEN, return_value=_FAKE_PARSED), \
             patch(_PATCH_SLEEP), \
             patch(_PATCH_LLM, side_effect=llm_side):
            result = run_local_agentic_loop(_DEFAULT_MESSAGE, tier="inprocess", max_iterations=10)

        responses = result["extracted_responses"]
        assert len(responses) == 3
        # First two are actions, last is completion
        assert responses[0]["type"] == "action"
        assert responses[0]["iteration"] == 1
        assert responses[1]["type"] == "action"
        assert responses[1]["iteration"] == 2
        assert responses[2]["type"] == "completion"
        assert responses[2]["iteration"] == 3


# ---------------------------------------------------------------------------
# 3. Max iterations cap
# ---------------------------------------------------------------------------

class TestLoopMaxIterationsCap:
    def test_loop_max_iterations_cap(self):
        """VLM always returns IN_PROGRESS -- loop must stop at max_iterations=5."""
        # All responses are IN_PROGRESS; loop should run exactly 5 times then stop
        always_in_progress = _make_llm_responses(
            *[{"status": "IN_PROGRESS", "action": "left_click", "reasoning": f"iter {i}"}
              for i in range(20)]
        )

        with patch(_PATCH_SCREENSHOT, return_value=FAKE_SCREENSHOT_B64), \
             patch(_PATCH_EXECUTE, return_value={"output": "ok"}), \
             patch(_PATCH_PARSE_SCREEN, return_value=_FAKE_PARSED), \
             patch(_PATCH_SLEEP), \
             patch(_PATCH_LLM, side_effect=always_in_progress):
            result = run_local_agentic_loop(_DEFAULT_MESSAGE, tier="inprocess", max_iterations=5)

        responses = result["extracted_responses"]
        assert len(responses) == 5
        # All should be action type (no completion since DONE was never returned)
        for r in responses:
            assert r["type"] == "action"
        # Verify iteration numbers are 1-5
        assert [r["iteration"] for r in responses] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# 4. Error on iteration 2 -- loop continues
# ---------------------------------------------------------------------------

class TestLoopErrorContinues:
    def test_loop_error_continues(self, caplog):
        """VLM raises on iteration 2, succeeds on 3 -- error logged, loop continues."""
        llm_side = _make_llm_responses(
            {"status": "IN_PROGRESS", "action": "left_click", "reasoning": "Step 1"},
            RuntimeError("LLM endpoint timeout"),
            {"status": "DONE", "reasoning": "Recovered and finished"},
        )

        with patch(_PATCH_SCREENSHOT, return_value=FAKE_SCREENSHOT_B64), \
             patch(_PATCH_EXECUTE, return_value={"output": "ok"}), \
             patch(_PATCH_PARSE_SCREEN, return_value=_FAKE_PARSED), \
             patch(_PATCH_SLEEP), \
             patch(_PATCH_LLM, side_effect=llm_side):
            with caplog.at_level(logging.ERROR, logger="hevolve.vlm.local_loop"):
                result = run_local_agentic_loop(_DEFAULT_MESSAGE, tier="inprocess", max_iterations=10)

        responses = result["extracted_responses"]
        assert len(responses) == 3

        # Iteration 1: successful action
        assert responses[0]["type"] == "action"
        assert responses[0]["iteration"] == 1

        # Iteration 2: error recorded
        assert responses[1]["type"] == "error"
        assert "LLM endpoint timeout" in responses[1]["content"]
        assert responses[1]["iteration"] == 2

        # Iteration 3: recovery + completion
        assert responses[2]["type"] == "completion"
        assert responses[2]["iteration"] == 3

        # Verify error was logged
        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("LLM endpoint timeout" in r.message for r in error_logs)


# ---------------------------------------------------------------------------
# 5. _parse_vlm_response: action JSON extraction from various formats
# ---------------------------------------------------------------------------

class TestActionPayloadParsing:
    """Test _parse_vlm_response extracts action JSON correctly from various formats."""

    def test_plain_json(self):
        raw = json.dumps({
            "Reasoning": "Click the button",
            "Next Action": "left_click",
            "coordinate": [150, 300],
            "Status": "IN_PROGRESS",
        })
        result = _parse_vlm_response(raw)
        assert result["Next Action"] == "left_click"
        assert result["coordinate"] == [150, 300]
        assert result["Status"] == "IN_PROGRESS"

    def test_json_in_markdown_code_block(self):
        raw = """Here is my analysis:

```json
{
    "Reasoning": "I see the OK button at position (200, 400)",
    "Next Action": "left_click",
    "coordinate": [200, 400],
    "Status": "IN_PROGRESS"
}
```

Let me click it now."""
        result = _parse_vlm_response(raw)
        assert result["Next Action"] == "left_click"
        assert result["coordinate"] == [200, 400]

    def test_json_in_bare_code_block(self):
        raw = """```
{"Reasoning": "typing text", "Next Action": "type", "value": "hello world", "Status": "IN_PROGRESS"}
```"""
        result = _parse_vlm_response(raw)
        assert result["Next Action"] == "type"
        assert result["value"] == "hello world"

    def test_json_surrounded_by_text(self):
        raw = 'I will click the button. {"Reasoning": "found it", "Next Action": "left_click", "coordinate": [50, 60], "Status": "IN_PROGRESS"} That should work.'
        result = _parse_vlm_response(raw)
        assert result["Next Action"] == "left_click"
        assert result["coordinate"] == [50, 60]

    def test_unparseable_response_returns_done(self):
        """Completely unparseable text should fall back to DONE."""
        raw = "I cannot determine what to do next."
        result = _parse_vlm_response(raw)
        assert result["Status"] == "DONE"
        assert result["Next Action"] == "None"

    def test_done_status_parsed(self):
        raw = json.dumps({
            "Reasoning": "Task is complete",
            "Next Action": "None",
            "Status": "DONE",
        })
        result = _parse_vlm_response(raw)
        assert result["Status"] == "DONE"
        assert result["Next Action"] == "None"


# ---------------------------------------------------------------------------
# 6. Coordinate normalization: bbox center calculation in _build_action_payload
# ---------------------------------------------------------------------------

class TestCoordinateNormalization:
    """Test bbox coordinate conversion to pixel values via _build_action_payload."""

    def test_box_id_resolves_to_center(self):
        """Box ID should resolve to the center pixel of the bounding box."""
        action_json = {
            "Next Action": "left_click",
            "Box ID": 3,
            "Status": "IN_PROGRESS",
        }
        parsed_screen = {
            "parsed_content_list": [
                {"idx": 1, "bbox": [0, 0, 100, 100]},
                {"idx": 2, "bbox": [200, 200, 400, 400]},
                {"idx": 3, "bbox": [100, 200, 300, 400]},  # center = (200, 300)
            ],
        }
        payload = _build_action_payload(action_json, parsed_screen)
        assert payload["action"] == "left_click"
        assert payload["coordinate"] == [200, 300]

    def test_explicit_coordinate_overrides_box_id(self):
        """If coordinate is explicitly set, Box ID is ignored."""
        action_json = {
            "Next Action": "left_click",
            "Box ID": 1,
            "coordinate": [42, 84],
            "Status": "IN_PROGRESS",
        }
        parsed_screen = {
            "parsed_content_list": [
                {"idx": 1, "bbox": [0, 0, 100, 100]},  # center = (50, 50) -- should NOT be used
            ],
        }
        payload = _build_action_payload(action_json, parsed_screen)
        assert payload["coordinate"] == [42, 84]

    def test_box_id_with_id_key(self):
        """Some parsers use 'id' instead of 'idx' -- should still resolve."""
        action_json = {
            "Next Action": "right_click",
            "Box ID": 7,
            "Status": "IN_PROGRESS",
        }
        parsed_screen = {
            "parsed_content_list": [
                {"id": 7, "bbox": [50, 100, 150, 200]},  # center = (100, 150)
            ],
        }
        payload = _build_action_payload(action_json, parsed_screen)
        assert payload["coordinate"] == [100, 150]

    def test_missing_box_id_no_coordinate(self):
        """No coordinate and missing Box ID -- payload has no coordinate key."""
        action_json = {
            "Next Action": "key",
            "value": "enter",
            "Status": "IN_PROGRESS",
        }
        parsed_screen = {"parsed_content_list": []}
        payload = _build_action_payload(action_json, parsed_screen)
        assert payload["action"] == "key"
        assert payload["text"] == "enter"
        assert "coordinate" not in payload

    def test_bbox_integer_truncation(self):
        """Bounding box center should use int() truncation for odd-sized boxes."""
        action_json = {
            "Next Action": "left_click",
            "Box ID": 1,
            "Status": "IN_PROGRESS",
        }
        parsed_screen = {
            "parsed_content_list": [
                {"idx": 1, "bbox": [11, 21, 30, 40]},  # center = int(20.5)=20, int(30.5)=30
            ],
        }
        payload = _build_action_payload(action_json, parsed_screen)
        assert payload["coordinate"] == [20, 30]

    def test_extra_keys_passed_through(self):
        """File operation keys (path, content, etc.) are forwarded to payload."""
        action_json = {
            "Next Action": "write_file",
            "path": "/tmp/test.txt",
            "content": "hello world",
            "Status": "IN_PROGRESS",
        }
        parsed_screen = {"parsed_content_list": []}
        payload = _build_action_payload(action_json, parsed_screen)
        assert payload["action"] == "write_file"
        assert payload["path"] == "/tmp/test.txt"
        assert payload["content"] == "hello world"

    def test_unresolvable_box_id(self):
        """Box ID not found in parsed list -- no coordinate in payload."""
        action_json = {
            "Next Action": "left_click",
            "Box ID": 999,
            "Status": "IN_PROGRESS",
        }
        parsed_screen = {
            "parsed_content_list": [
                {"idx": 1, "bbox": [0, 0, 100, 100]},
            ],
        }
        payload = _build_action_payload(action_json, parsed_screen)
        assert "coordinate" not in payload
