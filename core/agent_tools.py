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

            # Try local LTX-2 server first
            try:
                tool_logger.info(f"Trying local LTX-2 server at {LOCAL_LTX_URL}")
                response = requests.post(f"{LOCAL_LTX_URL}/generate", json=ltx_payload, headers=headers, timeout=600)
                if response.status_code == 200:
                    result = response.json()
                    video_url = result.get('video_url') or result.get('output_url') or result.get('video_path')
                    if video_url:
                        tool_logger.info(f"LTX-2 video generated: {video_url}")
                        return f"LTX-2 Video generated successfully. URL: {video_url}"
            except requests.exceptions.RequestException as e:
                tool_logger.info(f"Local LTX-2 server not available: {e}")

            # Try ComfyUI with LTX-Video workflow
            try:
                tool_logger.info(f"Trying ComfyUI at {LOCAL_COMFYUI_URL}")
                comfyui_workflow = {
                    "prompt": {
                        "1": {"class_type": "LTXVLoader", "inputs": {"ckpt_name": "ltx-video-2b-v0.9.safetensors"}},
                        "2": {"class_type": "LTXVConditioning", "inputs": {"positive": text, "negative": ltx_payload["negative_prompt"], "ltxv_model": ["1", 0]}},
                        "3": {"class_type": "LTXVSampler", "inputs": {"seed": int(time.time()) % 2147483647, "steps": ltx_payload["num_inference_steps"], "cfg": ltx_payload["guidance_scale"], "width": ltx_payload["width"], "height": ltx_payload["height"], "num_frames": ltx_payload["num_frames"], "ltxv_model": ["1", 0], "conditioning": ["2", 0]}},
                        "4": {"class_type": "LTXVDecode", "inputs": {"ltxv_model": ["1", 0], "samples": ["3", 0]}},
                        "5": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": ltx_payload["fps"], "filename_prefix": "ltx2_output", "format": "video/h264-mp4", "images": ["4", 0]}},
                    }
                }
                response = requests.post(f"{LOCAL_COMFYUI_URL}/prompt", json=comfyui_workflow, headers=headers, timeout=10)
                if response.status_code == 200:
                    comfy_prompt_id = response.json().get('prompt_id')
                    tool_logger.info(f"ComfyUI LTX-2 job queued: {comfy_prompt_id}")
                    for _ in range(120):
                        time.sleep(5)
                        history_response = requests.get(f"{LOCAL_COMFYUI_URL}/history/{comfy_prompt_id}")
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
                tool_logger.info(f"ComfyUI not available: {e}")

            return ("LTX-2 video generation failed. Please ensure one of: "
                    "(1) Local LTX-2 server at localhost:5002, "
                    "(2) ComfyUI with LTX-Video nodes at localhost:8188, "
                    "or (3) diffusers library with CUDA GPU")

        # Default: Avatar-based video generation
        database_url = 'https://mailer.hertzai.com'
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
            res = requests.get(f"{database_url}/get_image_by_id/{avatar_id}")
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
                voice_sample = requests.get(f"{database_url}/get_voice_sample_id/{voice_id}")
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
            requests.post(f"{database_url}/video_generate_save",
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
        url = "http://azurekong.hertzai.com:8000/llava/image_inference"
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
