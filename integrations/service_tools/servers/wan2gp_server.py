"""
Wan2GP Video Generation Sidecar Server — Flask API on a dynamic port.

Launched as a subprocess by RuntimeToolManager. On startup:
1. Finds a free port (OS-assigned)
2. Prints PORT=NNNNN to stdout (parent reads this)
3. Lazy-loads Wan2GP model based on VRAM availability
4. Serves video generation requests (async: submit → poll)

Usage (standalone test):
    python -m integrations.service_tools.servers.wan2gp_server

Pattern from: ltx2_server.py, acestep_tool.py (async task pattern)
"""

import json
import logging
import os
import socket
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from threading import Lock, Thread

from flask import Flask, request, jsonify, send_file

try:
    from integrations.service_tools.vram_manager import clear_cuda_cache
except ImportError:
    def clear_cuda_cache():
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            logger.exception("clear_cuda_cache: swallowed Exception")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('wan2gp_server')

app = Flask(__name__)

# Global state
_pipeline = None
_pipeline_lock = Lock()
_model_dir = None

# Async task queue (same pattern as ACE-Step: submit → poll)
_tasks = OrderedDict()  # task_id → {status, result, error}
_MAX_TASKS = 100

#: Why this sidecar cannot generate.  One string so the refusal, the
#: pipeline's error and the health probe cannot drift apart.
_UNIMPLEMENTED = (
    'Wan2GP video generation is not implemented on this node: no adapter '
    'to the upstream repo exists yet'
)

OUTPUT_DIR = os.path.join(Path.home(), '.hevolve', 'outputs', 'wan2gp')
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _get_model_dir():
    global _model_dir
    if _model_dir:
        return _model_dir
    _model_dir = os.environ.get(
        'WAN2GP_MODEL_DIR',
        str(Path.home() / '.hevolve' / 'models' / 'wan2gp')
    )
    return _model_dir


def _load_pipeline():
    """Lazy-load Wan2GP video generation pipeline."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        model_dir = _get_model_dir()
        logger.info(f"Loading Wan2GP pipeline from {model_dir}...")

        if model_dir not in sys.path:
            sys.path.insert(0, model_dir)

        try:
            import torch
            offload_mode = os.environ.get('WAN2GP_OFFLOAD', 'gpu')

            # NOT LOADED.  Upstream Wan2GP is a Gradio app (wgp.py) with no
            # importable pipeline and no HTTP generate API, so there is
            # nothing here to load yet -- an adapter has to be written
            # first.  This used to build the dict below with 'loaded': True
            # and log "Wan2GP pipeline loaded", which is how /generate came
            # to believe it could serve.  Reporting the truth is what lets
            # the caller fall through to an engine that works.
            del offload_mode
            _pipeline = {
                'loaded': False,
                'model_dir': model_dir,
                'error': _UNIMPLEMENTED,
            }
            logger.warning("Wan2GP pipeline not loaded: %s", _UNIMPLEMENTED)
            return _pipeline
        except Exception as e:
            logger.error(f"Failed to load Wan2GP: {e}")
            _pipeline = {'loaded': False, 'error': str(e)}
            return _pipeline


def _generate_video_worker(task_id: str, params: dict):
    """Kept as the single place a real Wan2GP call will land.

    It records the refusal rather than a result.  The previous body read
    prompt / num_frames / width / height / steps, discarded all five, wrote
    no file, and set status 'complete' with a video_url and an output_path
    that led nowhere -- a caller polling /check_result was told its video
    was ready.  /generate now refuses before any task is created, so this
    runs only if something calls it directly.
    """
    _tasks[task_id] = {'status': 'error', 'error': _UNIMPLEMENTED}


@app.route('/health', methods=['GET'])
def health():
    """Health check with VRAM stats."""
    # `status: ok` means the sidecar answers, NOT that it can generate.
    # Callers choosing an engine read generate_available; a probe that
    # said only 'ok' is how a dead generator kept looking healthy.
    status = {'status': 'ok', 'service': 'wan2gp',
              'generate_available': False,
              'generate_unavailable_reason': _UNIMPLEMENTED,
              'pending_tasks': sum(1 for t in _tasks.values() if t.get('status') == 'pending')}
    try:
        import torch
        if torch.cuda.is_available():
            status['gpu'] = torch.cuda.get_device_name(0)
            status['vram_total_gb'] = round(torch.cuda.get_device_properties(0).total_memory / 1e9, 2)
            status['vram_used_gb'] = round(torch.cuda.memory_allocated(0) / 1e9, 2)
    except ImportError:
        logger.debug("health: swallowed ImportError")
    return jsonify(status)


@app.route('/generate', methods=['POST'])
def generate():
    """Submit a video generation task (async)."""
    data = request.get_json() or {}
    prompt = data.get('prompt', '')

    if not prompt:
        return jsonify({'error': 'prompt is required'}), 400

    # Evict old tasks
    while len(_tasks) >= _MAX_TASKS:
        _tasks.popitem(last=False)

    # Refuse BEFORE a task exists.  Handing back a task_id for work that
    # cannot happen leaves the caller polling /check_result forever, and
    # the media pipeline treats a pending task as progress.  501 is the
    # same answer the TTS sidecar gives for an engine it does not have.
    logger.warning("wan2gp /generate refused: %s", _UNIMPLEMENTED)
    return jsonify({
        'error': _UNIMPLEMENTED,
        'detail': (
            'Upstream Wan2GP ships wgp.py, a Gradio app with no generate '
            'API, so this sidecar has no adapter to call. Route video '
            'generation to ltx2, or write the adapter here.'
        ),
    }), 501


@app.route('/check_result', methods=['POST'])
def check_result():
    """Check status of a video generation task."""
    data = request.get_json() or {}
    task_id = data.get('task_id', '')

    if not task_id or task_id not in _tasks:
        return jsonify({'error': 'invalid task_id'}), 404

    return jsonify(_tasks[task_id])


@app.route('/video/<task_id>', methods=['GET'])
def serve_video(task_id):
    """Serve generated video file."""
    path = os.path.join(OUTPUT_DIR, f"video_{task_id}.mp4")
    if os.path.exists(path):
        return send_file(path, mimetype='video/mp4')
    return jsonify({'error': 'not found'}), 404


@app.route('/unload', methods=['POST'])
def unload():
    """Unload pipeline to free memory."""
    global _pipeline
    _pipeline = None
    clear_cuda_cache()
    return jsonify({'status': 'unloaded'})


def _find_free_port() -> int:
    """Find a free port using OS assignment."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


if __name__ == '__main__':
    port = _find_free_port()
    print(f"PORT={port}", flush=True)
    app.run(host='127.0.0.1', port=port, threaded=True)
