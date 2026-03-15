"""
Video Generation Orchestrator — HARTOS-native video pipeline.

Absorbs the orchestration logic from MakeItTalk's views_c.py (request parsing,
asset download/caching, text chunking, queue ETA, task dispatch) and dispatches
GPU-bound subtasks to hive mesh peers or local GPU servers.

MakeItTalk on a no-GPU VM is a dead service — HARTOS handles the orchestration
and routes GPU work to whoever has a GPU (local, hive peer, or cloud).

Pipeline:
  1. Parse request (avatar, voice, text, flags)
  2. Download/cache image + audio assets
  3. Chunk text for streaming playback
  4. Dispatch GPU subtasks: TTS + face crop (parallel) → lip-sync (sequential)
  5. Publish per-chunk results to Crossbar pupit.{user_id} for real-time playback
  6. Return 202 with queue position + ETA

GPU subtasks dispatched via compute mesh:
  - audio_generation: Text → WAV (requires GPU for neural TTS)
  - crop_background_removal: Image → cropped face + bg removal (GPU optional)
  - lip_sync_generation: Audio + Image → video (requires GPU)
  - hd_upscale: Video → HD video (requires GPU, optional)
"""
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.video_orchestrator')

# ─── Constants ────────────────────────────────────────────

# Text chunking parameters (from MakeItTalk merge_sentences)
MIN_CHUNK_LEN = 50
MAX_CHUNK_LEN = 60

# Hallo max duration (seconds) — longer clips fall back to MakeItTalk pipeline
HALLO_MAX_DURATION = 24

# Queue ETA buffer (seconds)
QUEUE_BUFFER_TIME = 300

# Asset download timeout
ASSET_DOWNLOAD_TIMEOUT = 30

# Default time-per-second of audio for queue estimation
# MakeItTalk uses duration * 60 as processing time estimate
PROCESSING_TIME_FACTOR = 60

# Asset cache directory
ASSET_CACHE_DIR = os.path.join(
    os.environ.get('HEVOLVE_DATA_DIR', os.path.expanduser('~/.hevolve')),
    'cache', 'video_assets',
)


# ─── Text Chunking ───────────────────────────────────────

def _sent_tokenize(text: str) -> List[str]:
    """Split text into sentences. Uses nltk if available, else regex fallback."""
    try:
        from nltk.tokenize import sent_tokenize
        return sent_tokenize(text)
    except ImportError:
        # Regex fallback — split on sentence-ending punctuation
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        return [s for s in sentences if s.strip()]


def merge_sentences(sentences: List[str],
                    min_len: int = MIN_CHUNK_LEN,
                    max_len: int = MAX_CHUNK_LEN) -> List[str]:
    """Merge short sentences into chunks of min_len..max_len characters.

    Ported from MakeItTalk's merge_sentences() utility.
    Ensures each chunk is long enough for natural TTS output.
    """
    if not sentences:
        return []

    chunks = []
    current = ''

    for sent in sentences:
        candidate = (current + ' ' + sent).strip() if current else sent.strip()

        if len(candidate) > max_len and current:
            # Current chunk is full — flush it
            chunks.append(current.strip())
            current = sent.strip()
        else:
            current = candidate

    if current.strip():
        # Merge short trailing chunk with previous if possible
        if chunks and len(current.strip()) < min_len:
            chunks[-1] = (chunks[-1] + ' ' + current).strip()
        else:
            chunks.append(current.strip())

    return chunks


def chunk_text(text: str) -> List[str]:
    """Split text into TTS-friendly chunks for streaming playback."""
    sentences = _sent_tokenize(text)
    if not sentences:
        return [text] if text.strip() else []
    return merge_sentences(sentences)


# ─── Asset Management ────────────────────────────────────

def _ensure_cache_dir():
    """Create asset cache directory if needed."""
    os.makedirs(ASSET_CACHE_DIR, exist_ok=True)
    os.makedirs(os.path.join(ASSET_CACHE_DIR, 'images'), exist_ok=True)
    os.makedirs(os.path.join(ASSET_CACHE_DIR, 'audio'), exist_ok=True)


