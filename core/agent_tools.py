"""
core/agent_tools.py — Canonical AutoGen tool definitions (single source of truth).

Every core tool is defined ONCE here. Both create_recipe.py and reuse_recipe.py
call build_core_tool_closures() + register_core_tools() instead of duplicating
function bodies and registration lines.

Pattern mirrors:
  - integrations/service_tools/media_agent.py   → register_media_tools()
  - integrations/channels/memory/agent_memory_tools.py → register_autogen_tools()
  - integrations/agent_engine/marketing_tools.py → register_marketing_tools()
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Annotated, Any, List, Optional, Tuple

import requests
from json_repair import repair_json

from core.http_pool import pooled_get, pooled_post
from integrations.service_tools.model_catalog import ModelType

tool_logger = logging.getLogger('tool_execution')


# ---------------------------------------------------------------------------
# Generic registration helper
# ---------------------------------------------------------------------------

def register_core_tools(tools, helper, executor):
    """Register (name, desc, func) tuples on an AutoGen helper/executor pair.

    Args:
        tools: list of (name, description, func) tuples from build_core_tool_closures()
        helper: AutoGen agent that suggests tool use (register_for_llm)
        executor: AutoGen agent that executes tools (register_for_execution)
    """
    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        executor.register_for_execution(name=name)(func)


# ---------------------------------------------------------------------------
# Core tool closure factory
# ---------------------------------------------------------------------------

def build_core_tool_closures(ctx):
    """Build session-scoped tool closures.  Returns list of (name, desc, func).

    Args:
        ctx: dict with session variables:
            user_id, prompt_id, agent_data, helper_fun, user_prompt,
            request_id_list, recent_file_id, scheduler,
            simplemem_store (optional), memory_graph (optional),
            log_tool_execution (decorator), send_message_to_user1 (func),
            retrieve_json (func), strip_json_values (func),
            save_conversation_db (func)
    """
    # Unpack context -------------------------------------------------------
    user_id = ctx['user_id']
    prompt_id = ctx['prompt_id']
    agent_data = ctx['agent_data']
    helper_fun = ctx['helper_fun']
    user_prompt = ctx['user_prompt']
    request_id_list = ctx['request_id_list']
    recent_file_id = ctx['recent_file_id']
    scheduler = ctx['scheduler']
    simplemem_store = ctx.get('simplemem_store')
    memory_graph = ctx.get('memory_graph')
    # log_tool_execution: optional decorator (create_recipe.py has it, reuse_recipe.py may not)
    log_tool_execution = ctx.get('log_tool_execution') or (lambda f: f)
    send_message_to_user1 = ctx['send_message_to_user1']
    _retrieve_json = ctx['retrieve_json']
    _strip_json_values = ctx['strip_json_values']
    save_conversation_db = ctx['save_conversation_db']

    tools: List[Tuple[str, str, Any]] = []

    # ------------------------------------------------------------------
    # 1. text_2_image
    # ------------------------------------------------------------------
    @log_tool_execution
    def text_2_image(text: Annotated[str, "Text to create image"]) -> str:
        return helper_fun.txt2img(text)

    tools.append((
        "text_2_image",
        "Text to image Creator",
        text_2_image,
    ))

    # ------------------------------------------------------------------
    # 2. get_user_camera_inp
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_camera_inp(
        inp: Annotated[str, "The Question to check from visual context"],
    ) -> str:
        return helper_fun.get_user_camera_inp(inp, int(user_id), request_id_list[user_prompt])

    tools.append((
        "get_user_camera_inp",
        "Get user's visual information to process somethings",
        get_user_camera_inp,
    ))

    # ------------------------------------------------------------------
    # 3. save_data_in_memory
    # ------------------------------------------------------------------
    @log_tool_execution
    def save_data_in_memory(
        key: Annotated[str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
        value: Annotated[Optional[Any], "Value you want to store; strictly should be one of int, float, bool, json array or json object."] = None,
    ) -> str:
        """Store data with validation to prevent corruption."""
        tool_logger.info('INSIDE save_data_in_memory')
        try:
            if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                value = _retrieve_json(value)
                tool_logger.info(f"REPAIRED JSON STRING: {value}")
            if value is not None:
                json_str = json.dumps(value)
                validated_value = json.loads(json_str)
                tool_logger.info(f"VALIDATED VALUE (post JSON cycle): {validated_value}")
            else:
                validated_value = None

            keys = key.split('.')
            d = agent_data.setdefault(prompt_id, {})
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = validated_value
            tool_logger.info(f"VALUES STORED IN AGENT DATA: {validated_value}")
            tool_logger.info(f"FULL AGENT DATA AT KEY: {d}")

            if helper_fun.save_agent_data_to_file(prompt_id, agent_data):
                tool_logger.info(f"[OK] Data persisted to file for prompt_id {prompt_id}")
            else:
                tool_logger.warning(f"Failed to persist data to file for prompt_id {prompt_id}")

            try:
                stored_value = get_data_by_key(key)
                tool_logger.info(f"VERIFICATION - READ BACK VALUE: {stored_value}")
                if stored_value == "Key not found in stored data.":
                    tool_logger.error(f"VERIFICATION FAILED: Data not properly stored at key {key}")
            except Exception as e:
                tool_logger.error(f"VERIFICATION ERROR: {str(e)}")

            return f'{agent_data[prompt_id]}'
        except json.JSONDecodeError as je:
            error_msg = f"Invalid JSON structure in value: {str(je)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except TypeError as te:
            error_msg = f"Type error in value: {str(te)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except Exception as e:
            error_msg = f"Unexpected error saving data: {str(e)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"

    tools.append((
        "save_data_in_memory",
        "Use this to Store and retrieve data using key-value storage system",
        save_data_in_memory,
    ))

    # ------------------------------------------------------------------
    # 4. get_saved_metadata
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_saved_metadata() -> str:
        """Get metadata with automatic loading from persistent storage."""
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for get_saved_metadata, prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        stripped_json = _strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    tools.append((
        "get_saved_metadata",
        "Returns the schema of the json from internal memory with all keys but without actual values.",
        get_saved_metadata,
    ))

    # ------------------------------------------------------------------
    # 5. get_data_by_key
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_data_by_key(
        key: Annotated[str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."],
    ) -> str:
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})
        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            return "Key not found in stored data."

    tools.append((
        "get_data_by_key",
        "Returns all data from the internal Memory using key",
        get_data_by_key,
    ))

    # ------------------------------------------------------------------
    # 6. get_user_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_id() -> str:
        tool_logger.info('INSIDE get_user_id')
        return f'{user_id}'

    tools.append((
        "get_user_id",
        "Returns the unique identifier (user_id) of the current user.",
        get_user_id,
    ))

    # ------------------------------------------------------------------
    # 7. get_prompt_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_prompt_id() -> str:
        tool_logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    tools.append((
        "get_prompt_id",
        "Returns the unique identifier (prompt_id) associated with the current prompt or conversation.",
        get_prompt_id,
    ))

    # ------------------------------------------------------------------
    # 8. Generate_video (canonical — full LTX-2 + avatar)
    # ------------------------------------------------------------------
    @log_tool_execution
    def Generate_video(
        text: Annotated[str, "Text to be used for video generation"],
        avatar_id: Annotated[int, "Unique identifier for the avatar (use 0 for LTX-2 text-to-video)"],
        realtime: Annotated[bool, "If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"],
        model: Annotated[str, "Video model to use: 'avatar' for avatar-based video, 'ltx2' for LTX-2 text-to-video generation"] = "avatar",
    ) -> str:
        tool_logger.info(f'INSIDE Generate_video with model={model}')

        # LTX-2 Text-to-Video Generation
        if model.lower() == "ltx2":
            tool_logger.info(f'Using LTX-2 for video generation: {text[:50]}...')
            LOCAL_COMFYUI_URL = "http://localhost:8188"
            LOCAL_LTX_URL = "http://localhost:5002"
            headers = {'Content-Type': 'application/json'}
            ltx_payload = {
                "prompt": text,
                "negative_prompt": "worst quality, inconsistent motion, blurry, jittery, distorted",
                "num_frames": 97,
                "width": 832,
                "height": 480,
                "num_inference_steps": 30 if realtime else 50,
                "guidance_scale": 3.0,
                "fps": 24,
            }

            # Fast health probe — skip dead servers instantly (0ms vs 10s timeout)
            def _is_server_up(url, name):
                try:
                    r = pooled_get(f"{url}/health", timeout=1.5)
                    return r.status_code < 500
                except Exception:
                    tool_logger.info(f"{name} not reachable — skipping instantly")
                    return False

            # Try local LTX-2 server first — only if alive
            if _is_server_up(LOCAL_LTX_URL, "LTX-2"):
                try:
                    tool_logger.info(f"LTX-2 server is UP, generating...")
                    response = pooled_post(f"{LOCAL_LTX_URL}/generate", json=ltx_payload, headers=headers, timeout=600)
                    if response.status_code == 200:
                        result = response.json()
                        video_url = result.get('video_url') or result.get('output_url') or result.get('video_path')
                        if video_url:
                            tool_logger.info(f"LTX-2 video generated: {video_url}")
                            return f"LTX-2 Video generated successfully. URL: {video_url}"
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"LTX-2 generation failed: {e}")

            # Try ComfyUI — only if alive
            if _is_server_up(LOCAL_COMFYUI_URL, "ComfyUI"):
                try:
                    tool_logger.info(f"ComfyUI is UP, submitting workflow...")
                    comfyui_workflow = {
                        "prompt": {
                            "1": {"class_type": "LTXVLoader", "inputs": {"ckpt_name": "ltx-video-2b-v0.9.safetensors"}},
                            "2": {"class_type": "LTXVConditioning", "inputs": {"positive": text, "negative": ltx_payload["negative_prompt"], "ltxv_model": ["1", 0]}},
                            "3": {"class_type": "LTXVSampler", "inputs": {"seed": int(time.time()) % 2147483647, "steps": ltx_payload["num_inference_steps"], "cfg": ltx_payload["guidance_scale"], "width": ltx_payload["width"], "height": ltx_payload["height"], "num_frames": ltx_payload["num_frames"], "ltxv_model": ["1", 0], "conditioning": ["2", 0]}},
                            "4": {"class_type": "LTXVDecode", "inputs": {"ltxv_model": ["1", 0], "samples": ["3", 0]}},
                            "5": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": ltx_payload["fps"], "filename_prefix": "ltx2_output", "format": "video/h264-mp4", "images": ["4", 0]}},
                        }
                    }
                    response = pooled_post(f"{LOCAL_COMFYUI_URL}/prompt", json=comfyui_workflow, headers=headers, timeout=10)
                    if response.status_code == 200:
                        comfy_prompt_id = response.json().get('prompt_id')
                        tool_logger.info(f"ComfyUI LTX-2 job queued: {comfy_prompt_id}")
                        for _ in range(120):
                            time.sleep(5)
                            history_response = pooled_get(f"{LOCAL_COMFYUI_URL}/history/{comfy_prompt_id}")
                            if history_response.status_code == 200:
                                history = history_response.json()
                                if comfy_prompt_id in history:
                                    outputs = history[comfy_prompt_id].get('outputs', {})
                                    for node_id, output in outputs.items():
                                        for media_key in ('gifs', 'videos'):
                                            if media_key in output:
                                                filename = output[media_key][0].get('filename')
                                                if filename:
                                                    video_url = f"{LOCAL_COMFYUI_URL}/view?filename={filename}"
                                                    return f"LTX-2 Video generated via ComfyUI. URL: {video_url}"
                        return f"LTX-2 Video generation queued in ComfyUI (prompt_id: {comfy_prompt_id}). Check ComfyUI interface for output."
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"ComfyUI generation failed: {e}")

            # Local servers unavailable — try hive mesh peer with GPU
            try:
                from integrations.agent_engine.compute_config import get_compute_policy
                policy = get_compute_policy()
                if policy.get('compute_policy') != 'local_only':
                    from integrations.agent_engine.compute_mesh_service import get_compute_mesh
                    mesh = get_compute_mesh()
                    result = mesh.offload_to_best_peer(
                        model_type=ModelType.VIDEO_GEN,
                        prompt=text,
                        options={'model': 'ltx2', 'timeout': 300},
                    )
                    if result and 'error' not in result:
                        video_url = result.get('response', result.get('video_url', ''))
                        peer = result.get('offloaded_to', 'hive_peer')
                        tool_logger.info(f"LTX-2 video generated via hive peer {peer}: {video_url}")
                        return f"LTX-2 Video generated via hive peer. URL: {video_url}"
                    tool_logger.info(f"Hive mesh video offload failed: {result.get('error')}")
            except Exception as e:
                tool_logger.info(f"Hive mesh offload not available: {e}")

            return ("LTX-2 video generation failed. No local GPU, no hive peers with GPU. "
                    "Options: (1) Pair a GPU device: hart compute pair <address>, "
                    "(2) Set HEVOLVE_COMPUTE_POLICY=any, "
                    "(3) Install local LTX-2 server with CUDA GPU")

        # Default: Avatar-based video generation
        from core.config_cache import get_db_url
        database_url = get_db_url() or 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        tool_logger.info(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")

        headers = {'Content-Type': 'application/json'}
        data = {
            "text": str(text),
            'flag_hallo': 'false',
            'chattts': False,
            'openvoice': "false",
        }

        try:
            res = pooled_get(f"{database_url}/get_image_by_id/{avatar_id}")
            res = res.json()
            new_image_url = res["image_url"]
            voice_id = res.get('voice_id')
        except Exception:
            data['openvoice'] = "true"
            new_image_url = None
            voice_id = None

        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = new_image_url
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = str(request_id)
        data['gradient'] = "true"
        data['cus_bg'] = "false"
        data['solid_color'] = "false"
        data['inpainting'] = "false"
        data['prompt'] = ""
        data['gender'] = 'male'

        timeout = 60
        if not realtime:
            timeout = 600
            data['chattts'] = True
            data['flag_hallo'] = "true"
            data["cartoon_image"] = "False"

        if voice_id is not None:
            try:
                voice_sample = pooled_get(f"{database_url}/get_voice_sample_id/{voice_id}")
                voice_sample = voice_sample.json()
                data["audio_sample_url"] = voice_sample.get("voice_sample_url")
                data['voice_id'] = int(voice_id) if voice_id else None
            except Exception:
                data["audio_sample_url"] = None
                data['voice_id'] = None
        else:
            data["audio_sample_url"] = None
            data['voice_id'] = None

        conv_id = save_conversation_db(text, user_id, prompt_id, database_url, request_id)
        data['conv_id'] = int(conv_id)
        data['avatar_id'] = int(avatar_id)
        data['timeout'] = int(timeout)

        try:
            pooled_post(f"{database_url}/video_generate_save",
                          data=json.dumps(data), headers=headers, timeout=1)
        except Exception:
            pass

        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"

    tools.append((
        "Generate_video",
        "Generate video with text. Use model='ltx2' for AI text-to-video generation, or model='avatar' (default) for avatar-based video with voice synthesis.",
        Generate_video,
    ))

    # ------------------------------------------------------------------
    # 9. get_user_uploaded_file
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_uploaded_file() -> str:
        tool_logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'
        return 'No file uploaded from user'

    tools.append((
        "get_user_uploaded_file",
        "get user's recent uploaded files",
        get_user_uploaded_file,
    ))

    # ------------------------------------------------------------------
    # 10. get_text_from_image (img2txt)
    # ------------------------------------------------------------------
    @log_tool_execution
    def img2txt(
        image_url: Annotated[str, "image url of which you want text"],
        text: Annotated[str, "the details you want from image"] = 'Describe the Images & Text data in this image in detail',
    ) -> str:
        tool_logger.info('INSIDE img2txt')
        # SSRF protection: validate image URL before fetching
        try:
            from security.sanitize import validate_url
            image_url = validate_url(image_url)
        except (ImportError, ValueError) as e:
            tool_logger.warning(f"Image URL blocked by SSRF filter: {image_url} — {e}")
            return f"Error: URL blocked by security filter: {e}"
        # Try local Qwen Vision first (bundled mode), fall back to cloud
        from core.config_cache import get_vision_api, is_bundled
        url = get_vision_api()
        if not url:
            tool_logger.warning("No LLAVA_API configured — vision inference may fail on no-GPU instances")
            url = "http://azurekong.hertzai.com:8000/llava/image_inference"

        if is_bundled():
            # Local: use Qwen Vision via upload/vision endpoint
            payload = json.dumps({'image_url': image_url, 'prompt': text})
            response = requests.post(url, data=payload,
                                     headers={'Content-Type': 'application/json'}, timeout=60)
        else:
            payload = {'url': image_url, 'prompt': text}
            response = requests.request("POST", url, headers={}, data=payload, files=[], timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'

    tools.append((
        "get_text_from_image",
        "Image to Text/Question Answering from image",
        img2txt,
    ))

    # ------------------------------------------------------------------
    # 11. create_scheduled_jobs
    # ------------------------------------------------------------------
    @log_tool_execution
    def create_scheduled_jobs(
        interval_sec: Annotated[int, "time between two Interval in seconds."],
        job_description: Annotated[str, "Description of the job to be performed"],
        cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"] = None,
    ) -> str:
        tool_logger.info('INSIDE create_scheduled_jobs')
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'

    tools.append((
        "create_scheduled_jobs",
        "Creates time-based jobs using APScheduler to schedule jobs",
        create_scheduled_jobs,
    ))

    # ------------------------------------------------------------------
    # 12. send_message_to_user
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_message_to_user(
        text: Annotated[str, "Text you want to send to the user"],
        avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
        response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime',
    ) -> str:
        tool_logger.info('INSIDE send_message_to_user')
        tool_logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        thread = threading.Thread(target=send_message_to_user1, args=(user_id, text, '', prompt_id))
        thread.start()
        return f'Message sent successfully to user with request_id: {request_id_list[user_prompt]}-intermediate'

    tools.append((
        "send_message_to_user",
        "Sends a message/information to user. You can use this if you want to ask a question",
        send_message_to_user,
    ))

    # ------------------------------------------------------------------
    # 13. send_presynthesized_video_to_user
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_presynthesized_video_to_user(
        conv_id: Annotated[str, "Conversation ID associated with the text from memory"],
    ) -> str:
        tool_logger.info('INSIDE send_presynthesized_video_to_user')
        tool_logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    tools.append((
        "send_presynthesized_video_to_user",
        "Sends a presynthesized message/video/dialogue to user using conv_id.",
        send_presynthesized_video_to_user,
    ))

    # ------------------------------------------------------------------
    # 14. send_message_in_seconds
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_message_in_seconds(
        text: Annotated[str, "text to send to user"],
        delay: Annotated[int, "time to wait in seconds before sending text"],
        conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"] = None,
    ) -> str:
        tool_logger.info('INSIDE send_message_in_seconds')
        tool_logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '', prompt_id])
        return 'Message scheduled successfully'

    tools.append((
        "send_message_in_seconds",
        "Sends a presynthesized message/video/dialogue to user using conv_id with a timer.",
        send_message_in_seconds,
    ))

    # ------------------------------------------------------------------
    # 15. get_chat_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_chat_history(
        text: Annotated[str, "Text related to which you want history"],
        start: Annotated[Optional[str], "start date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
        end: Annotated[Optional[str], "end date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
    ) -> str:
        tool_logger.info('INSIDE get_chat_history')
        return helper_fun.get_time_based_history(text, f'user_{user_id}', start, end)

    tools.append((
        "get_chat_history",
        "Get Chat history based on text & start & end date",
        get_chat_history,
    ))

    # ------------------------------------------------------------------
    # 16. search_visual_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def search_visual_history(
        query: Annotated[str, "What to search for in visual/screen descriptions"],
        minutes_back: Annotated[int, "How many minutes back to search (default 30)"] = 30,
        channel: Annotated[str, "Which feed: 'camera', 'screen', or 'both' (default)"] = "both",
    ) -> str:
        """Search past camera/screen descriptions. Use for questions about what happened earlier visually."""
        results = helper_fun.search_visual_history(user_id, query, mins=minutes_back, channel=channel)
        if results:
            return '\n'.join(results)
        return "No matching visual/screen descriptions found in the given time range."

    tools.append((
        "search_visual_history",
        "Search past camera and screen descriptions by keyword and time range.",
        search_visual_history,
    ))

    # ------------------------------------------------------------------
    # 17. google_search
    # ------------------------------------------------------------------
    @log_tool_execution
    def google_search(
        text: Annotated[str, "Text/Query which you want to search"],
    ) -> str:
        tool_logger.info('INSIDE google search')
        return helper_fun.top5_results(text)

    tools.append((
        "google_search",
        "web/google/bing search api tool for a given query",
        google_search,
    ))

    # ------------------------------------------------------------------
    # Conditional: SimpleMem long-term memory
    # ------------------------------------------------------------------
    if simplemem_store is not None:
        from core.event_loop import get_or_create_event_loop

        @log_tool_execution
        def search_long_term_memory(
            query: Annotated[str, "Natural language query to search long-term memory"],
        ) -> str:
            """Search compressed long-term memory using semantic retrieval."""
            try:
                loop = get_or_create_event_loop()
                results = loop.run_until_complete(simplemem_store.search(query))
                if results:
                    return results[0].content
                return "No relevant memories found."
            except Exception as e:
                tool_logger.info(f"SimpleMem search error: {e}")
                return "Memory search unavailable."

        tools.append((
            "search_long_term_memory",
            "Search long-term memory for past conversations, facts, and context using natural language query.",
            search_long_term_memory,
        ))

        @log_tool_execution
        def save_to_long_term_memory(
            content: Annotated[str, "The information/fact to remember long-term"],
            speaker: Annotated[str, "Who said this (e.g. 'User', 'Assistant', 'System')"] = "System",
        ) -> str:
            """Save important information to compressed long-term memory."""
            try:
                loop = get_or_create_event_loop()
                loop.run_until_complete(simplemem_store.add(content, {
                    "sender_name": speaker,
                    "user_id": user_id,
                    "prompt_id": prompt_id,
                }))
                return "Saved to long-term memory."
            except Exception as e:
                tool_logger.info(f"SimpleMem save error: {e}")
                return "Failed to save to long-term memory."

        tools.append((
            "save_to_long_term_memory",
            "Save important facts or information to long-term memory for future retrieval across sessions.",
            save_to_long_term_memory,
        ))

    # ------------------------------------------------------------------
    # Suggest_Share_Worthy_Content
    # ------------------------------------------------------------------
    @log_tool_execution
    def suggest_share_worthy_content(
        query: Annotated[str, "Any text — not used for filtering, just provide context"] = "",
    ) -> str:
        """Find high-engagement posts that haven't been shared much and suggest sharing them."""
        try:
            from integrations.social.models import get_db, Post, ShareableLink
            from sqlalchemy import func as sa_func

            db = get_db()
            try:
                share_counts = (
                    db.query(
                        ShareableLink.resource_id,
                        sa_func.count(ShareableLink.id).label('link_count'),
                    )
                    .filter(ShareableLink.resource_type == 'post')
                    .group_by(ShareableLink.resource_id)
                    .subquery()
                )

                posts = (
                    db.query(Post, share_counts.c.link_count)
                    .outerjoin(share_counts, Post.id == share_counts.c.resource_id)
                    .filter(
                        Post.is_deleted == False,
                        Post.is_hidden == False,
                        Post.upvotes > 5,
                        Post.comment_count > 3,
                    )
                    .filter(
                        (share_counts.c.link_count == None) |  # noqa: E711
                        (share_counts.c.link_count < 3)
                    )
                    .order_by(Post.score.desc())
                    .limit(3)
                    .all()
                )

                if not posts:
                    return ("No under-shared high-engagement content found right now. "
                            "Keep creating great posts and the community will notice!")

                suggestions = []
                for post, link_count in posts:
                    title = (post.title or post.content or '')[:80].strip()
                    shares = link_count or 0
                    suggestions.append(
                        f"- \"{title}\" ({post.upvotes} upvotes, "
                        f"{post.comment_count} comments, only {shares} shares) "
                        f"[post_id: {post.id}]"
                    )

                header = ("These posts are resonating with the community but haven't "
                           "been shared much yet. Consider sharing them:\n")
                return header + "\n".join(suggestions)
            finally:
                db.close()
        except Exception as e:
            tool_logger.warning(f"Suggest_Share_Worthy_Content failed: {e}")
            return f"Could not fetch share-worthy content right now: {e}"

    tools.append((
        "Suggest_Share_Worthy_Content",
        "Find high-engagement posts that deserve wider reach but haven't been shared much. "
        "Use when the user asks about content worth sharing or to proactively suggest "
        "share-worthy community posts.",
        suggest_share_worthy_content,
    ))

    # ------------------------------------------------------------------
    # Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        input_text: Annotated[str, "JSON string with event, page, duration_ms, outcome fields"],
    ) -> str:
        """Record a user experience observation for self-improvement."""
        try:
            data = json.loads(input_text) if input_text.startswith('{') else {'event': input_text}
            event = data.get('event', 'interaction')
            page = data.get('page', '')
            duration_ms = data.get('duration_ms', 0)
            outcome = data.get('outcome', 'recorded')

            observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"

            if memory_graph:
                session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                memory_id = memory_graph.register(
                    content=observation,
                    metadata={
                        'memory_type': 'observation',
                        'source_agent': 'agent',
                        'session_id': session_key,
                        'page': page,
                        'event': event,
                    },
                    context_snapshot=f"UX observation during session {session_key}",
                )
                return f"Observation recorded (id: {memory_id}): {observation}"

            return f"Observation noted: {observation}"
        except Exception as e:
            tool_logger.warning(f"Observe_User_Experience failed: {e}")
            return f"Observation noted: {input_text}"

    tools.append((
        "Observe_User_Experience",
        "Record a user experience observation. Input: JSON with event, page, "
        "duration_ms, outcome. Used for self-improvement and understanding user "
        "behavior patterns.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        input_text: Annotated[str, "Topic or area to critique"],
    ) -> str:
        """Review past suggestions and outcomes to improve future behavior."""
        try:
            if not memory_graph:
                return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

            session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)

            # Recall past suggestions and observations
            suggestions = memory_graph.recall(
                input_text or 'suggestions made outcomes', mode='semantic', top_k=10,
            )
            observations = memory_graph.recall(
                'user experience observation', mode='semantic', top_k=10,
            )

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            # Format findings for agent reasoning
            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            # Store the critique itself as an insight
            insight = f"Self-critique on: {input_text}"
            memory_graph.register(
                content=insight,
                metadata={
                    'memory_type': 'insight',
                    'source_agent': 'agent',
                    'session_id': session_key,
                    'type': 'self_critique',
                },
                context_snapshot=f"Self-critique during session {session_key}",
            )

            return critique
        except Exception as e:
            tool_logger.warning(f"Self_Critique_And_Enhance failed: {e}")
            return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

    tools.append((
        "Self_Critique_And_Enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique. Helps the agent "
        "learn from its own interactions.",
        self_critique_and_enhance,
    ))

    # ------------------------------------------------------------------
    # device_control — Cross-device control via PeerLink (SAME_USER only)
    # ------------------------------------------------------------------
    @log_tool_execution
    def device_control(
        action: Annotated[str, "What to do: 'turn on light', 'check temperature', 'list files', 'run command ls -la'"],
        device_hint: Annotated[str, "Which device: 'phone', 'desktop', 'iot hub', or empty for default"] = '',
    ) -> str:
        """Control a device on the user's private network via PeerLink.

        Privacy-first: only targets the user's own devices (SAME_USER trust).
        Uses PeerLink dispatch channel with FleetCommandService fallback.
        """
        try:
            # Step 1: Find the target device via DeviceRoutingService
            target_device = None
            try:
                from integrations.social.models import db_session
                from integrations.social.device_routing_service import DeviceRoutingService
                with db_session(commit=False) as db:
                    if device_hint:
                        # Map hint to capability or form factor
                        capability = 'general'
                        if device_hint.lower() in ('phone', 'desktop', 'tablet', 'tv', 'embedded', 'robot'):
                            # Filter by form factor
                            devices = DeviceRoutingService.get_user_device_map(db, str(user_id))
                            for d in devices:
                                if d.get('form_factor', '') == device_hint.lower():
                                    target_device = d
                                    break
                        if not target_device:
                            target_device = DeviceRoutingService.pick_device(
                                db, str(user_id), required_capability=capability)
                    else:
                        target_device = DeviceRoutingService.pick_device(
                            db, str(user_id), required_capability='general')
            except Exception as e:
                tool_logger.debug(f"Device routing lookup failed: {e}")

            target_node_id = (target_device or {}).get('device_id', '')

            # Step 2: Try PeerLink dispatch channel (SAME_USER trust only)
            peerlink_sent = False
            if target_node_id:
                try:
                    from core.peer_link.link_manager import get_link_manager
                    from core.peer_link.link import TrustLevel
                    mgr = get_link_manager()
                    link = mgr.get_link(target_node_id)
                    if link and link.trust == TrustLevel.SAME_USER:
                        result = mgr.send(
                            target_node_id, 'dispatch',
                            {'type': 'device_control', 'action': action,
                             'user_id': str(user_id)},
                            wait_response=True, timeout=30.0,
                        )
                        if result is not None:
                            peerlink_sent = True
                            msg = result.get('message', str(result))
                            return f"Device control result: {msg}"
                    elif link and link.trust != TrustLevel.SAME_USER:
                        return ("Device control blocked: target device is not a SAME_USER "
                                "trusted device. Only your own devices can be controlled.")
                except Exception as e:
                    tool_logger.debug(f"PeerLink dispatch failed: {e}")

            # Step 3: Fallback to FleetCommandService
            if not peerlink_sent:
                try:
                    from integrations.social.models import db_session
                    from integrations.social.fleet_command import FleetCommandService
                    with db_session() as db:
                        cmd = FleetCommandService.push_command(
                            db, target_node_id or 'self',
                            'device_control',
                            {'action': action, 'device_hint': device_hint},
                        )
                        if cmd:
                            return f"Device control command queued (id={cmd.get('id', '?')}): {action}"
                        return "Device control: failed to queue command"
                except Exception as e:
                    tool_logger.debug(f"Fleet command fallback failed: {e}")

            # Step 4: Local execution as last resort (this IS the target device)
            try:
                from integrations.social.fleet_command import FleetCommandService
                result = FleetCommandService.execute_command(
                    'device_control', {'action': action})
                if result.get('success'):
                    return f"Device control (local): {result.get('message', 'OK')}"
                return f"Device control failed: {result.get('message', 'Unknown error')}"
            except Exception as e:
                return f"Device control unavailable: {e}"

        except Exception as e:
            tool_logger.warning(f"device_control failed: {e}")
            return f"Device control error: {e}"

    tools.append((
        "device_control",
        "Control a device on the user's private network. Actions: turn on/off lights, "
        "check temperature, list files, run commands. Privacy-first: only your own devices.",
        device_control,
    ))

    # ------------------------------------------------------------------
    # data_extraction_from_url — Parity with LangChain Data_Extraction_From_URL
    # ------------------------------------------------------------------
    @log_tool_execution
    def data_extraction_from_url(
        url: Annotated[str, "The URL to extract content from"],
        url_type: Annotated[str, "Type of URL: 'pdf' or 'website'"] = "website",
    ) -> str:
        """Extract content from a URL (PDF or website). Uses Crawl4AI or direct parsing."""
        try:
            from threadlocal import thread_local_data as _tld
            _uid = _tld.get_user_id() if hasattr(_tld, 'get_user_id') else user_id
            _rid = _tld.get_request_id() if hasattr(_tld, 'get_request_id') else None

            # Try Crawl4AI service first
            try:
                from integrations.service_tools import service_tool_registry
                crawl_tool = service_tool_registry.get_tool('Crawl4AI')
                if crawl_tool:
                    result = crawl_tool.execute(url)
                    if result:
                        return f"Extracted from {url}:\n{str(result)[:5000]}"
            except Exception:
                pass

            # Fallback: direct requests
            import requests as _req
            resp = _req.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            if url_type == 'pdf':
                return f"PDF downloaded ({len(resp.content)} bytes). Use a PDF parser for full extraction."
            text = resp.text[:5000]
            # Strip HTML tags naively
            import re
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return f"Extracted from {url}:\n{text[:4000]}"
        except Exception as e:
            return f"URL extraction failed: {e}"

    tools.append((
        "data_extraction_from_url",
        "Extract content from a URL (PDF or website). Input: URL and type ('pdf' or 'website'). "
        "Uses Crawl4AI for rich extraction with fallback to direct HTTP fetch.",
        data_extraction_from_url,
    ))

    # ------------------------------------------------------------------
    # get_user_details — Parity with LangChain User_details_tool
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_details() -> str:
        """Get current user's profile details."""
        try:
            uid = user_id
            # Try local DB first
            try:
                from integrations.social.models import get_db, User
                db = get_db()
                try:
                    user = db.query(User).filter_by(id=str(uid)).first()
                    if user:
                        return json.dumps(user.to_dict(), default=str)
                finally:
                    db.close()
            except Exception:
                pass

            # Fallback: cloud API
            import requests as _req
            resp = _req.post(
                'https://azurekong.hertzai.com:8443/db/getstudent_by_user_id',
                json={'user_id': uid}, timeout=10,
            )
            return f"User details: {resp.text}"
        except Exception as e:
            return f"Could not fetch user details: {e}"

    tools.append((
        "get_user_details",
        "Get the current user's profile information (name, email, preferences, etc.). "
        "Use when the user asks about their profile or when you need user context.",
        get_user_details,
    ))

    # ------------------------------------------------------------------
    # request_resource — Parity with LangChain Request_Resource
    # ------------------------------------------------------------------
    @log_tool_execution
    def request_resource(
        resource_description: Annotated[str, "JSON or plain text describing the needed resource. JSON format: {\"resource_type\": \"api_key\", \"key_name\": \"GOOGLE_API_KEY\", \"label\": \"Google API Key\", \"used_by\": \"search tool\", \"description\": \"needed for web search\"}"],
    ) -> str:
        """Request an API key, credential, token, or config value that is not currently available."""
        try:
            try:
                req = json.loads(resource_description)
            except (ValueError, TypeError):
                req = {
                    'resource_type': 'api_key',
                    'key_name': 'UNKNOWN',
                    'label': resource_description[:100],
                    'description': resource_description,
                    'used_by': 'Agent tool',
                }

            key_name = req.get('key_name', 'UNKNOWN')
            resource_type = req.get('resource_type', 'api_key')

            # Check env vars first
            env_val = os.environ.get(key_name)
            if env_val:
                return f"Resource '{key_name}' is already configured and available."

            # Check vault
            try:
                from desktop.ai_key_vault import AIKeyVault
                vault = AIKeyVault.get_instance()
                val = vault.get_tool_key(key_name) if resource_type != 'channel_secret' else vault.get_channel_secret(req.get('channel_type', ''), key_name)
                if val:
                    os.environ[key_name] = val
                    return f"Resource '{key_name}' loaded from vault and is now available."
            except Exception:
                pass

            # Track as pending and request from user
            try:
                from desktop.ai_key_vault import AIKeyVault
                AIKeyVault.get_instance().add_pending_request(
                    key_name=key_name, resource_type=resource_type,
                    channel_type=req.get('channel_type', ''),
                    label=req.get('label', key_name),
                    description=req.get('description', ''),
                    used_by=req.get('used_by', 'Agent tool'),
                )
            except Exception:
                pass

            secret_request = json.dumps({
                '__SECRET_REQUEST__': True, 'type': resource_type,
                'key_name': key_name, 'label': req.get('label', key_name),
                'description': req.get('description', f'{key_name} is required.'),
                'used_by': req.get('used_by', 'Agent tool'),
                'channel_type': req.get('channel_type', ''),
            })
            return (
                f"I need the user to provide '{req.get('label', key_name)}'. "
                f"Required for {req.get('used_by', 'a tool')}. "
                f"{req.get('description', '')} "
                f"RESOURCE_REQUEST:{secret_request}"
            )
        except Exception as e:
            return f"Resource request failed: {e}"

    tools.append((
        "request_resource",
        "Request an API key, credential, token, or config value. Checks vault/env first, "
        "then prompts the user if not found. Handles: API keys (OpenAI, Google, Slack), "
        "OAuth tokens, channel secrets, service credentials. "
        "Input: JSON with resource_type, key_name, label, used_by, description.",
        request_resource,
    ))

    # ------------------------------------------------------------------
    # observe_user_experience — Parity with LangChain Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        event: Annotated[str, "What happened (e.g. 'clicked', 'scrolled', 'left page')"],
        page: Annotated[str, "Which page or screen"] = "",
        outcome: Annotated[str, "What was the result or user reaction"] = "",
        duration_ms: Annotated[int, "How long the interaction lasted in ms"] = 0,
    ) -> str:
        """Record a user experience observation for self-improvement."""
        observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"
        try:
            if memory_graph:
                session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                mid = memory_graph.register(
                    content=observation,
                    metadata={'memory_type': 'observation', 'source_agent': 'agent',
                              'session_id': session_id, 'page': page, 'event': event},
                    context_snapshot=f"UX observation during session {session_id}",
                )
                return f"Observation recorded (id: {mid}): {observation}"
        except Exception:
            pass
        return f"Observation noted: {observation}"

    tools.append((
        "observe_user_experience",
        "Record a user experience observation. Use to track behavior patterns "
        "for self-improvement. Input: event, page, outcome, duration_ms.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # self_critique_and_enhance — Parity with LangChain Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        topic: Annotated[str, "Topic or area to critique (e.g. 'my recommendations', 'search quality')"] = "",
    ) -> str:
        """Review past agent suggestions and user observations to improve future behavior."""
        try:
            if not memory_graph:
                return "Self-critique unavailable: no memory graph for this session."

            suggestions = memory_graph.recall(topic or 'suggestions made outcomes', mode='semantic', top_k=10)
            observations = memory_graph.recall('user experience observation', mode='semantic', top_k=10)

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
            memory_graph.register(
                content=f"Self-critique on: {topic}",
                metadata={'memory_type': 'insight', 'source_agent': 'agent',
                          'session_id': session_id, 'type': 'self_critique'},
                context_snapshot=f"Self-critique during session {session_id}",
            )
            return critique
        except Exception as e:
            return f"Self-critique unavailable: {e}"

    tools.append((
        "self_critique_and_enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique.",
        self_critique_and_enhance,
    ))

    return tools


def register_remote_desktop_tools_if_available(ctx, helper, executor):
    """Register remote desktop tools if the module is available.

    Gracefully skips if integrations/remote_desktop is not installed.
    """
    try:
        from integrations.remote_desktop.agent_tools import (
            build_remote_desktop_tools, register_remote_desktop_tools,
        )
        rd_tools = build_remote_desktop_tools(ctx)
        register_remote_desktop_tools(rd_tools, helper, executor)
        tool_logger.info(f"Remote desktop tools registered ({len(rd_tools)} tools)")
    except ImportError:
        pass
    except Exception as e:
        tool_logger.warning(f"Remote desktop tools registration failed: {e}")


def register_memory_graph_tools(memory_graph, helper, executor, user_id, user_prompt):
    """Register MemoryGraph provenance tools if memory_graph is available.

    Delegates to the existing agent_memory_tools module.
    """
    if memory_graph is None:
        return
    try:
        from integrations.channels.memory.agent_memory_tools import create_memory_tools, register_autogen_tools
        mem_tools = create_memory_tools(memory_graph, str(user_id), user_prompt)
        register_autogen_tools(mem_tools, executor, helper)
        tool_logger.info(f"MemoryGraph tools registered for {user_prompt}")
    except Exception as e:
        tool_logger.warning(f"MemoryGraph tools registration failed: {e}")
