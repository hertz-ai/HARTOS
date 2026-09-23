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
# Runtime capability introspection
# ═══════════════════════════════════════════════════════════════

def _can_do(model_type: str, capability: str = None) -> bool:
    """Universal capability check — delegates to orchestrator.

    Works for any model type or dynamic service category.
    Single source of truth: orchestrator merges catalog + services + runtime.
    """
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        return get_orchestrator().can_do(model_type, capability)
    except Exception:
        return False


def _node_has_any(model_type: str) -> bool:
    """Whether this node has ANY model of a type, fit to run or not.

    Deliberately ignores compute: the question is "is something installed",
    not "can it run this second".  _can_do() answers the second, and a
    caller that cannot tell them apart will offer to install what is
    already here.

    Asks the CATALOG, through the catalog's own public accessor.  This
    used to reach ``get_orchestrator()._catalog``, which is the same
    object -- the orchestrator is built with ``catalog or get_catalog()``
    -- but borrowed it from a component that has nothing to do with the
    question, through a private attribute the orchestrator never promised
    to keep.  The split is clean and worth keeping that way: the catalog
    owns "what is installed here", the orchestrator owns "what can run
    right now", and this function only ever wanted the first.
    """
    try:
        from integrations.service_tools.model_catalog import get_catalog
        return bool(get_catalog().list_by_type(model_type))
    except Exception as e:
        # Never silent: this decides whether a caller offers an install,
        # so "I could not tell" must be visible.  warning, not exception,
        # because the callers sit on per-segment paths and a traceback
        # per call would flood the log it is meant to inform.
        logger.warning("_node_has_any(%r): catalog unreadable (%s); "
                       "answering 'nothing installed'", model_type, e)
        return False


def _degraded_reason(model_type: str, what: str) -> str:
    """Why a segment was dropped, saying which of the two reasons it is.

    "Offline" reads as "you do not have this"; often the truth is "you have
    it and the GPU is busy".  A reviewer reading a degraded segment deserves
    to know which, because only one of them is worth installing anything for.
    """
    if _node_has_any(model_type):
        return f'{what} installed but cannot run right now (no free memory)'
    return f'{what} service offline'


# ═══════════════════════════════════════════════════════════════
# Auto-start helpers
# ═══════════════════════════════════════════════════════════════

def _start_tool(tool_name: str) -> dict:
    """Start a tool if it is not running, and keep the runtime's reason when
    it could not.

    The runtime answers a refusal with WHY -- 'Insufficient VRAM for acestep
    (free=4.9GB); try cpu_only', MEASURED 2026-09-22 with a 3 GB llama-server
    on the card -- and _ensure_tool_running folded that into a bare False, so
    a caller could only say "not running".  Same shape as the runtime's own
    answer: 'running', plus 'error' when it is not.
    """
    try:
        from integrations.service_tools.runtime_manager import runtime_tool_manager
        status = runtime_tool_manager.get_tool_status(tool_name)
        if status.get('running'):
            return {'running': True}
        result = runtime_tool_manager.setup_tool(tool_name) or {}
        if not result.get('running') and result.get('error'):
            # never silent: this is the line an operator greps for
            logger.warning(f"Auto-start of {tool_name} refused: {result['error']}")
        return result
    except Exception as e:
        logger.warning(f"Auto-start failed for {tool_name}: {e}")
        return {'running': False, 'error': str(e)}


def composer_output_dir():
    """Where a finished composition is kept on this node.

    AceStep saves into a TEMP dir and serves it only through its own
    sidecar, on a port assigned at start.  A game's memo must outlive both,
    so the poll copies the file here, and the node's existing audio route
    (/api/voice/audio, hart_intelligence_entry) serves it from here.  One
    answer, asked by both the writer and the route.
    """
    from integrations.service_tools.model_storage import model_storage
    return model_storage.get_tool_dir('acestep') / 'output'


def _keep_composition(file_value):
    """(node url, local path) for a finished AceStep file, or None.

    AceStep reports the file as `/v1/audio?path=<absolute temp path>`
    (MEASURED 2026-09-22), sometimes as the bare path.  Either way the
    bytes are on this machine; they are copied out of the temp dir so a
    replay next week still has them.
    """
    import os
    import shutil
    import urllib.parse
    src = str(file_value or '')
    if 'path=' in src:
        query = urllib.parse.urlparse(src).query
        src = (urllib.parse.parse_qs(query).get('path') or [''])[0]
    if not src or not os.path.isfile(src):
        logger.warning("_keep_composition: %r is not a file on this node", file_value)
        return None
    out = composer_output_dir()
    out.mkdir(parents=True, exist_ok=True)
    dest = out / os.path.basename(src)
    if not dest.exists():
        shutil.copy2(src, dest)
    return f'/api/voice/audio/{dest.name}', str(dest)


