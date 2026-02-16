"""
Unified Media Generation Agent — single AutoGen tool for all media modalities.

Any agent in the system can call `generate_media()` to produce content across
image, audio (speech/music), and video modalities. The agent auto-selects the
best available tool, auto-starts services if needed, and returns results in a
consistent JSON format.

Routing table:
  image          → txt2img (external service)
  audio_speech   → tts_audio_suite (auto-start sidecar)
  audio_music    → acestep (external: uv run acestep-api)
  audio_speech_music → tts_audio_suite + acestep
  video          → wan2gp (VRAM >= 8GB) | ltx2 fallback
  video_with_audio → video tool + tts_audio_suite

Companion tool: `check_media_status()` for polling async tasks.
"""

import json
import logging
import time
from typing import Annotated, Optional

logger = logging.getLogger(__name__)

# Valid output modalities
VALID_MODALITIES = {
    'image', 'audio_speech', 'audio_music',
    'audio_speech_music', 'video', 'video_with_audio',
}


# ═══════════════════════════════════════════════════════════════
# Auto-start helpers
# ═══════════════════════════════════════════════════════════════

def _ensure_tool_running(tool_name: str) -> bool:
    """Auto-start a tool if it's not running. Returns True if available."""
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        status = runtime_tool_manager.get_tool_status(tool_name)
        if status.get('running'):
            return True
        result = runtime_tool_manager.setup_tool(tool_name)
        return result.get('running', False)
    except Exception as e:
        logger.warning(f"Auto-start failed for {tool_name}: {e}")
        return False


def _select_video_tool() -> str:
    """Select the best video tool based on available VRAM.

    VRAM >= 8 GB → wan2gp (higher quality)
    Otherwise   → ltx2 (supports CPU offload)
    """
    try:
        from integrations.service_tools.vram_manager import vram_manager
        info = vram_manager.detect_gpu()
        free_gb = info.get('free_gb', 0)
        if free_gb >= 8.0:
            return 'wan2gp'
    except Exception:
        pass
    return 'ltx2'


def _get_tool_base_url(tool_name: str) -> Optional[str]:
    """Get the base URL for a registered tool."""
    try:
        from integrations.service_tools.registry import service_tool_registry
        tool = service_tool_registry._tools.get(tool_name)
        if tool:
            return tool.base_url.rstrip('/')
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# Modality handlers
# ═══════════════════════════════════════════════════════════════

def _generate_image(context: str, input_text: str, style: str) -> dict:
    """Route to txt2img external service."""
    prompt = input_text or context
    if style:
        prompt = f"{prompt}, {style} style"
    try:
        from helper import txt2img
        img_url = txt2img(prompt)
        return {
            'status': 'completed',
            'output_modality': 'image',
            'results': [{'type': 'image', 'url': img_url, 'format': 'png'}],
            'model_used': 'txt2img',
        }
    except Exception as e:
        return {'status': 'error', 'error': str(e), 'output_modality': 'image'}


