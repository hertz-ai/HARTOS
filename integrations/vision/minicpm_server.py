"""
MiniCPM Vision Server — stripped standalone Flask server for the sidecar.

Derived from the root minicpm.py but with configurable model directory,
no external config.json dependency, and proper CLI args.

Usage:
    python -m integrations.vision.minicpm_server --model_dir ~/.hevolve/models/minicpm --port 9891
"""
import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from threading import Lock

from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

logger = logging.getLogger('minicpm_server')

app = Flask(__name__)
_model = None
_tokenizer = None
_device = None
_executor = ThreadPoolExecutor(max_workers=2)
_last_processing_time = {}
_last_processing_lock = Lock()

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '_uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}


def _init_model(model_dir: str, device: str = 'cuda:0'):
    """Load MiniCPM-V-2 onto the specified device."""
    global _model, _tokenizer, _device
    import torch
    from transformers import AutoModel, AutoTokenizer

    logger.info(f"Loading MiniCPM from {model_dir} on {device}")
    _device = device
    _model = AutoModel.from_pretrained(
        model_dir,
        trust_remote_code=True,
        torch_dtype=torch.float16,
    ).to(device=device, dtype=torch.float16)
    _model.eval()
    _tokenizer = AutoTokenizer.from_pretrained(
        model_dir,
        trust_remote_code=True,
    )
    logger.info("MiniCPM model loaded")


def _process_image_sync(image, prompt: str) -> str:
    """Run MiniCPM inference synchronously. Returns description string."""
    import torch
    msgs = [{'role': 'user', 'content': prompt}]
    with torch.inference_mode():
        res, _, _ = _model.chat(
            image=image,
            msgs=msgs,
            context=None,
            tokenizer=_tokenizer,
            sampling=True,
            temperature=0.7,
        )
    return res


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    return jsonify({'status': 'working', 'model': 'MiniCPM-V-2'})


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        'status': 'running',
        'model_loaded': _model is not None,
        'device': str(_device),
    })


@app.route('/upload', methods=['POST'])
def upload():
    """Process an image with an optional prompt. Returns {"result": "description"}."""
    from PIL import Image

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if not _allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400

    prompt = request.form.get(
        'prompt',
        'you are looking at user\'s camera feed, describe this image in 20 words',
    )
    user_id = request.form.get('user_id', '0')

    # Rate limit: 4 second throttle per user
    with _last_processing_lock:
        last_time = _last_processing_time.get(user_id, 0)
        if time.time() - last_time < 4 and last_time > 0:
            return jsonify({'result': '', 'throttled': True}), 429
        _last_processing_time[user_id] = time.time()

    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        image = Image.open(filepath).convert('RGB').resize((255, 255))
        result = _process_image_sync(image, prompt)

        # Clean up saved file
        try:
            os.remove(filepath)
        except OSError:
            pass

        return jsonify({'result': result})
    except Exception as e:
        logger.error(f"Error processing image: {e}")
        return jsonify({'error': 'Processing failed'}), 500


@app.route('/describe', methods=['POST'])
def describe_raw():
    """Accept raw image bytes (no multipart) + query param prompt."""
    from PIL import Image
    import io

    prompt = request.args.get(
        'prompt',
        'you are looking at user\'s camera feed, describe this image in 20 words',
    )

    try:
        image = Image.open(io.BytesIO(request.data)).convert('RGB').resize((255, 255))
        result = _process_image_sync(image, prompt)
        return jsonify({'result': result})
    except Exception as e:
        logger.error(f"Error in describe_raw: {e}")
        return jsonify({'error': 'Processing failed'}), 500


def main():
    parser = argparse.ArgumentParser(description='MiniCPM Vision Sidecar')
    parser.add_argument('--model_dir', default=os.path.join(
        os.path.expanduser('~'), '.hevolve', 'models', 'minicpm',
    ))
    parser.add_argument('--port', type=int, default=9891)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--device', default='cuda:0')
    parser.add_argument('--log_file', default='minicpm_sidecar.log')
    args = parser.parse_args()

    # Logging setup
    handler = RotatingFileHandler(args.log_file, maxBytes=500_000, backupCount=2)
    handler.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)

    # Load model
    _init_model(args.model_dir, args.device)

    # Serve
    from waitress import serve
    logger.info(f"MiniCPM sidecar starting on {args.host}:{args.port}")
    serve(app, host=args.host, port=args.port, threads=4)


if __name__ == '__main__':
    main()