def _ensure_tool_running(tool_name: str) -> bool:
    """Auto-start a tool if it's not running. Returns True if available."""
    return bool(_start_tool(tool_name).get('running', False))


def populate_videogen_catalog(catalog) -> int:
    """Register all video generation model variants into the ModelCatalog.

    This is the single source of truth for video gen model names, VRAM
    thresholds, and capabilities — replacing the hardcoded free_gb >= 8.0
    threshold in _select_video_tool().

    Called by ModelCatalog._populate_videogen_models().
    Returns number of new entries added.
    """
    from integrations.service_tools.model_catalog import ModelEntry, ModelType

    # (id, name, vram_gb, ram_gb, disk_gb, quality, speed, min_tier,
    #  supports_cpu, supports_cpu_offload, caps)
    videogen_models = [
        (
            'video_gen-wan2gp', 'Wan2GP',
            8.0, 12.0, 15.0, 0.88, 0.65, 'full',
            False, False,
            {'txt2vid': True, 'img2vid': True, 'resolution': '512x320',
             'fps': 24, 'async_task': True},
        ),
        (
            'video_gen-ltx2', 'LTX-Video-2',
            4.0, 8.0, 10.0, 0.78, 0.78, 'standard',
            True, True,
            {'txt2vid': True, 'img2vid': False, 'resolution': '832x480',
             'fps': 24, 'async_task': True, 'cpu_offload': True},
        ),
    ]

    added = 0
    for (mid, name, vram, ram, disk, quality, speed, min_tier,
         sup_cpu, sup_offload, caps) in videogen_models:
        # Claiming skip -- see ModelCatalog.already_registered: an entry
        # this populator still owns but does not rewrite would otherwise
        # be swept as stale on every populate.
        if catalog.already_registered(mid):
            continue
        entry = ModelEntry(
            id=mid, name=name, model_type=ModelType.VIDEO_GEN,
            source='huggingface',
            vram_gb=vram, ram_gb=ram, disk_gb=disk,
            min_capability_tier=min_tier,
            backend='sidecar',
            supports_gpu=True, supports_cpu=sup_cpu,
            supports_cpu_offload=sup_offload,
            cpu_offload_method='restart_cpu' if sup_offload else 'none',
            idle_timeout_s=600,
            capabilities=caps,
            quality_score=quality, speed_score=speed,
            tags=['local', 'video_gen'],
        )
        catalog.register(entry, persist=False)
        added += 1
    return added


def populate_audiogen_catalog(catalog) -> int:
    """Register audio generation models (music, singing) into ModelCatalog.

    Same pattern as populate_videogen_catalog — capabilities-based routing
    so the orchestrator can select the right model for music/singing tasks.

    Called by ModelCatalog._populate_audiogen_models().
    Returns number of new entries added.
    """
    from integrations.service_tools.model_catalog import ModelEntry, ModelType

    audiogen_models = [
        (
            'audio_gen-acestep', 'ACE-Step 1.5',
            6.0, 6.0, 4.0, 0.85, 0.90, 'standard',
            False, False,
            {'music_gen': True, 'singing': True, 'lyrics_input': True,
             'genre_control': True, 'tempo_control': True,
             'max_duration_s': 120, 'async_task': True},
        ),
        (
            'audio_gen-diffrhythm', 'DiffRhythm v1.2',
            4.0, 4.0, 3.0, 0.80, 0.75, 'standard',
            True, True,
            {'music_gen': False, 'singing': True, 'singing_voice': True,
             'lyrics_input': True, 'voice_conversion': True,
             'max_duration_s': 60, 'async_task': False},
        ),
    ]

    added = 0
    for (mid, name, vram, ram, disk, quality, speed, min_tier,
         sup_cpu, sup_offload, caps) in audiogen_models:
        # Claiming skip -- see ModelCatalog.already_registered: an entry
        # this populator still owns but does not rewrite would otherwise
        # be swept as stale on every populate.
        if catalog.already_registered(mid):
            continue
        entry = ModelEntry(
            id=mid, name=name, model_type=ModelType.AUDIO_GEN,
            source='huggingface',
            vram_gb=vram, ram_gb=ram, disk_gb=disk,
            min_capability_tier=min_tier,
            backend='sidecar',
            supports_gpu=True, supports_cpu=sup_cpu,
            supports_cpu_offload=sup_offload,
            cpu_offload_method='restart_cpu' if sup_offload else 'none',
            idle_timeout_s=600,
            capabilities=caps,
            quality_score=quality, speed_score=speed,
            tags=['local', 'audio_gen'],
        )
        catalog.register(entry, persist=False)
        added += 1
    return added