def _generate_audio_speech(context: str, input_text: str, duration: int) -> dict:
    """Route to TTS-Audio-Suite for speech synthesis."""
    text = input_text or context
    if not _ensure_tool_running('tts_audio_suite'):
        return {
            'status': 'error',
            'error': 'TTS-Audio-Suite not available and auto-start failed',
            'output_modality': 'audio_speech',
        }

    base_url = _get_tool_base_url('tts_audio_suite')
    if not base_url:
        return {'status': 'error', 'error': 'TTS-Audio-Suite not registered',
                'output_modality': 'audio_speech'}

    try:
        from core.http_pool import pooled_post
        resp = pooled_post(
            f"{base_url}/synthesize",
            json={'text': text},
            headers={'Content-Type': 'application/json'},
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            audio_url = data.get('audio_url') or data.get('url', '')
            return {
                'status': 'completed',
                'output_modality': 'audio_speech',
                'results': [{'type': 'audio', 'url': audio_url,
                             'format': 'wav'}],
                'model_used': 'tts_audio_suite',
            }
        return {'status': 'error', 'error': f'TTS HTTP {resp.status_code}',
                'output_modality': 'audio_speech'}
    except Exception as e:
        return {'status': 'error', 'error': str(e),
                'output_modality': 'audio_speech'}


def _generate_audio_music(context: str, input_text: str,
                          duration: int, style: str) -> dict:
    """Route to AceStep for AI music generation."""
    prompt = input_text or context
    if style:
        prompt = f"[{style}] {prompt}"

    base_url = _get_tool_base_url('acestep')
    if not base_url:
        # Try default URL
        base_url = 'http://localhost:8001'

    try:
        from core.http_pool import pooled_post
        payload = {
            'prompt': prompt,
            'duration': duration or 30,
        }
        if style:
            payload['genre'] = style
        resp = pooled_post(
            f"{base_url}/release_task",
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            task_id = data.get('task_id', '')
            return {
                'status': 'pending',
                'output_modality': 'audio_music',
                'task_id': f'acestep_{task_id}',
                'poll_tool': 'check_media_status',
                'message': 'Music generation started. Use check_media_status(task_id) to check progress.',
                'model_used': 'acestep',
            }
        return {'status': 'error', 'error': f'AceStep HTTP {resp.status_code}',
                'output_modality': 'audio_music'}
    except Exception as e:
        return {'status': 'error', 'error': str(e),
                'output_modality': 'audio_music'}


def _generate_video(context: str, input_text: str,
                    duration: int, style: str, model: str) -> dict:
    """Route to wan2gp or ltx2 for video generation."""
    prompt = input_text or context
    if style:
        prompt = f"{prompt}, {style}"

    # Select tool
    tool = model if model != 'auto' else _select_video_tool()

    if tool == 'wan2gp':
        if not _ensure_tool_running('wan2gp'):
            # Fall back to ltx2
            tool = 'ltx2'

    if tool == 'wan2gp':
        return _generate_video_wan2gp(prompt, duration)
    else:
        return _generate_video_ltx2(prompt, duration)


def _generate_video_wan2gp(prompt: str, duration: int) -> dict:
    """Submit video generation to Wan2GP."""
    base_url = _get_tool_base_url('wan2gp')
    if not base_url:
        return {'status': 'error', 'error': 'Wan2GP not registered',
                'output_modality': 'video'}

    try:
        from core.http_pool import pooled_post
        # ~24fps, duration in seconds → frames
        num_frames = max(49, (duration or 2) * 24 + 1)
        resp = pooled_post(
            f"{base_url}/generate",
            json={'prompt': prompt, 'num_frames': num_frames,
                  'width': 512, 'height': 320},
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            task_id = data.get('task_id', '')
            return {
                'status': 'pending',
                'output_modality': 'video',
                'task_id': f'wan2gp_{task_id}',
                'poll_tool': 'check_media_status',
                'message': 'Video generation started. Use check_media_status(task_id) to check progress.',
                'model_used': 'wan2gp',
            }
        return {'status': 'error', 'error': f'Wan2GP HTTP {resp.status_code}',
                'output_modality': 'video'}
    except Exception as e:
        return {'status': 'error', 'error': str(e), 'output_modality': 'video'}


def _generate_video_ltx2(prompt: str, duration: int) -> dict:
    """Submit video generation to LTX2 server (port 5002)."""
    ltx_url = 'http://localhost:5002'
    try:
        from core.http_pool import pooled_post
        num_frames = max(49, (duration or 2) * 24 + 1)
        resp = pooled_post(
            f"{ltx_url}/generate",
            json={
                'prompt': prompt,
                'negative_prompt': 'worst quality, inconsistent motion, blurry',
                'num_frames': num_frames,
                'width': 832, 'height': 480,
                'num_inference_steps': 30,
                'guidance_scale': 3.0,
                'fps': 24,
            },
            headers={'Content-Type': 'application/json'},
            timeout=600,
        )
        if resp.status_code == 200:
            data = resp.json()
            video_url = (data.get('video_url') or data.get('output_url')
                         or data.get('video_path', ''))
            if video_url:
                return {
                    'status': 'completed',
                    'output_modality': 'video',
                    'results': [{'type': 'video', 'url': video_url,
                                 'format': 'mp4'}],
                    'model_used': 'ltx2',
                }
            # Async task pattern
            task_id = data.get('task_id', '')
            if task_id:
                return {
                    'status': 'pending',
                    'output_modality': 'video',
                    'task_id': f'ltx2_{task_id}',
                    'poll_tool': 'check_media_status',
                    'message': 'Video generation started. Use check_media_status(task_id).',
                    'model_used': 'ltx2',
                }
        return {'status': 'error', 'error': f'LTX2 HTTP {resp.status_code}',
                'output_modality': 'video'}
    except Exception as e:
        return {'status': 'error', 'error': str(e), 'output_modality': 'video'}


# ═══════════════════════════════════════════════════════════════
# Core AutoGen tool functions
# ═══════════════════════════════════════════════════════════════

def generate_media(
    context: Annotated[str, "What to generate — a natural language description"],
    output_modality: Annotated[str, (
        "Output type: 'image' | 'audio_speech' | 'audio_music' | "
        "'audio_speech_music' | 'video' | 'video_with_audio'"
    )],
    input_text: Annotated[Optional[str], "Text input (prompt, lyrics, script)"] = None,
    input_audio: Annotated[Optional[str], "Path to audio file (for voice cloning)"] = None,
    input_image: Annotated[Optional[str], "Path to image file (for img2vid)"] = None,
    model: Annotated[str, "Model: 'auto' or specific name"] = "auto",
    duration: Annotated[Optional[int], "Duration in seconds (audio/video)"] = None,
    style: Annotated[Optional[str], "Style hint (realistic, cartoon, cinematic)"] = None,
) -> str:
    """Unified media generation tool.

    Auto-selects the best available tool, auto-starts services if needed,
    and returns results in a consistent JSON format.
    """
    t0 = time.time()
    modality = output_modality.lower().strip()

    if modality not in VALID_MODALITIES:
        result = {
            'status': 'error',
            'error': f"Invalid output_modality '{output_modality}'. "
                     f"Valid: {sorted(VALID_MODALITIES)}",
        }
        result['generation_time_seconds'] = round(time.time() - t0, 2)
        return json.dumps(result)

    try:
        if modality == 'image':
            result = _generate_image(context, input_text, style)

        elif modality == 'audio_speech':
            result = _generate_audio_speech(context, input_text, duration)

        elif modality == 'audio_music':
            result = _generate_audio_music(context, input_text, duration, style)

        elif modality == 'audio_speech_music':
            # Generate both speech and music
            speech = _generate_audio_speech(context, input_text, duration)
            music = _generate_audio_music(context, input_text, duration, style)
            results = []
            if speech.get('status') == 'completed':
                results.extend(speech.get('results', []))
            if music.get('status') == 'completed':
                results.extend(music.get('results', []))
            # If music is pending, include task info
            if music.get('status') == 'pending':
                result = {
                    'status': 'partial',
                    'output_modality': 'audio_speech_music',
                    'results': results,
                    'pending_task_id': music.get('task_id'),
                    'poll_tool': 'check_media_status',
                    'message': 'Speech complete. Music generation pending.',
                }
            elif results:
                result = {
                    'status': 'completed',
                    'output_modality': 'audio_speech_music',
                    'results': results,
                }
            else:
                result = {
                    'status': 'error',
                    'output_modality': 'audio_speech_music',
                    'error': 'Both speech and music generation failed',
                    'speech_error': speech.get('error'),
                    'music_error': music.get('error'),
                }

        elif modality == 'video':
            result = _generate_video(context, input_text, duration, style, model)

        elif modality == 'video_with_audio':
            # Generate video + speech narration
            video = _generate_video(context, input_text, duration, style, model)
            speech = _generate_audio_speech(context, input_text, duration)
            results = []
            if video.get('status') == 'completed':
                results.extend(video.get('results', []))
            if speech.get('status') == 'completed':
                results.extend(speech.get('results', []))
            if video.get('status') == 'pending':
                result = {
                    'status': 'pending',
                    'output_modality': 'video_with_audio',
                    'task_id': video.get('task_id'),
                    'poll_tool': 'check_media_status',
                    'speech_results': speech.get('results', []),
                    'message': 'Video generation pending. Speech may be ready.',
                }
            elif results:
                result = {
                    'status': 'completed',
                    'output_modality': 'video_with_audio',
                    'results': results,
                }
            else:
                result = {
                    'status': 'error',
                    'output_modality': 'video_with_audio',
                    'error': 'Media generation failed',
                    'video_error': video.get('error'),
                    'speech_error': speech.get('error'),
                }
        else:
            result = {'status': 'error', 'error': f'Unhandled modality: {modality}'}

    except Exception as e:
        logger.error(f"generate_media error: {e}", exc_info=True)
        result = {'status': 'error', 'error': str(e)}

    elapsed = round(time.time() - t0, 2)
    result['generation_time_seconds'] = elapsed
    return json.dumps(result)


def check_media_status(
    task_id: Annotated[str, "Task ID from a pending generate_media call (e.g. 'wan2gp_abc123')"],
) -> str:
    """Check status of an async media generation task.

    Parses the tool prefix from task_id and queries the correct backend.
    Returns JSON with status, progress percentage, and URL when done.
    """
    if '_' not in task_id:
        return json.dumps({'status': 'error',
                           'error': f'Invalid task_id format: {task_id}'})

    tool_prefix, raw_id = task_id.split('_', 1)

    # Determine check endpoint
    if tool_prefix == 'wan2gp':
        base_url = _get_tool_base_url('wan2gp')
        check_path = '/check_result'
    elif tool_prefix == 'acestep':
        base_url = _get_tool_base_url('acestep')
        if not base_url:
            base_url = 'http://localhost:8001'
        check_path = '/query_result'
    elif tool_prefix == 'ltx2':
        base_url = 'http://localhost:5002'
        check_path = '/check_result'
    else:
        return json.dumps({'status': 'error',
                           'error': f'Unknown tool prefix: {tool_prefix}'})

    if not base_url:
        return json.dumps({'status': 'error',
                           'error': f'{tool_prefix} service not available'})

    try:
        from core.http_pool import pooled_post
        resp = pooled_post(
            f"{base_url}{check_path}",
            json={'task_id': raw_id},
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Normalize response
            status = data.get('status', 'unknown')
            result_url = (data.get('video_url') or data.get('audio_url')
                          or data.get('url') or data.get('output_url', ''))
            progress = data.get('progress', data.get('percentage', 0))

            out = {
                'task_id': task_id,
                'status': status,
                'progress': progress,
            }
            if status in ('completed', 'done', 'finished') and result_url:
                media_type = 'video' if tool_prefix in ('wan2gp', 'ltx2') else 'audio'
                out['results'] = [{'type': media_type, 'url': result_url}]
            return json.dumps(out)

        return json.dumps({'status': 'error',
                           'error': f'HTTP {resp.status_code}'})
    except Exception as e:
        return json.dumps({'status': 'error', 'error': str(e)})


# ═══════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════

def register_media_tools(helper, assistant):
    """Register generate_media + check_media_status as AutoGen tools.

    Called from create_recipe.py alongside other tool registrations.
    Follows the same pattern as register_marketing_tools().
    """
    tools = [
        (
            'generate_media',
            'Unified media generation: create images, speech, music, or video from text. '
            'Supports output_modality: image, audio_speech, audio_music, '
            'audio_speech_music, video, video_with_audio. Auto-selects best tool.',
            generate_media,
        ),
        (
            'check_media_status',
            'Check status of an async media generation task. '
            'Pass the task_id from a pending generate_media result.',
            check_media_status,
        ),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} media generation tools")
