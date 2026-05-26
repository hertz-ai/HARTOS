"""
local_loop.py - Synchronous agentic loop for VLM execution.

Equivalent to OmniParser's sampling_loop_sync() (loop.py) but without Twisted.
Orchestrates: screenshot → parse → LLM reason → execute action → repeat.

Uses the same LLM config as create_recipe.py:285-300 (HEVOLVE_NODE_TIER aware).
Produces the same response format as Crossbar: {status, extracted_responses, ...}.
"""

import os
import json
import platform
import time
import logging
import re

logger = logging.getLogger('hevolve.vlm.local_loop')

# Max iterations to prevent infinite loops (same safeguard as OmniParser)
MAX_ITERATIONS = 30

# Action list — single source of truth for both the legacy SYSTEM_PROMPT
# and the unified-mode combined_prompt. Keeping one string means the
# legacy OmniParser path and the unified Qwen3-VL path can never drift
# on which actions the model is allowed to emit.
_VLM_ACTION_LIST = (
    "Available actions:\n"
    "- GUI: left_click, right_click, double_click, type, key, hotkey, hover, "
    "mouse_move, wait, scroll_up, scroll_down\n"
    "- Deterministic (PREFER these when the task is expressible as a "
    "command — they're 100x faster than GUI grounding):\n"
    "    * shell: run any shell/PowerShell/bash command. Use for launching "
    "apps (command='notepad'), opening files in specific apps "
    "(command='notepad hello.txt'), running git/npm/python, file ops, etc. "
    "Put the full command in the 'command' field.\n"
    "    * open_file_gui: open a file or app in the OS default handler. "
    "Put the target in the 'path' field (e.g. path='notepad' or "
    "path='C:\\\\Users\\\\foo\\\\doc.pdf').\n"
    "- File: list_folders_and_files, Open_file_and_copy_paste, write_file, "
    "read_file_and_understand\n"
)

# System prompt matching OmniParser vlm_agent.py _get_system_prompt()
_os_name = platform.system()  # 'Windows', 'Linux', 'Darwin', etc.
SYSTEM_PROMPT = (
    "You are using a " + _os_name + " device.\n"
    "You are able to use a mouse and keyboard to interact with the computer "
    "based on the given task and screenshot.\n"
    "You have access to every app running in the device via the mouse and "
    "keyboard interfaces mentioned above for GUI actions.\n"
    "\n"
    + _VLM_ACTION_LIST +
    "\n"
    "IMPORTANT: Prefer deterministic actions (shell, open_file_gui) over "
    "clicking when the task is expressible as a command. Only fall back to "
    "clicks for things that MUST be done visually (e.g. clicking a specific "
    "button inside an already-running app's UI that has no keyboard "
    "shortcut). After the first action, verify the expected outcome on screen "
    "before taking any new action.\n"
    "\n"
    "Output your response in JSON format:\n"
    '{\n'
    '    "Reasoning": "Brief explanation of what you see and why this action is needed",\n'
    '    "Next Action": "action_name or None if task is complete",\n'
    '    "Box ID": <element_id if clicking an element>,\n'
    '    "coordinate": [x, y],\n'
    '    "value": "text for type/hotkey actions",\n'
    '    "command": "shell command string when Next Action is shell",\n'
    '    "path": "file or app name when Next Action is open_file_gui",\n'
    '    "Status": "IN_PROGRESS or DONE"\n'
    '}\n'
    "\n"
    'When the task is complete, set "Next Action": "None" and "Status": "DONE".\n'
)