def _select_audio_tool(task: str = 'music') -> str:
    """Select the best audio generation tool for a task.

    Consults ModelCatalog via orchestrator — same pattern as _select_video_tool.
    task: 'music' → ACE Step, 'sing'/'lyrics' → DiffRhythm (fallback ACE Step)
    """
    cap_key = 'singing_voice' if task in ('sing', 'lyrics') else 'music_gen'
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        entry = get_orchestrator().select_best(
            'audio_gen', require_capability={cap_key: True})
        if entry:
            _CATALOG_TO_TOOL = {
                'audio_gen-acestep':    'acestep',
                'audio_gen-diffrhythm': 'diffrhythm',
            }
            tool = _CATALOG_TO_TOOL.get(entry.id)
            if tool:
                return tool
    except Exception:
        logger.exception("_select_audio_tool: swallowed Exception")
    # Fallback defaults
    return 'diffrhythm' if task in ('sing', 'lyrics') else 'acestep'


def _select_video_tool() -> str:
    """Select the best video generation tool for current hardware.

    Consults ModelCatalog (single source of truth for VRAM thresholds).
    Falls back to direct VRAM query if catalog is unavailable.

    Returns 'wan2gp' or 'ltx2'.
    """
    # ── Primary path: ask the catalog/orchestrator ───────────────────────────
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        entry = get_orchestrator().select_best('video_gen')
        if entry:
            # Map catalog ID → tool name used by service_tool_registry
            _CATALOG_TO_TOOL = {
                'video_gen-wan2gp': 'wan2gp',
                'video_gen-ltx2':   'ltx2',
            }
            tool = _CATALOG_TO_TOOL.get(entry.id)
            if tool:
                return tool
    except Exception:
        logger.exception("_select_video_tool: swallowed Exception")

    # ── Fallback: direct VRAM query ──────────────────────────────────────────
    try:
        from integrations.service_tools.vram_manager import vram_manager
        info = vram_manager.detect_gpu()
        free_gb = info.get('free_gb', 0)
        if free_gb >= 8.0:
            return 'wan2gp'
    except Exception:
        logger.exception("_select_video_tool: swallowed Exception")
    return 'ltx2'


#: AceStep answers with an INTEGER status, not a word
#: (acestep/api/server_utils.py:10 STATUS_MAP, and map_status() maps anything
#: unrecognised to 2 == failed). Without this translation a finished
#: composition came back as `1`, matched none of the completed words, and read
#: as unfinished -- a second way for a real outcome to be unreadable.
_ACESTEP_STATUS_CODE = {0: 'processing', 1: 'succeeded', 2: 'failed'}

#: Every status check_media_status reports for a job that FAILED and will not
#: recover -- the one definition every poller tests against. check_media_status
#: turns an AceStep failure, a "succeeded but saved nothing" and an unknown task
#: into the house ``'error'``; the video sidecars pass their own ``'failed'``
#: through. A caller that tested only one of them polled a dead job to its
#: deadline: Nunba's tts_engine branched on ``== 'failed'`` and spent 120 s on
#: every failed composition (finding N1, 2026-09-23). Test ``in`` this, never a
#: literal, so the pollers cannot drift apart again.
MEDIA_FAILED_STATUSES = frozenset({'failed', 'error'})


