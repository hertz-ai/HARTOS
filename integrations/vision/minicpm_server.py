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
from typing import Optional

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


def _resolve_dtype(device: str):
    """Pick the load dtype for `device`.

    CPU used to get float32 unconditionally.  MiniCPM-V-2's own
    config.json declares `"torch_dtype": "bfloat16"` and ships 6.5 GB of
    bf16 safetensors, so float32 does not add accuracy — it just doubles
    the checkpoint to ~13 GB of RAM, which is more than the machines that
    have NO GPU (the ones that end up on the CPU path, and the ones RTM
    sends here when VRAMManager.suggest_offload_mode says cpu_offload /
    cpu_only) tend to have free.  Use the checkpoint's own dtype when
    torch can, and keep float32 as the fallback.

    MINICPM_DTYPE (float32 | bfloat16 | float16) overrides.
    """
    import torch
    override = os.environ.get('MINICPM_DTYPE', '').strip().lower()
    if override:
        resolved = getattr(torch, override, None)
        if isinstance(resolved, torch.dtype):
            return resolved
        logger.warning(f"Ignoring unknown MINICPM_DTYPE={override!r}")
    if device != 'cpu':
        return torch.float16
    try:
        # Not every torch build / CPU can actually run bf16 kernels.
        torch.zeros(2, dtype=torch.bfloat16) @ torch.zeros(2, 2, dtype=torch.bfloat16)
        return torch.bfloat16
    except Exception as e:
        logger.info(f"bfloat16 unusable on this CPU ({e}) — loading float32")
        return torch.float32


def _patch_transformers_cache_compat() -> None:
    """Restore Cache.get_max_length() for MiniCPM-V-2's vendored modelling code.

    The weights ship their own modeling_minicpm.py (trust_remote_code), written
    against transformers 4.36, and its prepare_inputs_for_generation calls
    `past_key_values.get_max_length()`.  transformers deprecated that accessor
    in 4.47 and removed it in favour of `get_max_cache_shape()`, so on a modern
    transformers every caption died INSIDE generate():

        MEASURED 2026-09-21, transformers 4.50.3, MiniCPM-V-2 on CPU:
        minicpm_server - ERROR - Error in describe_raw:
        'DynamicCache' object has no attribute 'get_max_length'

    — after a clean 562 s model load, i.e. the whole RTM path worked and only
    this one accessor was missing.  Both methods answer the same question (the
    cache's maximum length, None for a DynamicCache), so aliasing is faithful.

    NECESSARY BUT NOT SUFFICIENT on transformers >= 4.47.  With this in place
    the same request gets one step further into generate() and then dies in
    the model's own prepare_inputs_for_generation with "index is out of bounds
    for dimension with size 0" (MEASURED 2026-09-21, transformers 4.50.3):
    4.36-era code slices input_ids against cache-length semantics that have
    since changed.  Fixing that would mean rewriting the vendor's modelling
    file, which is not ours and is overwritten by the next download.  The
    supported answer is a PINNED INTERPRETER: RuntimeToolManager
    ._resolve_python_for already prefers ``<model_dir>/.venv``, so a
    ~/.hevolve/models/minicpm/.venv holding transformers <4.41 makes this tool
    work with no further code change — the same isolation ACE-Step uses.
    This shim stays because it is the correct repair for the one accessor
    that was merely RENAMED, and it is what lets the failure surface at the
    real incompatibility instead of masking it behind an AttributeError.

    Scoped to this process: the sidecar is its own subprocess, so nothing else
    in HART OS sees the patched class.  Idempotent, and a no-op on a
    transformers old enough to still have the method.
    """
    try:
        from transformers.cache_utils import Cache
    except Exception as e:
        logger.debug(f"cache compat shim skipped (no cache_utils): {e}")
        return
    if hasattr(Cache, 'get_max_length'):
        return
    if not hasattr(Cache, 'get_max_cache_shape'):
        logger.warning("transformers Cache has neither get_max_length nor "
                       "get_max_cache_shape — MiniCPM generate() will fail")
        return

    def _get_max_length(self):
        # Call through the INSTANCE so the subclass override wins.
        # Binding Cache.get_max_cache_shape directly does not work: on the
        # base class it is the abstract stub that raises
        # NotImplementedError("Make sure to implement `get_max_cache_shape`
        # in a subclass."), and DynamicCache's own override (which returns
        # None) would never be reached.  MEASURED 2026-09-21 — the direct
        # alias turned the original AttributeError into exactly that
        # NotImplementedError, one step further into generate().
        return self.get_max_cache_shape()

    Cache.get_max_length = _get_max_length
    logger.info("Compat: Cache.get_max_length now delegates to "
                "get_max_cache_shape for MiniCPM-V-2's "
                "transformers-4.36-era remote code")


