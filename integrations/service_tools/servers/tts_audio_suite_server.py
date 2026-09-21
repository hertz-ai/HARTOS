"""
TTS-Audio-Suite Sidecar Server — Flask API on a dynamic port.

Launched as a subprocess by RuntimeToolManager. On startup:
1. Finds a free port (OS-assigned)
2. Prints PORT=NNNNN to stdout (parent reads this)
3. Lazy-loads TTS models
4. Serves TTS requests

Usage (standalone test):
    python -m integrations.service_tools.servers.tts_audio_suite_server

Pattern from: ltx2_server.py
"""

import json
import logging
import os
import socket
import sys
import uuid
from pathlib import Path
from threading import Lock

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
logger = logging.getLogger('tts_audio_suite_server')

app = Flask(__name__)

# Global state
_model = None
_model_lock = Lock()
_model_dir = None
OUTPUT_DIR = os.path.join(Path.home(), '.hevolve', 'outputs', 'tts_audio_suite')
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _get_model_dir():
    global _model_dir
    if _model_dir:
        return _model_dir
    _model_dir = os.environ.get(
        'TTS_AUDIO_SUITE_MODEL_DIR',
        str(Path.home() / '.hevolve' / 'models' / 'tts-audio-suite')
    )
    return _model_dir


def _load_model():
    """Lazy-load TTS-Audio-Suite model."""
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        model_dir = _get_model_dir()
        logger.info(f"Loading TTS-Audio-Suite from {model_dir}...")

        if model_dir not in sys.path:
            sys.path.insert(0, model_dir)

        try:
            offload_mode = os.environ.get('TTS_OFFLOAD', 'gpu')

            # TTS-Audio-Suite supports multiple TTS backends
            # Actual loading depends on repo structure
            _model = {
                'loaded': True,
                'model_dir': model_dir,
                'offload_mode': offload_mode,
            }
            logger.info(f"TTS-Audio-Suite loaded (mode: {offload_mode})")
            return _model
        except Exception as e:
            logger.error(f"Failed to load TTS-Audio-Suite: {e}")
            _model = {'loaded': False, 'error': str(e)}
            return _model


@app.route('/health', methods=['GET'])
def health():
    """Health check with VRAM stats."""
    status = {'status': 'ok', 'service': 'tts_audio_suite'}
    try:
        import torch
        if torch.cuda.is_available():
            status['gpu'] = torch.cuda.get_device_name(0)
            status['vram_total_gb'] = round(torch.cuda.get_device_properties(0).total_memory / 1e9, 2)
            status['vram_used_gb'] = round(torch.cuda.memory_allocated(0) / 1e9, 2)
    except ImportError:
        logger.debug("health: swallowed ImportError")
    return jsonify(status)


@app.route('/synthesize', methods=['POST'])
def synthesize():
    """Synthesize speech from text."""
    data = request.get_json() or {}
    text = data.get('text', '')
    model_name = data.get('model', 'default')
    language = data.get('language', 'en')

    if not text:
        return jsonify({'error': 'text is required'}), 400

    # This sidecar CANNOT synthesize, and must not pretend otherwise.
    #
    # Measured 2026-09-21 against the live sidecar: the previous body
    # returned 200 {"success": true, "audio_url": "/audio/<id>"} while
    # writing nothing -- GET /audio/<id> answered 404 and OUTPUT_DIR held
    # zero files.  media_agent._generate_audio_speech turns a 200 into
    # {'status': 'completed', ...}, so an agent asking for speech was told
    # it had speech.  A success flag for work that did not happen is worse
    # than an error.
    #
    # It cannot be implemented here either: diodiogod/TTS-Audio-Suite is a
    # ComfyUI custom-node pack (pyproject [tool.comfy]; 60
    # NODE_CLASS_MAPPINGS in nodes.py; requirements.txt: "This custom node
    # uses install.py"), so the clone exposes no synthesis HTTP API to
    # proxy.  Its engines -- chatterbox, chatterbox_official_23lang,
    # cosyvoice, f5_tts, omnivoice -- are ALREADY first-class entries in
    # integrations/channels/media/tts_router.py::ENGINE_REGISTRY, each with
    # its own tool module.  Synthesizing here would be a second TTS stack
    # beside the canonical router, which this codebase treats as a defect.
    logger.warning(
        "/synthesize refused: this sidecar has no engine; the canonical "
        "path is tts_router.get_tts_router().synthesize()"
    )
    return jsonify({
        'error': 'not_implemented',
        'message': (
            'tts_audio_suite exposes no synthesis backend. Upstream is a '
            'ComfyUI node pack with no HTTP API, and its engines are '
            'already in the canonical TTS registry. Use '
            'integrations.channels.media.tts_router.get_tts_router()'
            '.synthesize() instead.'
        ),
        'canonical_path': 'integrations.channels.media.tts_router',
        'text': text,
        'model': model_name,
        'language': language,
    }), 501


@app.route('/models', methods=['GET'])
def list_models():
    """List available TTS models.

    Empty by construction: this sidecar has no engine (see /synthesize).
    It previously advertised a 'default' model that could not be invoked,
    which read to a tool-selecting agent as a usable capability.
    """
    return jsonify({
        'models': [],
        'canonical_path': 'integrations.channels.media.tts_router',
        'message': (
            'No models served here. TTS engines live in '
            'tts_router.ENGINE_REGISTRY.'
        ),
    })


@app.route('/audio/<filename>', methods=['GET'])
def serve_audio(filename):
    """Serve generated audio file."""
    path = os.path.join(OUTPUT_DIR, f"tts_{filename}.wav")
    if os.path.exists(path):
        return send_file(path, mimetype='audio/wav')
    return jsonify({'error': 'not found'}), 404


@app.route('/unload', methods=['POST'])
def unload():
    """Unload model to free memory."""
    global _model
    _model = None
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