def _cache_key(url: str) -> str:
    """Deterministic cache key from URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def download_asset(url: str, asset_type: str = 'images') -> Optional[str]:
    """Download and cache an asset (image or audio) from URL.

    Returns local file path, or None on failure.
    Uses content-hash caching — same URL returns cached file instantly.
    """
    if not url:
        return None

    _ensure_cache_dir()

    # Determine extension from URL
    ext = ''
    url_path = url.split('?')[0]
    if '.' in url_path.split('/')[-1]:
        ext = '.' + url_path.split('/')[-1].rsplit('.', 1)[-1]
    if not ext:
        ext = '.png' if asset_type == 'images' else '.wav'

    cache_file = os.path.join(ASSET_CACHE_DIR, asset_type,
                              _cache_key(url) + ext)

    # Return cached file if exists
    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
        return cache_file

    try:
        from core.http_pool import pooled_get
        resp = pooled_get(url, timeout=ASSET_DOWNLOAD_TIMEOUT)
        if resp.status_code == 200:
            with open(cache_file, 'wb') as f:
                f.write(resp.content)
            return cache_file
    except Exception as e:
        logger.warning("Asset download failed (%s): %s", url, e)

    return None


# ─── Queue Estimation ────────────────────────────────────

def estimate_audio_duration(text: str) -> float:
    """Estimate audio duration from text length (seconds).

    Average speaking rate: ~150 words/minute = 2.5 words/second.
    """
    words = len(text.split())
    return max(2.0, words / 2.5)


def calculate_queue_eta(queue_depth: int, audio_duration: float) -> dict:
    """Calculate queue position and ETA for a video generation request.

    Ported from MakeItTalk's queue estimation logic.

    Returns:
        {
            'total_jobs_in_queue': int,
            'position': int,
            'estimated_seconds': int,
            'soft_time_limit': int,
            'hard_time_limit': int,
        }
    """
    position = queue_depth + 1
    time_per_job = audio_duration * PROCESSING_TIME_FACTOR
    estimated = int(position * time_per_job + QUEUE_BUFFER_TIME)
    soft_limit = estimated
    hard_limit = soft_limit + QUEUE_BUFFER_TIME

    return {
        'total_jobs_in_queue': queue_depth,
        'position': position,
        'estimated_seconds': estimated,
        'soft_time_limit': soft_limit,
        'hard_time_limit': hard_limit,
    }


# ─── Request Parsing ─────────────────────────────────────

class VideoGenRequest:
    """Parsed video generation request.

    Normalizes the various parameter formats from MakeItTalk / chatbot_pipeline
    into a clean internal representation.
    """

    def __init__(self, data: dict):
        self.uid = data.get('uid') or f"{uuid.uuid4().hex[:8]}_{data.get('user_id', 'anon')}"
        self.user_id = str(data.get('user_id', ''))
        self.publish_id = str(data.get('publish_id', self.user_id))
        self.text = data.get('text', '')

        # Voice configuration
        self.voice_name = data.get('voiceName', data.get('voice_name', ''))
        self.gender = data.get('gender', 'male')
        self.openvoice = _to_bool(data.get('openvoice', False))
        self.chattts = _to_bool(data.get('chattts', False))
        self.kokuro = _to_bool(data.get('kokuro', False))

        # Image/avatar configuration
        self.avatar_id = data.get('avatar_id', '')
        self.image_url = data.get('image_url', '')
        self.audio_sample_url = data.get('audio_sample_url', '')

        # Processing flags
        self.flag_hallo = _to_bool(data.get('flag_hallo', False))
        self.hd_video = _to_bool(data.get('hd_vid', data.get('hd_video', False)))
        self.vtoonify = _to_bool(data.get('vtoonify', False))
        self.remove_bg = _to_bool(data.get('remove_bg', True))
        self.crop = _to_bool(data.get('crop', True))
        self.is_premium = _to_bool(data.get('is_premium', data.get('premium', False)))
        self.chunking = _to_bool(data.get('chunking', False))

        # Background options
        self.inpainting = _to_bool(data.get('inpainting', False))
        self.inpainting_prompt = data.get('prompt', '')
        self.gradient = _to_bool(data.get('gradient', False))
        self.solid_color = _to_bool(data.get('solid_color', False))
        self.background_path = data.get('background_path', '')

        # Request metadata
        self.request_id = data.get('request_id', '')

    def validate(self) -> Optional[str]:
        """Validate request. Returns error string or None if valid."""
        if not self.text and not self.audio_sample_url:
            return 'Either text or audio_sample_url is required'
        if not self.image_url and not self.avatar_id:
            return 'Either image_url or avatar_id is required'
        return None


def _to_bool(val) -> bool:
    """Convert various truthy representations to bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes')
    return bool(val)


