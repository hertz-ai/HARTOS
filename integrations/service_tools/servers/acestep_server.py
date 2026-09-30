"""ACE-Step 1.5 Music Generation Sidecar — dynamic port, upstream app.

Launched as a subprocess by RuntimeToolManager.  On startup:
1. Binds an OS-assigned free port on 127.0.0.1
2. Prints ``PORT=NNNNN`` to stdout (the parent reads this)
3. Imports ACE-Step's own FastAPI app from the cloned repo
4. Serves it on the already-bound socket

This does NOT reimplement music generation.  ACE-Step ships a FastAPI
server exposing exactly the three routes ``acestep_tool.py`` already
declares — ``POST /release_task``, ``POST /query_result`` and
``GET /health`` — so the sidecar's whole job is to bind a dynamic port
and hand it to that app.  Writing a second inference path here would
duplicate the upstream pipeline and drift from it.

Why this file had to exist: ``TOOL_CONFIGS['acestep']`` carried
``run_command = ['uv', 'run', 'acestep-api', '--port', '8001']``, but
``_start_sidecar`` only ever reads ``server_script`` — nothing in the
tree read ``run_command`` (grep: one hit, its own definition).  So
``setup_tool('acestep')`` returned ``{'error': 'Server script not
found: None'}`` and the tool could never start.  That command also
pinned port 8001, contradicting the module's own "all sidecar servers
use dynamic port allocation" contract.

Environment set by RuntimeToolManager._start_sidecar:
    ACESTEP_MODEL_DIR  — the cloned repo (storage.get_tool_dir)
    ACESTEP_OFFLOAD    — 'gpu' | 'cpu_offload' | 'cpu_only'

Usage (standalone test):
    python integrations/service_tools/servers/acestep_server.py
"""

import os
import socket
import sys
from pathlib import Path


def _bind_dynamic_port(host: str = '127.0.0.1'):
    """Bind an OS-assigned port and return (socket, port).

    The socket is handed to uvicorn rather than closed and re-bound.
    wan2gp_server/tts_audio_suite_server find a port, close it, then
    re-bind later — a TOCTOU window another process can win.  ACE-Step
    widens that window to tens of seconds because torch/diffusers import
    between the two, so the listener is kept open the whole time.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    return sock, sock.getsockname()[1]


def _model_dir() -> Path:
    return Path(os.environ.get(
        'ACESTEP_MODEL_DIR',
        str(Path.home() / '.hevolve' / 'models' / 'acestep'),
    ))


def _apply_offload_env(model_dir: Path) -> None:
    """Translate HART's offload verdict into ACE-Step's own env vars.

    ACE-Step self-tunes from FREE VRAM in acestep/gpu_config.py (tier1
    <=4GB enables offload_to_cpu + offload_dit_to_cpu + INT8
    quantization), so 'cpu_offload' deliberately leaves that autodetect
    alone and only forces the LM off the GPU.  'cpu_only' is the one
    mode that has to override the tier logic outright.
    """
    mode = os.environ.get('ACESTEP_OFFLOAD', 'gpu')

    # Weights live beside the repo unless the operator says otherwise, so
    # they follow HEVOLVE_MODEL_DIR instead of landing on the home drive.
    os.environ.setdefault('ACESTEP_CHECKPOINTS_DIR',
                          str(model_dir / 'checkpoints'))
    # Load on first request, not at import: the parent gives the sidecar
    # 30s to report its port, and a cold checkpoint load far exceeds that.
    os.environ.setdefault('ACESTEP_NO_INIT', 'true')

    if mode == 'cpu_only':
        os.environ['ACESTEP_DEVICE'] = 'cpu'
        os.environ['ACESTEP_LM_DEVICE'] = 'cpu'
        os.environ['ACESTEP_LM_OFFLOAD_TO_CPU'] = 'true'
    elif mode == 'cpu_offload':
        os.environ['ACESTEP_LM_OFFLOAD_TO_CPU'] = 'true'


def main() -> int:
    model_dir = _model_dir()
    if not model_dir.is_dir():
        print(f"ACESTEP_MODEL_DIR does not exist: {model_dir}",
              file=sys.stderr, flush=True)
        return 2

    _apply_offload_env(model_dir)

    # Bind and announce BEFORE the heavy import — torch + diffusers take
    # far longer than the parent's 30s port timeout.
    sock, port = _bind_dynamic_port()
    print(f"PORT={port}", flush=True)

    # The repo is a clone, not an installed distribution (wan2gp_server
    # uses the same sys.path insertion), so `import acestep` needs it.
    if str(model_dir) not in sys.path:
        sys.path.insert(0, str(model_dir))

    import uvicorn
    from acestep.api_server import app

    config = uvicorn.Config(app, log_level='info')
    uvicorn.Server(config).run(sockets=[sock])
    return 0


if __name__ == '__main__':
    sys.exit(main())