def _init_model(model_dir: str, device: str = 'cuda:0'):
    """Load MiniCPM-V-2 onto the specified device (CUDA, MPS, or CPU)."""
    global _model, _tokenizer, _device
    from transformers import AutoModel, AutoTokenizer

    _patch_transformers_cache_compat()
    logger.info(f"Loading MiniCPM from {model_dir} on {device}")
    _device = device
    dtype = _resolve_dtype(device)

    # low_cpu_mem_usage halves the PEAK.  Without it from_pretrained builds
    # the whole 6.5 GB model with randomly-initialised weights FIRST and then
    # loads the state dict on top, so the high-water mark is roughly the model
    # plus its largest shard (4.99 GB here) — about 11.5 GB for a 6.5 GB
    # checkpoint.  With it, the model starts on the meta device and each shard
    # is materialised in place.  MEASURED 2026-09-21 on this box: loads
    # succeeded with 13.3 GB and 7.1 GB of free RAM and died with 5.8 GB
    # ("OSError: The paging file is too small for this operation to complete.
    # (os error 1455)" inside safetensors' safe_open) and again with 6.5 GB.
    # It needs `accelerate`; where that is missing transformers raises, so
    # fall back to the old shape rather than refusing to start.
    try:
        _model = AutoModel.from_pretrained(
            model_dir,
            trust_remote_code=True,
            torch_dtype=dtype,
            low_cpu_mem_usage=True,
        )
    except (ImportError, ValueError, TypeError) as e:
        logger.warning(f"low_cpu_mem_usage unavailable ({e}) — loading the "
                       f"high-peak way; this needs ~2x the checkpoint in RAM")
        _model = AutoModel.from_pretrained(
            model_dir,
            trust_remote_code=True,
            torch_dtype=dtype,
        )
    _model = _model.to(device=device, dtype=dtype)
    _model.eval()
    _tokenizer = AutoTokenizer.from_pretrained(
        model_dir,
        trust_remote_code=True,
    )
    logger.info(f"MiniCPM model loaded on {device} ({dtype})")


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


def _device_for_offload_mode(mode: str) -> Optional[str]:
    """Map RuntimeToolManager's offload mode onto a torch device string.

    RTM sets ``<TOOL>_OFFLOAD`` in the child's environment to one of
    'gpu' | 'cpu_offload' | 'cpu_only' (VRAMManager.suggest_offload_mode).
    Returns None for 'gpu' (and for anything unrecognised) so the caller
    falls through to its own cuda/mps/cpu auto-detection.

    'cpu_offload' maps to plain CPU here, NOT to an accelerate
    device_map: this server loads with AutoModel.from_pretrained(...).to(device),
    which has no layer-offload plumbing.  Honouring the advisory by staying
    off the GPU is the truthful reading -- claiming a partial-GPU load we
    do not implement would be worse.
    """
    if mode in ('cpu_only', 'cpu_offload'):
        return 'cpu'
    return None


def main():
    parser = argparse.ArgumentParser(description='MiniCPM Vision Sidecar')
    # MINICPM_MODEL_DIR / MINICPM_OFFLOAD are the env keys
    # RuntimeToolManager._start_sidecar sets for this tool
    # (f"{tool_name.upper()}_MODEL_DIR" / f"{tool_name.upper()}_OFFLOAD").
    parser.add_argument('--model_dir', default=os.environ.get(
        'MINICPM_MODEL_DIR',
        os.path.join(os.path.expanduser('~'), '.hevolve', 'models', 'minicpm'),
    ))
    # No --port => RuntimeToolManager sidecar contract: bind an OS-assigned
    # port and announce it as `PORT=NNNNN` on stdout, which
    # RuntimeToolManager._read_port_from_stdout reads back within
    # PORT_ANNOUNCE_TIMEOUT_S.  The line is printed BEFORE the multi-GB
    # model load for that reason.  An explicit --port (hart-vision.nix,
    # manual runs) keeps the old fixed-port behaviour and prints nothing.
    #
    # KNOWN COST, not yet fixed: the announce still sits behind
    # `from core.port_registry import find_free_port`, and importing that
    # runs core/__init__.py — MEASURED 2026-09-21 on a loaded box, 130 s
    # (`python -X importtime`), which is the whole of the 129 s setup_tool()
    # took that run.  It fits inside the 180 s budget but it is 130 s of
    # dead time on every start.  The fix is for the PARENT to allocate the
    # port and pass it in (RTM already owns ports via _ports), which would
    # retire the PORT= handshake for every sidecar at once; doing it per
    # server here would just mean another private socket-bind copy, which
    # is what core.port_registry.find_free_port was added to stop.
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--host', default=None)
    parser.add_argument('--device', default=None)
    parser.add_argument('--log_file', default='minicpm_sidecar.log')
    args = parser.parse_args()

    from core.port_registry import find_free_port

    sidecar_mode = args.port is None
    port = args.port if args.port is not None else find_free_port()
    # Sidecar binds loopback only (the parent talks to 127.0.0.1); the
    # explicit-port deployment keeps its previous all-interfaces default.
    host = args.host or ('127.0.0.1' if sidecar_mode else '0.0.0.0')

    if sidecar_mode:
        print(f"PORT={port}", flush=True)

    # Logging setup
    handler = RotatingFileHandler(args.log_file, maxBytes=500_000, backupCount=2)
    handler.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    # stdout/stderr stay on the pipes in sidecar mode.  They used to be
    # redirected to a file here, because a parent that stops reading after
    # the PORT= line lets a chatty child fill the pipe buffer and block —
    # but RuntimeToolManager._drain_pipes now pumps both pipes into the
    # parent's log for the process's whole life, which is the canonical fix
    # for every sidecar.  Redirecting on top of it only moved this child's
    # tracebacks somewhere the parent could not see: MEASURED 2026-09-21,
    # a load that ran the box out of RAM showed up as a bare "SIDECAR DIED"
    # with the tqdm shard bar sitting in a side file.
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)

    # Device: explicit flag wins, then RTM's offload advisory, then auto-detect
    device = args.device or _device_for_offload_mode(
        os.environ.get('MINICPM_OFFLOAD', ''))
    if device is None:
        import torch
        if torch.cuda.is_available():
            device = 'cuda:0'
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = 'mps'
        else:
            device = 'cpu'
        logger.info(f"Auto-detected device: {device}")

    # Load model
    _init_model(args.model_dir, device)

    # Serve
    from waitress import serve
    logger.info(f"MiniCPM sidecar starting on {host}:{port}")
    serve(app, host=host, port=port, threads=4)


if __name__ == '__main__':
    main()