def _unwrap_envelope(payload, task_id: str = '') -> dict:
    """The answer itself, whether or not the sidecar wrapped it.

    AceStep replies {'data': {...}, 'code': 200, 'error': None}; wan2gp and
    the TTS suite reply flat. Reading the top level of an ENVELOPED answer
    finds nothing and says so quietly, which is exactly how this failed:

      * the submit read task_id from the envelope, got '', and produced the
        id 'acestep_' -- so every composition was accepted, generated, and
        then unpollable and unclaimable. MEASURED tonight: the server
        answered 200 with a real task_id and the caller kept none of it.
      * the poll read status from the envelope and answered 'unknown'
        forever, so nothing ever completed.

    Together they meant a node could compose perfectly and no game would
    ever hear a note of it.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get('data')
    if isinstance(inner, dict) and ('task_id' in inner or 'status' in inner):
        return inner
    if isinstance(inner, list):
        # /query_result is a BATCH endpoint: wrap_response(data_list) puts a
        # LIST under 'data', one item per requested id
        # (acestep/api/http/query_result_route.py:66). Reading the outer
        # envelope found no 'status' and answered 'unknown' for every
        # outcome -- completed and failed alike.
        if task_id:
            for item in inner:
                if isinstance(item, dict) and item.get('task_id') == task_id:
                    return item
        if len(inner) == 1 and isinstance(inner[0], dict):
            return inner[0]
        return {}
    return payload


def _get_tool_base_url(tool_name: str) -> Optional[str]:
    """Where a tool is actually listening, asked of both things that know.

    There are TWO registries and they hold different tools:

      * ServiceToolRegistry -- tools declared in service_tools.json,
        including any an operator started by hand and registered.
      * RuntimeToolManager  -- the sidecars HARTOS starts itself. It
        assigns the port at launch (63734 for AceStep on 2026-09-21) and
        learns it from the child's PORT= line.

    Asking only the first meant every runtime-started sidecar resolved to
    None, and the caller reported "not running" about a process that was
    serving happily on a port this machine knew. MEASURED: get_tool_status
    said running on 63734 while this returned None.
    """
    try:
        from integrations.service_tools.registry import service_tool_registry
        tool = service_tool_registry._tools.get(tool_name)
        if tool and getattr(tool, 'base_url', None):
            return tool.base_url.rstrip('/')
    except Exception:
        logger.exception("_get_tool_base_url: swallowed Exception")
    try:
        from integrations.service_tools.runtime_manager import (
            runtime_tool_manager)
        status = runtime_tool_manager.get_tool_status(tool_name) or {}
        port = status.get('port')
        if status.get('running') and port:
            return f'http://127.0.0.1:{port}'
    except Exception as e:
        logger.warning(f'_get_tool_base_url({tool_name}): runtime manager '
                       f'could not say where it is ({e})')
    return None


# ═══════════════════════════════════════════════════════════════
# Modality handlers
# ═══════════════════════════════════════════════════════════════

#: What an error from this module MEANS, told apart without a caller having
#: to read prose.  The three kinds are real and they want opposite answers:
#:
#:   'absent'      no tool is registered, or auto-start failed.  Nothing is
#:                 installed to do this here.  A caller may reasonably OFFER
#:                 TO INSTALL one (integrations.agent_engine.capability_setup).
#:   'unreachable' a tool IS configured but the call did not arrive -- the
#:                 process is not running, the port refused, it timed out.
#:                 Offering to install is wrong; it is installed.
#:   'refused'     the tool answered, and said no (an HTTP status).  The
#:                 capability exists and works; THIS request was rejected.
#:   'unknown'     none of the above matched.
ABSENT, UNREACHABLE, REFUSED, UNKNOWN = (
    'absent', 'unreachable', 'refused', 'unknown')

#: Wordings this module itself emits for "nothing is installed".  Kept HERE,
#: next to the returns that produce them, deliberately: a consumer that
#: matched these strings from another module would silently stop working the
#: day someone reworded a message, and the person rewording it would have no
#: way to know. Producer and reader change together in one file.
_ABSENT_MARKERS = ('not registered', 'not available', 'no tool',
                   'auto-start failed', 'not installed')

#: Exception text for "configured, but the call did not arrive".
_UNREACHABLE_MARKERS = ('connection refused', 'connectionerror', 'timed out',
                        'timeout', 'max retries', 'failed to establish',
                        'actively refused', 'connection aborted',
                        'name or service not known', 'getaddrinfo',
                        # installed, but will not fit in memory this instant
                        'cannot run right now',
                        # installed, but its sidecar has no registered port
                        'is not running')


def classify_error(result) -> str:
    """Why a generate_media call failed, as one of the four constants above.

    THE POINT: callers must not grep this module's prose for themselves.
    Deciding whether to offer the owner an install is a real branch -- asking
    someone to install what is already installed is its own defect -- and it
    hung on wording that lives in another file. This puts the reader beside
    the writer so they move together.

    Takes the dict generate_media returns (or its JSON string). Anything that
    is not an error answers UNKNOWN rather than guessing.
    """
    if isinstance(result, str):
        try:
            import json as _json
            result = _json.loads(result)
        except Exception:
            return UNKNOWN
    if not isinstance(result, dict):
        return UNKNOWN
    # 'unavailable' counts as an error to read.  The modality gate in
    # generate_media returns status='unavailable' (not 'error') when this
    # node cannot do a modality at all -- which is the PRINCIPAL absent case
    # and the one a caller most needs to tell apart, e.g. asking for music on
    # a machine with no music engine.  Reading only status=='error' answered
    # UNKNOWN there, so the caller could not offer to install what is plainly
    # not installed.  (Found by rn-1 wiring the game-sound install offer.)
    if result.get('status') not in ('error', 'unavailable'):
        return UNKNOWN

    text = str(result.get('error', '')).lower()
    if not text:
        return UNKNOWN
    # 'absent' first: "TTS-Audio-Suite not available" would otherwise look
    # like a transport failure to a naive substring pass.
    if any(m in text for m in _ABSENT_MARKERS):
        return ABSENT
    if any(m in text for m in _UNREACHABLE_MARKERS):
        return UNREACHABLE
    # "<Tool> HTTP 503" -- it answered, so it exists and it said no.
    if 'http' in text and any(c.isdigit() for c in text):
        return REFUSED
    return UNKNOWN


def _generate_image(context: str, input_text: str, style: str) -> dict:
    """Route to txt2img external service."""
    prompt = input_text or context
    if style:
        prompt = f"{prompt}, {style} style"
    try:
        from hartos.helper import txt2img
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

    # The speech and video paths start their sidecar before dialing it.  This
    # one dialed straight away, so a composer that was installed and merely
    # not up answered "not running" to every game, for ever (run 8,
    # 2026-09-22 -- the proof scripts had been starting it by hand).
    started = _start_tool('acestep')
    if not started.get('running'):
        why = str(started.get('error') or 'auto-start failed')
        if _node_has_any('audio_gen'):
            # Installed and will not fit this instant.  Worded so that
            # classify_error reads UNREACHABLE, never ABSENT: offering to
            # install what is on the disk is its own defect.
            return {'status': 'error',
                    'error': f'AceStep installed but cannot run right now ({why})',
                    'output_modality': 'audio_music'}
        return {'status': 'error',
                'error': f'AceStep not available and auto-start failed ({why})',
                'output_modality': 'audio_music'}

    base_url = _get_tool_base_url('acestep')
    if not base_url:
        # No pinned fallback.  Sidecar ports are assigned at start (this node
        # put AceStep on 51168 on 2026-09-21), so dialing a literal 8001
        # reaches nothing -- or, worse, something else that happens to be
        # listening.  If the registry has no port, the service is not up, and
        # saying so is the honest answer.
        return {'status': 'error',
                'error': 'AceStep service is not running (no port registered '
                         'on this node).'}

    try:
        from core.http_pool import pooled_post
        payload = {
            'prompt': prompt,
            # AceStep reads 'audio_duration' (release_task_models.py:48), not
            # 'duration'.  MEASURED 2026-09-22: sending 'duration' was silently
            # ignored and every game cue came back at the model's 60s default
            # -- a minute-long "correct answer" chime.
            'audio_duration': duration or 30,
            # WAV, not AceStep's default of mp3.  MEASURED 2026-09-22: the
            # composition SUCCEEDS ("Done! Generated 2 audio tensors",
            # normalised to peak 0.89) and then the save fails with
            # "ffmpeg executable not found -- MP3 export failed without
            # fallback", so the finished music is discarded at the last
            # step.  ffmpeg is not on PATH on this box and is not one of
            # the tool's declared dependencies; wav needs no encoder at
            # all.  A game can play wav, and the memo stores a path, so
            # nothing downstream cares which of the two it is.
            'audio_format': 'wav',
        }
        if style:
            payload['genre'] = style
        resp = pooled_post(
            f"{base_url}/release_task",
            json=payload,
            headers={'Content-Type': 'application/json'},
            # /release_task only ENQUEUES -- it answers with a task_id and the
            # composing happens in the background.  But the FIRST call to a
            # cold sidecar also waits for the model to load, and 30s does not
            # cover that: MEASURED on this box, a first request timed out at
            # 30s against a server that was alive and loading (the read timed
            # out; the connection did not refuse).  The caller then reports a
            # failure for work that IS proceeding, and the task_id in the
            # answer we never read is lost, so the composition can never be
            # polled or claimed -- it runs to completion for nobody.  Matches
            # the video submit beside it, which learned this already.
            timeout=120,
        )
        if resp.status_code == 200:
            data = _unwrap_envelope(resp.json())
            task_id = data.get('task_id', '')
            if not task_id:
                return {'status': 'error',
                        'error': 'AceStep accepted the job but named no task, '
                                 'so it could never be collected.',
                        'output_modality': 'audio_music'}
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
        # A READ timeout is not a failure: the connection was accepted, so
        # the server is there and busy.  On a node composing for the FIRST
        # time that busy-ness is an ~8.5GB weight download (MEASURED on this
        # box: model.safetensors 3.71GB + 4.79GB at ~15MB/s, so ten minutes
        # before it can answer anything).  Reported as an error, the agent
        # tells the person their composer refused when it is in fact getting
        # ready -- and the game is left silent with nothing pending.
        if _reads_as_still_waking(e):
            return {
                'status': 'warming_up',
                'output_modality': 'audio_music',
                'message': 'The composer is starting up (a first run '
                           'downloads its model). Ask again shortly.',
                'model_used': 'acestep',
            }
        return {'status': 'error', 'error': str(e),
                'output_modality': 'audio_music'}


def _reads_as_still_waking(error) -> bool:
    """True when a request failed because the server is busy, not absent.

    A read timeout means the TCP connection was ACCEPTED and no answer came
    back in time -- something is listening. A connection refusal means
    nothing is. Only the first deserves "wait and ask again".
    """
    said = str(error).lower()
    if 'refused' in said or 'no connection could be made' in said:
        return False
    # A RESET is also "busy", not "gone".  MEASURED 2026-09-22: the server
    # dropped the connection at 09:42 while loading its vae and text
    # encoder, and at 09:43:30 logged "Generating audio... (DiT backend:
    # PyTorch (cuda))" and carried on to completion.  Reported as a failure,
    # the caller abandoned a composition that was working -- and the memo
    # never filled for a sound that did get made.
    return ('read timed out' in said
            or 'readtimeout' in said
            or 'connection aborted' in said
            or 'connectionreset' in said
            or '10054' in said)


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
        result = _generate_video_wan2gp(prompt, duration)
        if result.get('status') != 'error':
            return result
        # wan2gp answers /generate with 501 -- it has no adapter to the
        # upstream repo, which ships a Gradio app and no generate API.
        # The selector still picks it on any card with >=8 GB free, so a
        # ladder that stopped at the first engine turned a servable
        # request into an error.  Fall through exactly as the
        # _ensure_tool_running failure above already does.
        logger.warning(
            "wan2gp video generation unavailable (%s); falling back to ltx2",
            result.get('error'))
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
    """Submit video generation to the LTX2 server.

    The base URL comes from the registry, as _generate_video_wan2gp's does.
    It used to be a literal localhost:5002, which cannot be
    right: RuntimeToolManager gives every sidecar an OS-assigned port and
    learns it from the server's PORT= line (MEASURED 2026-09-21: 65523 on
    one run, 64507 on the next), so 5002 named whatever else happened to
    hold it.  The literal survives only as the fallback for a server an
    no fallback literal: dialing a port the registry does not know is
    the same blind dial that made AceStep's music path fail against
    8001 while its sidecar sat on 51168.  An unregistered service is
    not running, and that is what a caller is told.
    """
    ltx_url = _get_tool_base_url('ltx2')
    if not ltx_url:
        return {'status': 'error',
                'error': 'LTX2 service is not running (no port registered '
                         'on this node).'}
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

    Runtime capability-aware: checks what this node can do before attempting.
    Returns clear guidance when a modality is unavailable (not cryptic errors).
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

    # Runtime capability gate — universal orchestrator check
    _MODALITY_TO_CHECK = {
        'audio_speech': ('tts', None),
        'audio_speech_music': ('tts', None),
        'audio_music': ('audio_gen', 'music_gen'),
        'video': ('video_gen', 'txt2vid'),
        'video_with_audio': ('video_gen', 'txt2vid'),
        'image': ('image_gen', None),
    }
    check = _MODALITY_TO_CHECK.get(modality)
    if check and not _can_do(*check):
        # can_do() is "loaded OR can_load", and can_load drops any model that
        # will not fit in the memory free AT THIS INSTANT.  So a fully
        # installed engine reads as unavailable while the GPU is busy.  Those
        # are different problems with different answers -- one is "install
        # something", the other is "wait or free memory" -- so say which.
        # Conflating them made a caller offer to install what was already
        # installed, which is the defect classify_error exists to prevent.
        installed = _node_has_any(check[0])
        return json.dumps({
            'status': 'unavailable',
            'error': (
                f'{modality} is installed on this node but cannot run right '
                f'now (not enough free memory).' if installed else
                f'{modality} not available on this node right now.'),
            'modality': modality,
            'suggestion': f'Describe the {modality.replace("_", " ")} in text instead, '
                          f'or delegate to a node with {check[0]} capability.',
        })

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

    # Feed generation result to HevolveAI for dense error signal learning
    # Success: generated output becomes prediction target
    # Error: error pattern informs modality routing confidence
    try:
        from integrations.agent_engine.world_model_bridge import get_world_model_bridge
        bridge = get_world_model_bridge()
        bridge.submit_output_feedback(
            output_modality=result.get('output_modality', modality),
            status=result.get('status', 'error'),
            context=context[:2000],
            model_used=result.get('model_used', 'unknown'),
            error_message=result.get('error'),
            generation_time_seconds=elapsed,
        )
    except Exception as e:
        logger.debug(f"[MediaAgent] Output feedback to HevolveAI skipped: {e}")

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
        check_path = '/query_result'
    elif tool_prefix == 'ltx2':
        base_url = _get_tool_base_url('ltx2')
        check_path = '/check_result'
    else:
        return json.dumps({'status': 'error',
                           'error': f'Unknown tool prefix: {tool_prefix}'})

    if not base_url:
        # "not running", not "not available": the tool is installed, its
        # service simply has no registered port, and a caller must not be
        # told to install what is already here.
        return json.dumps({'status': 'error',
                           'error': f'{tool_prefix} service is not running '
                                    f'(no port registered on this node).'})

    try:
        from core.http_pool import pooled_post
        # AceStep's /query_result reads 'task_id_list' and parses a missing
        # key as '[]' (query_result_route.py:57 -> parse_task_id_list), so a
        # bare 'task_id' asked about NO tasks: the answer was an empty batch
        # and every state -- completed, failed -- read as 'unknown'. The
        # other two tools use /check_result, a different contract.
        body = ({'task_id_list': [raw_id]} if tool_prefix == 'acestep'
                else {'task_id': raw_id})
        resp = pooled_post(
            f"{base_url}{check_path}",
            json=body,
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        if resp.status_code == 200:
            data = _unwrap_envelope(resp.json(), task_id=raw_id)
            if not data:
                # The server answered, and said nothing about this id. That is
                # not a state to report -- inventing one is what hid the bug.
                return json.dumps({
                    'status': 'error',
                    'error': f'{tool_prefix} knows nothing about task {raw_id}',
                })
            # Normalize response
            status = data.get('status', 'unknown')
            if tool_prefix == 'acestep' and isinstance(status, int):
                status = _ACESTEP_STATUS_CODE.get(status, 'failed')
            # AceStep does not put the artifact at the top of the item.  Its
            # finished item is {"task_id", "status": <int>, "result": "<JSON
            # STRING of a list>", "progress_text"} and the path lives INSIDE
            # that string at [0]["file"] (query_result_service.py:
            # _build_store_result_payload).  MEASURED 2026-09-22: two WAVs
            # saved at 10:34:30, and forty polls over the next seventeen
            # minutes all read "composing" because this looked for a flat
            # url key that does not exist.  The nested item also carries
            # "error" and "stage" for an unfinished or failed task.
            nested = {}
            if isinstance(data.get('result'), str) and data['result'].strip():
                try:
                    items = json.loads(data['result'])
                    if isinstance(items, list) and items and isinstance(items[0], dict):
                        nested = items[0]
                except Exception as e:
                    logger.debug('check_media_status: result not JSON: %s', e)
            if nested.get('error') and not data.get('error'):
                data['error'] = nested['error']
            result_url = (data.get('video_url') or data.get('audio_url')
                          or data.get('url') or data.get('output_url')
                          or data.get('audio_path') or data.get('file_path')
                          or nested.get('file') or '')
            progress = data.get('progress', data.get('percentage', 0))

            out = {
                'task_id': task_id,
                'status': status,
                'progress': progress,
            }
            # A failure must CARRY its reason.  The agent's poll reads
            # progress['error'] to decide between "offer to install" and
            # "the composer refused"; a bare status of 'failed' with no
            # error field reads as "unknown reason" and neither branch
            # can act on it.  AceStep puts the reason inside the nested
            # result item (surfaced into data['error'] above).
            if status == 'failed' or data.get('error'):
                out['status'] = 'error'
                out['error'] = str(data.get('error') or
                                   f'{tool_prefix} reported failure')
                return json.dumps(out)
            # An ARTIFACT is the completion signal, whatever the status
            # vocabulary says.  MEASURED 2026-09-22: AceStep answered
            # status=1 -- a numeric code, not one of the words this list
            # knows -- so a finished job read as unfinished and a caller
            # polled it forever.  I did not guess what 1 means; a result
            # url is unambiguous in a way a status enum I cannot find the
            # definition of is not.
            if result_url:
                status = 'completed'
            _done_words = ('completed', 'complete', 'done', 'finished',
                           'success', 'succeeded')
            if status in _done_words and result_url and tool_prefix == 'acestep':
                # AceStep's own value is a sidecar-relative temp path that
                # nothing off the sidecar can fetch (hartos-3a F1): keep the
                # file, report the node's url for it.
                kept = _keep_composition(result_url)
                if kept:
                    out['results'] = [{'type': 'audio', 'url': kept[0],
                                       'path': kept[1]}]
                    out['status'] = 'completed'
                else:
                    out['status'] = 'error'
                    out['error'] = ('acestep finished but its file is not on '
                                    f'this node: {result_url}')
            elif status in _done_words and result_url:
                media_type = 'video' if tool_prefix in ('wan2gp', 'ltx2') else 'audio'
                out['results'] = [{'type': media_type, 'url': result_url}]
                out['status'] = 'completed'
            elif status in _done_words:
                # Succeeded with NOTHING SAVED. This really happens: AceStep
                # generated two audio tensors and then "MP3 export failed
                # without fallback: ffmpeg executable not found" threw them
                # away, while still reporting success. Reporting 'completed'
                # here would tell the caller it has music it was never given
                # -- the same "cannot tell done from nothing" this whole fix
                # is about.
                out['status'] = 'error'
                out['error'] = (
                    f'{tool_prefix} reported success but saved no artifact '
                    f'(a missing encoder does this: mp3/opus/aac need ffmpeg '
                    f'on PATH; wav and flac do not)')
            return json.dumps(out)

        return json.dumps({'status': 'error',
                           'error': f'HTTP {resp.status_code}'})
    except Exception as e:
        return json.dumps({'status': 'error', 'error': str(e)})


# ═══════════════════════════════════════════════════════════════
def synthesize_multilingual_audio(
    text: Annotated[str, (
        "Text to synthesize. May contain multiple languages (auto-detected by script) "
        "and media tags: <music genre='jazz' duration='10'>prompt</music>, "
        "<sing duration='15'>lyrics</sing>, <lyrics>song text</lyrics>. "
        "Each segment is routed to the best available engine."
    )],
    output_path: Annotated[Optional[str], "Path for output WAV. Auto-generated if omitted."] = None,
    task_id: Annotated[Optional[str], "Agent ledger task_id for pause/resume tracking."] = None,
) -> str:
    """Synthesize mixed-language text + media tags into one audio file.

    Compute-aware: uses ModelOrchestrator to select the best model per
    segment. Agents can pause/resume via the agent_ledger task_id.
    Returns JSON with status, output_path, and degraded_segments (if any
    segment type was unavailable on this node).
    """
    # Runtime capability gate: check what this node can actually do.
    # Same two meanings as the modality gate above, and this one carries the
    # voice of every spoken turn: a node whose GPU is merely busy must not
    # read as a node with no speech engine, or a caller offers to install
    # what is already installed.
    if not _can_do('tts'):
        installed = _node_has_any('tts')
        return json.dumps({
            'status': 'unavailable',
            'error': (
                'Audio synthesis is installed on this node but cannot run '
                'right now (not enough free memory).' if installed else
                'Audio synthesis not available on this node (text-only mode).'),
            'suggestion': ('Wait for the current work to finish, or return '
                           'text content directly.' if installed else
                           'Return text content directly — the user will read it.'),
        })

    try:
        from tts.tts_engine import get_tts_engine
        engine = get_tts_engine()
        if not engine:
            return json.dumps({'status': 'error', 'error': 'TTS engine not available'})

        from tts.language_segmenter import segment
        segments = segment(text)
        if not segments:
            return json.dumps({'status': 'error', 'error': 'No segments found in text'})

        # Filter out segment types this node can't handle, report them
        degraded = []
        runnable = []
        for seg in segments:
            seg_type = seg.get('type', 'speech')
            if seg_type == 'speech':
                runnable.append(seg)
            elif seg_type in ('music',) and not _can_do('audio_gen', 'music_gen'):
                degraded.append({'type': seg_type, 'text': seg.get('text', ''),
                                 'reason': _degraded_reason('audio_gen',
                                                            'music gen')})
            elif seg_type in ('sing', 'lyrics') and not _can_do('audio_gen', 'singing'):
                degraded.append({'type': seg_type, 'text': seg.get('text', ''),
                                 'reason': _degraded_reason('audio_gen',
                                                            'singing voice')})
            else:
                runnable.append(seg)

        result = engine._synthesize_multilingual(
            runnable, output_path=output_path, task_id=task_id) if runnable else None

        resp = {
            'status': 'completed' if result else 'partial',
            'output_path': result,
            'segments_total': len(segments),
            'segments_synthesized': len(runnable),
            'segment_types': [s.get('type', 'speech') for s in runnable],
        }
        if degraded:
            resp['degraded_segments'] = degraded
            resp['status'] = 'partial'
        return json.dumps(resp)
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
        (
            'synthesize_multilingual_audio',
            'Synthesize mixed-language text into one audio file. '
            'Auto-detects languages by script (Tamil, Hindi, English, etc.) '
            'and routes each segment to the best TTS engine. '
            'Supports <music>, <sing>, <lyrics> tags for music/singing. '
            'Pass task_id for pause/resume via agent ledger.',
            synthesize_multilingual_audio,
        ),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} media generation tools")
