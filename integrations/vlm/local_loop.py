"""
local_loop.py — Synchronous agentic loop for VLM execution.

Equivalent to OmniParser's sampling_loop_sync() (loop.py) but without Twisted.
Orchestrates: screenshot → parse → LLM reason → execute action → repeat.

Uses the same LLM config as create_recipe.py:285-300 (HEVOLVE_NODE_TIER aware).
Produces the same response format as Crossbar: {status, extracted_responses, ...}.
"""

import os
import json
import time
import logging
import re

logger = logging.getLogger('hevolve.vlm.local_loop')

# Max iterations to prevent infinite loops (same safeguard as OmniParser)
MAX_ITERATIONS = 30

# System prompt matching OmniParser vlm_agent.py _get_system_prompt()
SYSTEM_PROMPT = """You are using a Windows device.
You are able to use a mouse and keyboard to interact with the computer based on the given task and screenshot.
You have access to every app running in the device via the mouse and keyboard interfaces mentioned above for GUI actions.

Available actions:
- GUI: left_click, right_click, double_click, type, key, hotkey, hover, mouse_move, wait, scroll_up, scroll_down
- File: list_folders_and_files, open_file_gui, Open_file_and_copy_paste, write_file, read_file_and_understand

IMPORTANT: After the first action, verify if the expected outcome of previous actions is visible on the screen before taking any new action.

Output your response in JSON format:
{
    "Reasoning": "Brief explanation of what you see and why this action is needed",
    "Next Action": "action_name or None if task is complete",
    "Box ID": <element_id if clicking an element>,
    "coordinate": [x, y],
    "value": "text for type/hotkey actions",
    "Status": "IN_PROGRESS or DONE"
}

When the task is complete, set "Next Action": "None" and "Status": "DONE".
"""


def run_local_agentic_loop(
    message: dict,
    tier: str,
    max_iterations: int = MAX_ITERATIONS
) -> dict:
    """
    Local agentic loop: screenshot → parse → LLM reason → execute → repeat.

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
    from integrations.vlm.local_omniparser import parse_screen

    instruction = message.get('instruction_to_vlm_agent', '')
    enhanced = message.get('enhanced_instruction', instruction)
    user_id = message.get('user_id', '')
    prompt_id = message.get('prompt_id', '')
    max_eta = message.get('max_ETA_in_seconds', 1800)

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
            break

        logger.info(f"VLM loop iteration {iteration + 1}/{max_iterations}")

        try:
            # 1. Take screenshot
            screenshot_b64 = take_screenshot(tier)

            # 2. Parse UI elements
            parsed = parse_screen(screenshot_b64, tier)
            screen_info = parsed.get('screen_info', '')

            # 3. Build LLM prompt with current screen state
            user_content = _build_vision_prompt(screen_info, screenshot_b64, iteration)
            messages.append({"role": "user", "content": user_content})

            # 4. Call local LLM for reasoning
            llm_response = _call_local_llm(messages)
            action_json = _parse_vlm_response(llm_response)

            logger.info(f"VLM action: {action_json.get('Next Action', 'None')}")

            # Record the assistant response
            messages.append({"role": "assistant", "content": llm_response})

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
                break

            # 5. Execute the action
            action_payload = _build_action_payload(action_json, parsed)
            result = execute_action(action_payload, tier)

            extracted_responses.append({
                "type": "action",
                "content": {
                    "action": next_action,
                    "reasoning": action_json.get('Reasoning', ''),
                    "result": result.get('output', ''),
                },
                "iteration": iteration + 1,
            })

            # Small delay between iterations (let UI update)
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"VLM loop iteration {iteration + 1} error: {e}")
            extracted_responses.append({
                "type": "error",
                "content": str(e),
                "iteration": iteration + 1,
            })
            # Continue to next iteration rather than aborting
            continue

    execution_time = time.time() - start_time
    logger.info(
        f"VLM loop finished: {len(extracted_responses)} actions in {execution_time:.1f}s"
    )

    return {
        "status": "success",
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

    if node_tier in ('regional', 'central') and os.environ.get('HEVOLVE_LLM_ENDPOINT_URL'):
        base_url = os.environ['HEVOLVE_LLM_ENDPOINT_URL']
        model = os.environ.get('HEVOLVE_LLM_MODEL_NAME', 'gpt-4.1-mini')
        api_key = os.environ.get('HEVOLVE_LLM_API_KEY', 'dummy')
    else:
        llama_port = os.environ.get('LLAMA_CPP_PORT', '8080')
        base_url = f'http://localhost:{llama_port}/v1'
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

    # Pass through extra keys for file operations
    for key in ('path', 'source_path', 'destination_path', 'content', 'duration'):
        if key in action_json:
            payload[key] = action_json[key]

    return payload