def run_local_agentic_loop(
    message: dict,
    tier: str,
    max_iterations: int = MAX_ITERATIONS
) -> dict:
    """
    Local agentic loop: screenshot → parse → LLM reason → execute → repeat.

    Supports two modes:
        - Legacy (default): OmniParser screen parsing + separate LLM reasoning call
        - Unified (HEVOLVE_VLM_UNIFIED=true): Single Qwen3-VL call for parsing + reasoning

    Args:
        message: dict with keys from execute_windows_or_android_command:
            - instruction_to_vlm_agent: str
            - enhanced_instruction: str (optional, from recipe matching)
            - user_id: str
            - prompt_id: str
            - os_to_control: str
            - max_ETA_in_seconds: int
        tier: 'inprocess' or 'http'
    Returns:
        dict matching Crossbar response format:
        {status, extracted_responses, execution_time_seconds}
    """
    from integrations.vlm.local_computer_tool import take_screenshot, execute_action

    instruction = message.get('instruction_to_vlm_agent', '')
    enhanced = message.get('enhanced_instruction', instruction)
    user_id = message.get('user_id', '')
    prompt_id = message.get('prompt_id', '')
    max_eta = message.get('max_ETA_in_seconds', 1800)

    # exit_reason is overwritten as the loop progresses. Defaults to max_iterations
    # so a loop that runs to the iteration cap without a DONE signal is honest
    # about it to the caller (instead of pretending status='success').
    exit_reason = 'max_iterations'
    consecutive_action_errors = 0

    # Detect unified Qwen3-VL mode
    use_unified = os.environ.get('HEVOLVE_VLM_UNIFIED', '').lower() in ('1', 'true')

    if use_unified:
        from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend
        qwen3vl = get_qwen3vl_backend()
        logger.info(
            f"Starting unified VLM loop (Qwen3-VL, tier={tier}, user={user_id}, "
            f"prompt={prompt_id}): {instruction[:100]}"
        )
    else:
        from integrations.vlm.local_omniparser import parse_screen
        qwen3vl = None
        logger.info(
            f"Starting local VLM loop (tier={tier}, user={user_id}, "
            f"prompt={prompt_id}): {instruction[:100]}"
        )

    # Build conversation messages for LLM
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": enhanced},
    ]

    extracted_responses = []
    start_time = time.time()

    for iteration in range(max_iterations):
        elapsed = time.time() - start_time
        if elapsed > max_eta:
            logger.warning(f"VLM loop hit ETA limit ({max_eta}s) at iteration {iteration}")
            exit_reason = 'timeout'
            break

        logger.info(f"VLM loop iteration {iteration + 1}/{max_iterations}")

        try:
            # 1. Take screenshot
            screenshot_b64 = take_screenshot(tier)

            if use_unified and qwen3vl is not None:
                # ── Single VLM call: plan step + ground coordinates in one prompt ──
                # One image encoding (~500 visual tokens) instead of two.
                # Halves latency: ~10s per step instead of ~20s.
                from integrations.vlm.local_computer_tool import VLM_IMG_W, VLM_IMG_H

                combined_prompt = (
                    f"You are a computer use agent on {_os_name}.\n"
                    f"Task: {enhanced}\n\n"
                )
                if extracted_responses:
                    last = extracted_responses[-1].get('content', '')
                    if isinstance(last, dict):
                        combined_prompt += (
                            f"Previous action: {last.get('action', '?')} — "
                            f"{last.get('reasoning', '')[:80]}.\n"
                            f"Check the screenshot: did it succeed?\n\n"
                        )
                combined_prompt += (
                    _VLM_ACTION_LIST +
                    "\n"
                    "What is the SINGLE next action? Respond in JSON ONLY:\n"
                    "{\n"
                    '  "Reasoning": "What you see and why this action",\n'
                    '  "Next Action": "left_click|right_click|double_click|'
                    'type|key|hotkey|scroll_up|scroll_down|wait|shell|'
                    'open_file_gui|None",\n'
                    '  "coordinate": [x, y],\n'
                    '  "value": "text to type or key name",\n'
                    '  "command": "shell command when Next Action is shell",\n'
                    '  "path": "file or app name when Next Action is open_file_gui",\n'
                    '  "Status": "IN_PROGRESS|DONE"\n'
                    "}\n\n"
                    "For click actions: provide <point>x,y</point> normalized "
                    "0-1000 coordinates.\n"
                    "For type/key/hotkey: set coordinate to null, put text in value.\n"
                    "Only fall back to clicks when the task requires interacting "
                    "with something already visible on screen that cannot be "
                    "done via a command.\n"
                    'When task is complete: "Next Action": "None", "Status": "DONE".'
                )

                raw = qwen3vl._call_api([{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": combined_prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{screenshot_b64}"}},
                    ]
                }])
                # Guard against None (e.g. thinking-only response with no content)
                if raw is None:
                    raw = ''
                action_json = _parse_vlm_response(raw)

                # Extract coordinates from <point>x,y</point> if present in raw
                next_action = action_json.get('Next Action', 'None')
                _CLICK_ACTIONS = {'left_click', 'right_click', 'double_click',
                                  'middle_click', 'hover', 'mouse_move'}

                if next_action in _CLICK_ACTIONS:
                    coord = action_json.get('coordinate')
                    # Try parsing <point> tags from raw response
                    import re as _re
                    point_match = _re.search(r'<point>\s*(\d+)\s*,\s*(\d+)\s*</point>', raw or '')
                    if point_match:
                        nx, ny = int(point_match.group(1)), int(point_match.group(2))
                    elif coord and isinstance(coord, list) and len(coord) == 2:
                        nx, ny = coord[0], coord[1]
                    else:
                        nx, ny = 500, 500  # center fallback

                    # Scale from 1000-normalized or image space to screen space
                    try:
                        import pyautogui as _pag
                        _sw, _sh = _pag.size()
                        if nx <= 1000 and ny <= 1000:
                            # Normalized 0-1000 coords
                            screen_x = int(nx * _sw / 1000)
                            screen_y = int(ny * _sh / 1000)
                        else:
                            # Image pixel coords
                            screen_x = int(nx * _sw / VLM_IMG_W)
                            screen_y = int(ny * _sh / VLM_IMG_H)
                    except Exception:
                        screen_x, screen_y = nx, ny
                    action_json['coordinate'] = [screen_x, screen_y]
                    logger.info(f"Action: {next_action} at ({screen_x},{screen_y}) "
                                f"norm=({nx},{ny})")
                    # Sanity check: flag clicks in the likely taskbar region.
                    # If the VLM's reasoning talks about a Start menu item or
                    # app window but the coordinate lands in the bottom 50px,
                    # the grounding probably drifted onto the taskbar strip.
                    # We log a warning and let the verify step catch it; the
                    # router will see exit_reason=action_error if this pattern
                    # keeps happening, so it can respond honestly.
                    try:
                        import pyautogui as _pag2
                        _sw2, _sh2 = _pag2.size()
                        reasoning_lc = (action_json.get('Reasoning') or '').lower()
                        if (screen_y >= _sh2 - 50
                                and any(t in reasoning_lc for t in
                                        ('start menu', 'menu item', 'recommended', 'pinned'))):
                            logger.warning(
                                f"VLM click ({screen_x},{screen_y}) is in taskbar "
                                f"region (screen height={_sh2}), but reasoning "
                                f"mentions Start menu — probable grounding drift"
                            )
                    except Exception:
                        pass
                else:
                    action_json['coordinate'] = None
                    logger.info(f"Action: {next_action} "
                                f"value='{action_json.get('value', '')[:50]}'")

                parsed = {'screen_info': '', 'parsed_content_list': []}
            else:
                # ── Legacy path: OmniParser + separate LLM call ──
                # 2. Parse UI elements
                parsed = parse_screen(screenshot_b64, tier)
                screen_info = parsed.get('screen_info', '')

                # 3. Build LLM prompt with current screen state
                user_content = _build_vision_prompt(screen_info, screenshot_b64, iteration)
                messages.append({"role": "user", "content": user_content})

                # 4. Call local LLM for reasoning
                llm_response = _call_local_llm(messages)
                action_json = _parse_vlm_response(llm_response)

                # Record the assistant response
                messages.append({"role": "assistant", "content": llm_response})

            logger.info(f"VLM action: {action_json.get('Next Action', 'None')}")

            # Check if task is complete
            next_action = action_json.get('Next Action', 'None')
            status = action_json.get('Status', 'IN_PROGRESS')

            if next_action == 'None' or next_action is None or status == 'DONE':
                logger.info("VLM task completed")
                extracted_responses.append({
                    "type": "completion",
                    "content": action_json.get('Reasoning', 'Task completed'),
                    "iteration": iteration + 1,
                })
                exit_reason = 'done'
                break

            # 5. Execute the action
            action_payload = _build_action_payload(action_json, parsed)
            result = execute_action(action_payload, tier)
            action_ok = result.get('status') != 'error'
            if action_ok:
                consecutive_action_errors = 0
            else:
                consecutive_action_errors += 1

            extracted_responses.append({
                "type": "action",
                "content": {
                    "action": next_action,
                    "reasoning": action_json.get('Reasoning', ''),
                    "result": result.get('output', ''),
                    "ok": action_ok,
                },
                "iteration": iteration + 1,
            })

            # Bail after 3 consecutive action errors — something is structurally
            # broken (bad coordinates, action type mismatch, subprocess dead)
            # and more iterations won't help.
            if consecutive_action_errors >= 3:
                logger.warning("VLM loop: 3 consecutive action errors, aborting")
                exit_reason = 'action_error'
                break

            # Small delay between iterations (let UI update)
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"VLM loop iteration {iteration + 1} error: {e}")
            extracted_responses.append({
                "type": "error",
                "content": str(e),
                "iteration": iteration + 1,
            })
            consecutive_action_errors += 1
            if consecutive_action_errors >= 3:
                logger.warning("VLM loop: 3 consecutive iteration errors, aborting")
                exit_reason = 'action_error'
                break
            # Continue to next iteration rather than aborting
            continue

    execution_time = time.time() - start_time
    logger.info(
        f"VLM loop finished: {len(extracted_responses)} actions in "
        f"{execution_time:.1f}s (exit_reason={exit_reason})"
    )

    # status mirrors exit_reason: only 'done' is a real success. Callers
    # (LangChain router, autogen) can inspect exit_reason to craft an honest
    # response instead of confidently lying when the loop timed out.
    return {
        "status": "success" if exit_reason == 'done' else "incomplete",
        "exit_reason": exit_reason,
        "extracted_responses": extracted_responses,
        "execution_time_seconds": execution_time,
    }