# ─── GPU Subtask Dispatch ────────────────────────────────

class SubtaskResult:
    """Result from a dispatched GPU subtask."""

    def __init__(self, success: bool = False, data: dict = None,
                 error: str = '', peer: str = 'local'):
        self.success = success
        self.data = data or {}
        self.error = error
        self.peer = peer


def _dispatch_gpu_subtask(
    task_type: str,
    task_params: dict,
    user_id: str = '',
    timeout: int = 300,
) -> SubtaskResult:
    """Dispatch a GPU subtask to local GPU or hive mesh peer.

    Follows the same routing chain as model_bus_service._route_video_gen():
      1. Local GPU server (health-cached, skip dead in 0ms)
      2. Hive mesh peer with GPU
      3. Error — no GPU available

    Args:
        task_type: 'audio_generation', 'crop_background', 'lip_sync', 'hd_upscale'
        task_params: Task-specific parameters dict
        user_id: For routing status publishing
        timeout: Max wait time (seconds)

    Returns:
        SubtaskResult with success/data/error
    """
    from core.port_registry import get_port

    # Map task types to local GPU endpoints
    local_endpoints = {
        'audio_generation': {
            'url': f"http://localhost:{get_port('tts_gpu', 5003)}",
            'path': '/synthesize',
        },
        'crop_background': {
            'url': f"http://localhost:{get_port('image_proc', 5004)}",
            'path': '/process',
        },
        'lip_sync': {
            'url': f"http://localhost:{get_port('lip_sync', 5005)}",
            'path': '/generate',
        },
        'hd_upscale': {
            'url': f"http://localhost:{get_port('video2x', 5006)}",
            'path': '/hd-video/',
        },
    }

    endpoint = local_endpoints.get(task_type, {})

    # 1. Try local GPU — health-cached
    if endpoint:
        try:
            from integrations.agent_engine.model_bus_service import get_model_bus
            bus = get_model_bus()
            local_url = endpoint['url']
            backend_name = f'video_{task_type}'

            if bus._is_backend_alive(backend_name, local_url):
                from core.http_pool import pooled_post
                resp = pooled_post(
                    f"{local_url}{endpoint['path']}",
                    json=task_params,
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    bus._mark_backend_alive(backend_name)
                    return SubtaskResult(
                        success=True, data=resp.json(), peer='local_gpu')
                bus._mark_backend_dead(backend_name)
        except Exception as e:
            logger.debug("Local GPU %s unavailable: %s", task_type, e)

    # 2. Hive mesh peer
    try:
        from integrations.agent_engine.compute_config import get_compute_policy
        policy = get_compute_policy()
        if policy.get('compute_policy') != 'local_only':
            from integrations.agent_engine.compute_mesh_service import get_compute_mesh
            mesh = get_compute_mesh()
            result = mesh.offload_to_best_peer(
                model_type=task_type,
                prompt=json.dumps(task_params),
                options={'timeout': timeout, 'user_id': user_id},
            )
            if result and 'error' not in result:
                return SubtaskResult(
                    success=True,
                    data=result,
                    peer=result.get('offloaded_to', 'hive_peer'),
                )
            logger.info("Hive offload %s: %s", task_type,
                        result.get('error', 'no result'))
    except Exception as e:
        logger.debug("Hive mesh %s unavailable: %s", task_type, e)

    return SubtaskResult(success=False, error=f'No GPU backend for {task_type}')


def _dispatch_parallel(tasks: List[Tuple[str, dict, str, int]]) -> List[SubtaskResult]:
    """Dispatch multiple GPU subtasks in parallel.

    Args:
        tasks: List of (task_type, params, user_id, timeout) tuples

    Returns:
        List of SubtaskResult in same order as input
    """
    results = [None] * len(tasks)

    def _run(idx, task_type, params, user_id, timeout):
        results[idx] = _dispatch_gpu_subtask(task_type, params, user_id, timeout)

    threads = []
    for i, (ttype, params, uid, tout) in enumerate(tasks):
        t = threading.Thread(target=_run, args=(i, ttype, params, uid, tout),
                             daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=600)  # Hard cap: 10 minutes

    # Replace None with error for timed-out tasks
    return [r or SubtaskResult(success=False, error='Task timed out')
            for r in results]


# ─── Crossbar Publishing ─────────────────────────────────

def _publish_chunk_result(publish_id: str, chunk_data: dict):
    """Publish a completed chunk to pupit.{publish_id} for streaming playback.

    The client (WebWorker/React Native) picks up each chunk and plays
    the video segment immediately — streaming, not waiting for full video.
    """
    if not publish_id:
        return
    try:
        from hart_intelligence import publish_async
        topic = f'com.hertzai.pupit.{publish_id}'
        publish_async(topic, json.dumps(chunk_data))
    except Exception as e:
        logger.debug("Chunk publish failed: %s", e)


def _publish_status(user_id: str, message: str, request_id: str = ''):
    """Publish routing status to user's chat (thinking bubble)."""
    try:
        from integrations.agent_engine.model_bus_service import _publish_routing_status
        _publish_routing_status(user_id, message, request_id)
    except Exception:
        pass


# ─── Main Orchestrator ───────────────────────────────────

class VideoOrchestrator:
    """Orchestrates video generation pipeline.

    Replaces MakeItTalk's views_c.py orchestration with HARTOS-native
    dispatch to local GPU or hive mesh peers.

    Usage:
        orch = get_video_orchestrator()
        result = orch.generate(request_data)
    """

    def __init__(self):
        self._active_jobs: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._job_counter = 0

    @property
    def queue_depth(self) -> int:
        """Current number of active jobs."""
        with self._lock:
            return len(self._active_jobs)

    def generate(self, data: dict) -> dict:
        """Main entry point — orchestrate a video generation request.

        Args:
            data: Raw request dict from API endpoint

        Returns:
            On success (HTTP 202 pattern):
                {
                    'status': 'accepted',
                    'uid': '<request-id>',
                    'total_jobs_in_queue': int,
                    'position': int,
                    'estimated_seconds': int,
                }
            On error:
                {'error': '<message>'}
        """
        req = VideoGenRequest(data)

        # Validate
        err = req.validate()
        if err:
            return {'error': err}

        # Estimate duration + queue position
        audio_duration = estimate_audio_duration(req.text)
        eta = calculate_queue_eta(self.queue_depth, audio_duration)

        # Enforce Hallo duration constraint
        if req.flag_hallo and audio_duration > HALLO_MAX_DURATION:
            req.flag_hallo = False
            logger.info("Hallo disabled — estimated %ds > %ds max",
                        audio_duration, HALLO_MAX_DURATION)

        # Register job
        job_id = req.uid
        with self._lock:
            self._active_jobs[job_id] = {
                'uid': job_id,
                'user_id': req.user_id,
                'started': time.time(),
                'status': 'queued',
            }

        # Dispatch asynchronously — don't block the API response
        thread = threading.Thread(
            target=self._execute_pipeline,
            args=(req, eta),
            daemon=True,
            name=f'video-gen-{job_id}',
        )
        thread.start()

        return {
            'status': 'accepted',
            'uid': job_id,
            'total_jobs_in_queue': eta['total_jobs_in_queue'],
            'position': eta['position'],
            'estimated_seconds': eta['estimated_seconds'],
        }

    def _execute_pipeline(self, req: VideoGenRequest, eta: dict):
        """Execute the full video generation pipeline in background.

        Pipeline stages:
          1. Download/cache assets (image, audio sample)
          2. Split text into chunks (if chunking enabled)
          3. For each chunk (or full text):
             a. Dispatch TTS audio generation (GPU)
             b. Dispatch face crop + bg removal (GPU, parallel with TTS)
             c. Dispatch lip-sync video generation (GPU, sequential after a+b)
             d. Optionally dispatch HD upscale (GPU)
             e. Publish chunk result to Crossbar pupit.{publish_id}
          4. Cleanup job tracking
        """
        job_id = req.uid
        try:
            self._update_job(job_id, 'processing')
            _publish_status(req.user_id,
                            'Starting video generation...', req.request_id)

            # 1. Download assets
            image_path = None
            audio_sample_path = None

            if req.image_url:
                _publish_status(req.user_id,
                                'Downloading image...', req.request_id)
                image_path = download_asset(req.image_url, 'images')
                if not image_path:
                    self._fail_job(job_id, req, 'Failed to download image')
                    return

            if req.audio_sample_url:
                audio_sample_path = download_asset(
                    req.audio_sample_url, 'audio')

            # 2. Chunk text for streaming
            if req.chunking and req.text:
                chunks = chunk_text(req.text)
            else:
                chunks = [req.text] if req.text else []

            if not chunks:
                self._fail_job(job_id, req, 'No text to process')
                return

            _publish_status(
                req.user_id,
                f'Processing {len(chunks)} segment{"s" if len(chunks) > 1 else ""}...',
                req.request_id,
            )

            # 3. Process each chunk
            for idx, chunk_text_str in enumerate(chunks, 1):
                chunk_uid = f"{job_id}_{idx}"

                success = self._process_chunk(
                    req=req,
                    chunk_uid=chunk_uid,
                    chunk_text=chunk_text_str,
                    chunk_idx=idx,
                    total_chunks=len(chunks),
                    image_path=image_path,
                    audio_sample_path=audio_sample_path,
                    eta=eta,
                )

                if not success:
                    # Continue with remaining chunks — partial delivery
                    # is better than no delivery
                    logger.warning("Chunk %d/%d failed for %s",
                                   idx, len(chunks), job_id)

            self._update_job(job_id, 'completed')
            _publish_status(req.user_id,
                            'Video generation complete.', req.request_id)

        except Exception as e:
            logger.error("Video pipeline failed for %s: %s", job_id, e,
                         exc_info=True)
            self._fail_job(job_id, req, str(e))
        finally:
            # Cleanup job tracking
            with self._lock:
                self._active_jobs.pop(job_id, None)

    def _process_chunk(
        self,
        req: VideoGenRequest,
        chunk_uid: str,
        chunk_text: str,
        chunk_idx: int,
        total_chunks: int,
        image_path: Optional[str],
        audio_sample_path: Optional[str],
        eta: dict,
    ) -> bool:
        """Process a single text chunk through the full GPU pipeline.

        Parallel dispatch: TTS + face crop run simultaneously.
        Sequential: lip-sync waits for both to complete.

        Returns True on success, False on failure.
        """
        _publish_status(
            req.user_id,
            f'Processing segment {chunk_idx}/{total_chunks}...',
            req.request_id,
        )

        # ── Stage 1: Parallel — TTS + face crop ──

        tts_params = {
            'uid': chunk_uid,
            'text': chunk_text,
            'gender': req.gender,
            'voice_name': req.voice_name,
            'openvoice': req.openvoice,
            'chattts': req.chattts,
            'kokuro': req.kokuro,
        }
        if audio_sample_path:
            tts_params['audio_sample_path'] = audio_sample_path

        crop_params = {
            'uid': chunk_uid,
            'image_path': image_path,
            'crop': req.crop,
            'remove_bg': req.remove_bg,
            'vtoonify': req.vtoonify,
            'flag_hallo': req.flag_hallo,
            'premium': req.is_premium,
            'inpainting': req.inpainting,
            'prompt': req.inpainting_prompt,
            'gradient': req.gradient,
            'solid_color': req.solid_color,
            'background_path': req.background_path,
        }

        parallel_tasks = [
            ('audio_generation', tts_params, req.user_id,
             eta.get('soft_time_limit', 300)),
            ('crop_background', crop_params, req.user_id, 120),
        ]

        results = _dispatch_parallel(parallel_tasks)
        audio_result, crop_result = results[0], results[1]

        if not audio_result.success:
            # Try offline TTS fallback (CPU) via model_bus_service
            _publish_status(req.user_id,
                            'GPU TTS unavailable, trying offline voice...',
                            req.request_id)
            audio_result = self._tts_cpu_fallback(chunk_text, chunk_uid, req)

        if not audio_result.success:
            logger.error("Audio generation failed for chunk %s: %s",
                         chunk_uid, audio_result.error)
            _publish_chunk_result(req.publish_id, {
                'chunk_idx': chunk_idx,
                'total_chunks': total_chunks,
                'status': 'error',
                'error': f'Audio generation failed: {audio_result.error}',
            })
            return False

        # Image crop failure is non-fatal — use original image
        effective_image = (crop_result.data if crop_result.success
                          else {'image_path': image_path})

        # ── Stage 2: Sequential — lip-sync generation ──

        _publish_status(
            req.user_id,
            f'Generating lip-sync video ({chunk_idx}/{total_chunks})...',
            req.request_id,
        )

        lip_sync_params = {
            'uid': chunk_uid,
            'audio_result': audio_result.data,
            'image_result': effective_image,
            'text': chunk_text,
            'flag_hallo': req.flag_hallo,
            'hd_video': req.hd_video,
        }

        lip_result = _dispatch_gpu_subtask(
            'lip_sync', lip_sync_params, req.user_id,
            eta.get('soft_time_limit', 300),
        )

        if not lip_result.success:
            logger.error("Lip sync failed for chunk %s: %s",
                         chunk_uid, lip_result.error)
            _publish_chunk_result(req.publish_id, {
                'chunk_idx': chunk_idx,
                'total_chunks': total_chunks,
                'status': 'error',
                'error': f'Video generation failed: {lip_result.error}',
            })
            return False

        # ── Stage 3: Optional HD upscale ──

        video_data = lip_result.data
        if req.hd_video:
            _publish_status(req.user_id,
                            'Upscaling to HD...', req.request_id)
            hd_result = _dispatch_gpu_subtask(
                'hd_upscale',
                {'uid': chunk_uid, 'video_result': video_data},
                req.user_id, 600,  # HD can take a while
            )
            if hd_result.success:
                video_data = hd_result.data
            else:
                logger.info("HD upscale skipped for %s: %s",
                            chunk_uid, hd_result.error)
                # Non-fatal — deliver SD video

        # ── Stage 4: Publish chunk to client ──

        chunk_result = {
            'chunk_idx': chunk_idx,
            'total_chunks': total_chunks,
            'status': 'completed',
            'uid': chunk_uid,
            'video_url': video_data.get('video_url', ''),
            'audio_url': audio_result.data.get('audio_url',
                          audio_result.data.get('gen_audio_url', '')),
            'peer': lip_result.peer,
        }
        _publish_chunk_result(req.publish_id, chunk_result)

        return True

    def _tts_cpu_fallback(self, text: str, uid: str,
                          req: VideoGenRequest) -> SubtaskResult:
        """CPU TTS fallback when no GPU is available for audio generation.

        Uses HARTOS's offline TTS (Pocket TTS / LuxTTS) instead of GPU neural TTS.
        """
        try:
            from integrations.agent_engine.model_bus_service import get_model_bus
            bus = get_model_bus()
            result = bus.infer(
                prompt=text,
                model_type='tts',
                options={
                    'user_id': req.user_id,
                    'voice': req.voice_name or 'alba',
                    'request_id': req.request_id,
                },
            )
            if result and result.get('response'):
                audio_path = result['response']
                return SubtaskResult(
                    success=True,
                    data={
                        'audio_path': audio_path,
                        'gen_audio_url': audio_path,
                    },
                    peer='local_cpu',
                )
        except Exception as e:
            logger.debug("CPU TTS fallback failed: %s", e)

        return SubtaskResult(success=False, error='All TTS backends failed')

    def _update_job(self, job_id: str, status: str):
        """Update job tracking status."""
        with self._lock:
            if job_id in self._active_jobs:
                self._active_jobs[job_id]['status'] = status

    def _fail_job(self, job_id: str, req: VideoGenRequest, error: str):
        """Mark job as failed and notify user."""
        self._update_job(job_id, 'failed')
        _publish_status(
            req.user_id,
            f"Video generation failed: {error}. "
            "No GPU is available locally or on the hive network. "
            "You can try again shortly, or connect a GPU device with: "
            "hart compute pair <device-address>",
            req.request_id,
        )
        _publish_chunk_result(req.publish_id, {
            'status': 'error',
            'error': error,
            'uid': job_id,
        })

    def get_job_status(self, job_id: str) -> Optional[dict]:
        """Get status of an active job."""
        with self._lock:
            return self._active_jobs.get(job_id)

    def get_stats(self) -> dict:
        """Get orchestrator statistics."""
        with self._lock:
            return {
                'active_jobs': len(self._active_jobs),
                'jobs': {k: v.get('status', 'unknown')
                         for k, v in self._active_jobs.items()},
            }


# ─── Singleton ────────────────────────────────────────────

_orchestrator: Optional[VideoOrchestrator] = None
_orch_lock = threading.Lock()


def get_video_orchestrator() -> VideoOrchestrator:
    """Get or create the singleton VideoOrchestrator."""
    global _orchestrator
    if _orchestrator is None:
        with _orch_lock:
            if _orchestrator is None:
                _orchestrator = VideoOrchestrator()
    return _orchestrator


def reset_video_orchestrator():
    """Reset singleton (testing only)."""
    global _orchestrator
    _orchestrator = None
