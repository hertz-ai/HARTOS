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


# ─── Stop registry — port of OmniParser agentic_rpc.app_state["active_sessions"] ───
# When the VLM is mid-loop on the user's screen and the user clicks
# the indicator window's Stop button, Nunba POSTs to /api/vlm/stop on
# HARTOS.  That handler calls request_stop() below, which sets the
# user's threading.Event.  The next iteration of run_local_agentic_loop
# checks the event via _is_stop_requested() and exits cleanly with
# exit_reason='stopped' instead of running another action on the user's
# screen.
#
# Why threading.Event: pyautogui actions inside an iteration are
# already synchronous on the loop's thread, so we can't preempt mid-
# action.  But every action has natural seams (between iterations and
# after each pyautogui call), and Event.is_set() is a cheap atomic
# check we can sprinkle there without locking.
#
# Why per-(user_id, prompt_id) key: same instance can have multiple
# concurrent VLM sessions if more than one user is connected.  Stop
# fires on a specific session, not globally, mirroring OmniParser's
# active_sessions dict shape.
import threading as _threading

_vlm_stop_flags: dict = {}              # f"{user_id}:{prompt_id}" -> Event
_vlm_stop_lock = _threading.Lock()


def _stop_key(user_id: str, prompt_id: str) -> str:
    return f"{user_id}:{prompt_id}"


def _register_session(user_id: str, prompt_id: str) -> _threading.Event:
    """Called by run_local_agentic_loop on entry — creates the Event so
    a /api/vlm/stop POST can later flip it."""
    key = _stop_key(user_id, prompt_id)
    with _vlm_stop_lock:
        ev = _vlm_stop_flags.get(key)
        if ev is None:
            ev = _threading.Event()
            _vlm_stop_flags[key] = ev
        else:
            # Existing flag from a prior session — clear it so this run
            # starts un-stopped.  Preserves the singleton-Event pattern
            # without leaking state across runs.
            ev.clear()
    return ev


def _unregister_session(user_id: str, prompt_id: str) -> None:
    """Called by run_local_agentic_loop on exit (success or stop) —
    drops the Event so the dict doesn't grow unbounded."""
    key = _stop_key(user_id, prompt_id)
    with _vlm_stop_lock:
        _vlm_stop_flags.pop(key, None)


def _is_stop_requested(user_id: str, prompt_id: str) -> bool:
    """Cheap check called at iteration boundaries inside the loop."""
    key = _stop_key(user_id, prompt_id)
    with _vlm_stop_lock:
        ev = _vlm_stop_flags.get(key)
    return bool(ev and ev.is_set())


def request_stop(user_id: str, prompt_id: str) -> bool:
    """Public API — called by /api/vlm/stop in hart_intelligence_entry.py.

    Sets the stop flag on a registered session.  Returns True when a
    matching session was found, False when the user has no active VLM
    loop (caller logs accordingly so the UI can distinguish "stopped"
    from "nothing to stop").

    Pairs with the loop's iteration-boundary check at the top of every
    iteration.  Stop becomes visible to the loop on its NEXT iteration
    — typically within 1-3 seconds depending on which step is in
    flight (screenshot, LLM call, action execution).
    """
    key = _stop_key(user_id, prompt_id)
    with _vlm_stop_lock:
        ev = _vlm_stop_flags.get(key)
        if ev is None:
            return False
        ev.set()
    return True


