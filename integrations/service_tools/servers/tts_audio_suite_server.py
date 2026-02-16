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
            status['vram_total_gb'] = round(torch.cuda.get_device_properties(0).total_mem / 1e9, 2)
            status['vram_used_gb'] = round(torch.cuda.memory_allocated(0) / 1e9, 2)
    except ImportError:
        pass
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

    model = _load_model()
    if not model.get('loaded'):
        return jsonify({'error': f"Model not loaded: {model.get('error', 'unknown')}"}), 503

    try:
        output_id = str(uuid.uuid4())[:8]
        output_path = os.path.join(OUTPUT_DIR, f"tts_{output_id}.wav")

        # TODO: Replace with actual TTS-Audio-Suite inference call once repo is cloned
        return jsonify({
            'success': True,
            'audio_url': f"/audio/{output_id}",
            'text': text,
            'model': model_name,
            'language': language,
            'message': 'TTS-Audio-Suite synthesis placeholder — model integration pending repo clone',
        })
    except Exception as e:
        logger.error(f"Synthesis failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/models', methods=['GET'])
def list_models():
    """List available TTS models."""
    return jsonify({
        'models': [
            {'name': 'default', 'description': 'Default TTS model', 'languages': ['en']},
        ],
        'message': 'Model list placeholder — populated after model download',
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
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
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