def _build_vision_prompt(screen_info: str, screenshot_b64: str, iteration: int) -> list:
    """Build multimodal prompt with screen info + screenshot image."""
    content = []

    if iteration == 0:
        content.append({
            "type": "text",
            "text": (
                "Here is the current screen state. "
                "Analyze the UI elements and decide the next action.\n\n"
                f"UI Elements:\n{screen_info}"
            ),
        })
    else:
        content.append({
            "type": "text",
            "text": (
                "Here is the updated screen after the previous action. "
                "Verify the previous action succeeded, then decide the next action.\n\n"
                f"UI Elements:\n{screen_info}"
            ),
        })

    # Add screenshot as image
    content.append({
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
    })

    return content


def _call_local_llm(messages: list) -> str:
    """
    Call local LLM using the same config as create_recipe.py:285-300.

    Uses OpenAI-compatible API (llama.cpp / Qwen3-VL / cloud endpoint).
    """
    import requests as _req

    node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')

    # VLM-specific override takes priority, then global AutoGen LLM config,
    # then node-tier aware defaults (same model the user configured)
    if os.environ.get('HEVOLVE_VLM_ENDPOINT_URL'):
        base_url = os.environ['HEVOLVE_VLM_ENDPOINT_URL']
        model = os.environ.get('HEVOLVE_VLM_MODEL_NAME',
                               os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini'))
        api_key = os.environ.get('HEVOLVE_VLM_API_KEY',
                                 os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy'))
    elif os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
        # Use the same LLM config as AutoGen (user's configured model)
        base_url = os.environ['HEVOLVE_LLM_ENDPOINT_URL']
        model = os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini')
        api_key = os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy')
    elif os.environ.get('OPENAI_API_KEY'):
        # Fall back to OpenAI API if configured (common for standalone)
        base_url = 'https://api.openai.com/v1'
        model = os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini')
        api_key = os.environ['OPENAI_API_KEY']
    else:
        # Last resort: local llama.cpp / Qwen3-VL
        from core.port_registry import get_local_llm_url
        base_url = get_local_llm_url()
        model = 'Qwen3-VL-4B-Instruct'
        api_key = 'dummy'

    try:
        resp = _req.post(
            f'{base_url.rstrip("/")}/chat/completions',
            json={
                'model': model,
                'messages': messages,
                'max_tokens': 4096,
                'temperature': 0.0,
            },
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"Local LLM call failed: {e}")
        raise


def _parse_vlm_response(response_text: str) -> dict:
    """
    Parse VLM JSON response, handling markdown code blocks and partial JSON.

    Matches OmniParser vlm_agent.py extract_data() pattern.
    """
    if not response_text:
        return {"Next Action": "None", "Status": "DONE", "Reasoning": "Empty VLM response"}
    # Try to extract JSON from code blocks first
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object
    brace_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response_text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    # Fallback: treat as completed if no parseable JSON
    logger.warning(f"Could not parse VLM response as JSON: {response_text[:200]}")
    return {
        "Next Action": "None",
        "Status": "DONE",
        "Reasoning": response_text[:500],
    }


def _build_action_payload(action_json: dict, parsed_screen: dict) -> dict:
    """
    Convert VLM response JSON into action payload for local_computer_tool.

    Resolves Box ID → coordinate using parsed_screen bounding boxes.
    """
    next_action = action_json.get('Next Action', '')
    coordinate = action_json.get('coordinate')
    text = action_json.get('value', '')
    box_id = action_json.get('Box ID')

    # Resolve Box ID to coordinate if no explicit coordinate given
    if coordinate is None and box_id is not None:
        parsed_list = parsed_screen.get('parsed_content_list', [])
        for item in parsed_list:
            if item.get('idx') == box_id or item.get('id') == box_id:
                bbox = item.get('bbox', [])
                if len(bbox) == 4:
                    # Center of bounding box
                    coordinate = [
                        int((bbox[0] + bbox[2]) / 2),
                        int((bbox[1] + bbox[3]) / 2),
                    ]
                break

    payload = {'action': next_action}
    if coordinate:
        payload['coordinate'] = coordinate
    if text:
        payload['text'] = text

    # Pass through extra keys for file/shell operations. 'command' is for
    # the 'shell' action and 'path' covers 'open_file_gui' — both already
    # live in SUPPORTED_ACTIONS so _execute_inprocess handles them natively.
    for key in ('path', 'source_path', 'destination_path', 'content',
                'duration', 'command'):
        if key in action_json:
            payload[key] = action_json[key]

    return payload