def list_active_sessions() -> list:
    """Return [(user_id, prompt_id), ...] of currently-running VLM
    loops.  Used by /api/vlm/stop with no payload to bulk-stop, and by
    diagnostics."""
    with _vlm_stop_lock:
        return [tuple(k.split(':', 1)) for k in _vlm_stop_flags.keys()]


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

    # Phase 3.5 wire-up: classify the task with the complementary path
    # router and use it to size the iteration budget.  Single-shot
    # tasks ("click X") shouldn't burn the full 30-iter budget when
    # one click satisfies the goal — the multi-iter loop's overhead
    # is real (per-iter screenshot + VLM call ~3-5s).  Multi-step
    # tasks get the full caller-supplied max_iterations.
    _route = 'multi_step'  # safe default — never over-cap a real loop
    try:
        if qwen3vl is not None:
            _route = qwen3vl.route_task(instruction or enhanced)
            logger.info(f"VLM loop route_task: '{instruction[:60]}' → {_route}")
            if _route == 'single_shot' and max_iterations > 3:
                # Cap at 3 — gives one nudge-retry + one followup
                # if the click misses without burning the full budget.
                max_iterations = 3
            elif _route == 'enumerate' and max_iterations > 1:
                # Enumerate = parse_and_reason snapshot, no follow-up
                # iter needed.
                max_iterations = 1
    except Exception as e:
        logger.debug(f'route_task wire-up skipped: {e}')

    # Build conversation messages for LLM
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": enhanced},
    ]

    extracted_responses = []
    start_time = time.time()

    # Register this session in the stop registry so /api/vlm/stop can
    # signal it.  Cleanup happens just before the final return below
    # (no try/finally — the existing iteration body wraps every error
    # in its own try/continue so exceptions never escape this scope).
    _register_session(user_id, prompt_id)

    for iteration in range(max_iterations):
        # User-requested stop wins over every other exit condition.
        # Check FIRST so a stop fired during the previous iteration's
        # action lands at this seam without one more click happening.
        if _is_stop_requested(user_id, prompt_id):
            logger.info(
                f"VLM loop stopped by /api/vlm/stop at iteration "
                f"{iteration + 1} (user={user_id}, prompt={prompt_id})"
            )
            exit_reason = 'stopped'
            break

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

                # Taskbar pre-check (additive — restores point_and_act's
                # smart strategy that 8fa6e97 dropped when this loop
                # adopted its own inline prompt).  When the task targets
                # a taskbar item ("open Chrome", "click Start", etc.),
                # _taskbar_list_lookup short-circuits the VLM call
                # entirely and returns a click coord direct from the
                # taskbar enumeration — typically <1s vs the 5-10s a
                # full VLM grounding takes.  On miss, returns None and
                # the existing inline prompt path runs unchanged.
                _step_started = time.time()
                try:
                    import pyautogui as _pag_pre
                    _sw_pre, _sh_pre = _pag_pre.size()
                except Exception:
                    _sw_pre = _sh_pre = None
                _taskbar_action = None
                if _sw_pre and _sh_pre:
                    try:
                        _taskbar_action = qwen3vl.try_taskbar_pre_check(
                            screenshot_b64, enhanced,
                            _sw_pre, _sh_pre, _step_started,
                        )
                    except Exception as _tb_err:
                        logger.debug(
                            f"taskbar_pre_check failed (non-fatal): {_tb_err}")
                if _taskbar_action is not None:
                    # Single source of truth for "point_and_act result
                    # -> action_json shape" conversion.  Was inline
                    # 14 lines duplicating the dict construction.
                    action_json = _point_action_to_action_json(_taskbar_action)
                    raw = _taskbar_action.get('raw', '')
                    logger.info(
                        f"Loop: taskbar_list shortcut → "
                        f"({_taskbar_action.get('screen_x')},"
                        f"{_taskbar_action.get('screen_y')})"
                    )
                    # Fall through to the existing post-action handling
                    # below (which executes action_json + records it).
                    # Skip the combined_prompt + _call_api block.
                    _skip_combined_prompt = True
                else:
                    _skip_combined_prompt = False

                # Skip the heavy combined-prompt VLM call entirely when
                # taskbar_pre_check above already produced a click —
                # the taskbar lookup is the authoritative grounding for
                # taskbar tasks (point_and_act has used the same
                # short-circuit since cb92a2e).  Without this guard the
                # _call_api below would overwrite action_json with a
                # less-grounded result.
                if not _skip_combined_prompt:
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
                    # Phase 5 follow-through: was a 4th inline <point>
                    # regex parser duplicating parser._parse_point_shape.
                    # Now delegates to the canonical parser so the
                    # action_json JSON-coordinate vs the raw <point>
                    # tag agree (they previously could disagree when
                    # the JSON had Box ID + the raw text had a point).
                    nx, ny = _extract_click_coord(raw, action_json)

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
                    except Exception as _scale_err:
                        logger.debug(f"coord scale to screen failed: {_scale_err}")
                        screen_x, screen_y = nx, ny
                    action_json['coordinate'] = [screen_x, screen_y]
                    logger.info(f"Action: {next_action} at ({screen_x},{screen_y}) "
                                f"norm=({nx},{ny})")

                    # Bias-detection + elimination retry — additive
                    # restoration of point_and_act's strategy 3 that
                    # 8fa6e97 dropped when this loop adopted its own
                    # inline prompt.  Catches center/bottom/top-edge
                    # hallucinations in the 0-1000 normalized coords
                    # the loop just produced and reissues the VLM with
                    # an elimination prompt that explicitly forbids
                    # the suspect region.  Skipped when the action
                    # came from taskbar_pre_check (its coords are
                    # already lookup-grounded, no need to retry).
                    # All wrapped in try/except so a retry-time error
                    # NEVER takes down the iteration — original coords
                    # remain in action_json.
                    if action_json.get('_strategy') != 'taskbar_list':
                        try:
                            _bias = qwen3vl.detect_grounding_bias(
                                nx, ny, 'left_click', enhanced,
                            )
                            if _bias:
                                _retry = qwen3vl.retry_with_elimination(
                                    screenshot_b64, enhanced,
                                    VLM_IMG_W, VLM_IMG_H, _bias,
                                )
                                if _retry is not None:
                                    _r_dict, _enx, _eny = _retry
                                    nx, ny = _enx, _eny
                                    # Re-scale retry coords to screen
                                    # space using the same rule as the
                                    # original (0-1000 vs image-pixel).
                                    try:
                                        import pyautogui as _pag_r
                                        _swr, _shr = _pag_r.size()
                                        if nx <= 1000 and ny <= 1000:
                                            screen_x = int(nx * _swr / 1000)
                                            screen_y = int(ny * _shr / 1000)
                                        else:
                                            screen_x = int(nx * _swr / VLM_IMG_W)
                                            screen_y = int(ny * _shr / VLM_IMG_H)
                                    except Exception:
                                        screen_x, screen_y = nx, ny
                                    action_json['coordinate'] = [
                                        screen_x, screen_y,
                                    ]
                                    action_json['_strategy'] = (
                                        'elimination_retry'
                                    )
                                    logger.info(
                                        f"Loop bias retry ({_bias}) → "
                                        f"({screen_x},{screen_y}) "
                                        f"norm=({nx},{ny})"
                                    )
                        except Exception as _bias_err:
                            logger.debug(
                                f"bias retry failed (non-fatal, "
                                f"keeping original coords): {_bias_err}"
                            )
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

            # 5. Execute the action.
            # Phase 6 wire-up: pass safety=True so the per-session cap
            # + window blocklist + audit JSONL fire on every loop click.
            # Verify=True triggers the post-click pre/post diff + 50px
            # nudge retry from Phase 4.  Both default-tunable via env
            # but ON in the loop is the right safe default — solo
            # /visual_agent calls keep their existing behaviour.
            action_payload = _build_action_payload(action_json, parsed)
            _safety_on = os.environ.get(
                'HEVOLVE_VLM_LOOP_SAFETY', '1').lower() not in ('0', 'false', 'no')
            _verify_on = os.environ.get(
                'HEVOLVE_VLM_LOOP_VERIFY', '0').lower() in ('1', 'true', 'yes')
            result = execute_action(
                action_payload, tier,
                safety=_safety_on, verify=_verify_on)
            action_ok = result.get('status') != 'error'
            if action_ok:
                consecutive_action_errors = 0
            else:
                consecutive_action_errors += 1

            # Surface coordinate + strategy in the response content so
            # observers (benchmark, audit, /visual_agent telemetry,
            # post-hoc replay) can reconstruct what the VLM actually
            # decided this iteration without re-parsing action_json.
            # Was missing - vlm_grounding_benchmark.py:loop_one_iter
            # path always read content['coordinate'] = None and scored
            # all 6 targets as FAIL, hiding any real grounding regression
            # behind a fixed metric.
            extracted_responses.append({
                "type": "action",
                "content": {
                    "action": next_action,
                    "reasoning": action_json.get('Reasoning', ''),
                    "result": result.get('output', ''),
                    "ok": action_ok,
                    "coordinate": action_json.get('coordinate'),
                    "_strategy": action_json.get('_strategy', 'inline_prompt'),
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

    # Drop this session's stop flag so the registry doesn't grow
    # across runs.  Pairs with _register_session above.
    _unregister_session(user_id, prompt_id)

    # status mirrors exit_reason: only 'done' is a real success. Callers
    # (LangChain router, autogen) can inspect exit_reason to craft an honest
    # response instead of confidently lying when the loop timed out.
    # 'stopped' is its own honest exit_reason — Nunba's indicator UX
    # reads it to render the right "Stopped" badge instead of a
    # generic "incomplete".
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


def _point_action_to_action_json(point_action: dict) -> dict:
    """Convert a point_and_act-shaped result (from
    Qwen3VLBackend.try_taskbar_pre_check / point_and_act / retry_with_
    elimination) into the action_json shape the loop's post-action
    handler expects.

    Single source of truth for the shape transformation - was
    duplicated inline in the iteration body, flagged by reviewer
    as remaining DRY violation after the Phase 5 parser cleanup.

    Both shapes are documented:
      point_action: {action, screen_x, screen_y, norm_x, norm_y,
                    text, done, reasoning, raw, strategy?}
      action_json:  {Reasoning, Next Action, coordinate, value,
                    Status, _strategy?}
    """
    return {
        'Reasoning': point_action.get('reasoning', ''),
        'Next Action': point_action.get('action', 'left_click'),
        'coordinate': [
            point_action.get('screen_x'),
            point_action.get('screen_y'),
        ],
        'value': point_action.get('text', ''),
        'Status': 'DONE' if point_action.get('done') else 'IN_PROGRESS',
        '_strategy': point_action.get('strategy', 'taskbar_list'),
    }


def _extract_click_coord(raw: str, action_json: dict) -> tuple:
    """Pull the click target coord from the VLM response.

    Single source of truth for "where in 0-1000 norm space did the
    VLM say to click?" — was a 4th parallel parser inline in the
    iteration body.  Now delegates to
    :func:`integrations.vlm.parser.parse_vlm_action` for the
    ``<point>`` regex, then falls back to ``action_json['coordinate']``,
    then to dead center (500, 500).

    Returns ``(nx, ny)`` always — never raises, never returns None.
    Center fallback is the historical behaviour the VLM loop has
    relied on since 2026-04-10.
    """
    from integrations.vlm.parser import parse_vlm_action
    pa = parse_vlm_action(raw or '', expected_shape='point_only')
    if pa.norm_x is not None and pa.norm_y is not None:
        return pa.norm_x, pa.norm_y
    coord = action_json.get('coordinate')
    if coord and isinstance(coord, list) and len(coord) == 2 \
            and coord[0] is not None and coord[1] is not None:
        return coord[0], coord[1]
    return 500, 500


def _parse_vlm_response(response_text: str) -> dict:
    """
    Parse VLM JSON response, handling markdown code blocks and partial JSON.

    Matches OmniParser vlm_agent.py extract_data() pattern.

    Phase 5: thin shim onto the canonical parser in
    :mod:`integrations.vlm.parser`.  Returns the same dict shape this
    function always has (``{Next Action, Status, Reasoning, ...}``)
    via :meth:`ParsedAction.to_action_json_dict`.  The byte-equivalent
    fallback for empty / unparseable input is preserved.
    """
    from integrations.vlm.parser import parse_vlm_action
    pa = parse_vlm_action(response_text or '', expected_shape='action_json')
    return pa.to_action_json_dict()


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
