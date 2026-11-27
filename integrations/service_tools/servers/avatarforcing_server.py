"""AvatarForcing talking-avatar sidecar -- dynamic port, the engine shipped beside this file.

Launched as a subprocess by RuntimeToolManager under the model's own venv
(<model dir>/.venv, made by servers/avatarforcing/setup_model.py, the row's
git_prepare; _resolve_python_for picks it).  On startup the engine's stream_server:
1. binds an OS-assigned free port on 127.0.0.1 and prints ``PORT=NNNNN``
   (the parent waits for that line), then answers HTTP at once;
2. loads the model in the background (/health reports ``ready``);
3. serves ``POST /session`` (a reply's speech audio + the avatar's portrait,
   as file: URLs from HARTOS on this machine) and ``/live.mp4?sid=...``, that
   reply's stream: one fragmented MP4, H.264 + AAC, generated while it plays.

Two folders: the ENGINE (servers/avatarforcing/: lowvram/, stream_server.py,
realtime_lowvram.py) ships with HARTOS; the MODEL folder is the upstream
AvatarForcing checkout (models/, configs/, data/, pretrained_dir/).  The engine
is first on the path; the server runs in the model folder.  It cannot import
HARTOS: the tool venv runs with PYTHONPATH/PYTHONHOME stripped.

Environment set by RuntimeToolManager._start_sidecar:
    AVATARFORCING_MODEL_DIR  -- the model folder (storage.get_tool_dir)
    AVATARFORCING_OFFLOAD    -- the mode this start runs in (OFFLOAD_FLAGS): 'gpu', or an
                                offload mode that moves the audio encoder / the flow transformer
                                to the CPU.  Every mode needs CUDA (the face encoder/decoder stay on
                                the GPU), so 'cpu_only' is refused.
"""

import os
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent / 'avatarforcing'


def _model_dir() -> Path:
    return Path(os.environ.get(
        'AVATARFORCING_MODEL_DIR',
        str(Path.home() / '.hevolve' / 'models' / 'avatarforcing'),
    ))


#: AVATARFORCING_OFFLOAD -> the engine's flags (servers/avatarforcing/stream_server.py,
#: lowvram/engine.py).  Measured on an RTX 3070 Laptop, 2026-11-26 (AvatarForcing results,
#: cpu_offload_bench_summary.md): gpu 1.46x real time at a 535 MB CUDA peak; cpu_audio 0.40x at
#: 450 MB; cpu_audio_ft_int8 0.30x at 272 MB (parity cos 0.989 / PSNR 28.3 dB).  Which mode a start
#: gets is vram_manager.suggest_offload_mode's answer (resource-based, measured per machine); this
#: table only names what each mode runs where.  RTM's generic 'cpu_offload' is the lighter offload.
OFFLOAD_FLAGS = {
    'gpu': [],
    'cpu_audio': ['--audio_device', 'cpu'],
    'cpu_offload': ['--audio_device', 'cpu'],
    'cpu_audio_ft_int8': ['--audio_device', 'cpu', '--ft_device', 'cpu', '--cpu_int8'],
}


def main() -> int:
    model_dir = _model_dir()
    if not (model_dir / 'configs' / 'inference.yaml').is_file():
        print(f"No AvatarForcing checkout in {model_dir}", file=sys.stderr, flush=True)
        return 2
    mode = os.environ.get('AVATARFORCING_OFFLOAD', '') or 'gpu'
    if mode not in OFFLOAD_FLAGS:
        # 'cpu_only' (no CUDA): every mode keeps the face encoder/decoder on the GPU
        print(f"AvatarForcing has no {mode!r} mode (it needs CUDA); refusing the start",
              file=sys.stderr, flush=True)
        return 3
    os.environ['AVATARFORCING_MODEL_DIR'] = str(model_dir)  # stream_server runs there
    for p in (str(model_dir), str(ENGINE_DIR)):  # engine first
        if p not in sys.path:
            sys.path.insert(0, p)
    import stream_server  # the engine's server; prints PORT=N itself

    # No demo user video: sessions bring their own audio and portrait. The checkout's sample
    # face is the demo /view's only.
    stream_server.main(['--port', '0', '--user_video', '', '--ref', 'data/rumi.jpg',
                        '--avatar_audio', 'data/avatar.wav'] + OFFLOAD_FLAGS[mode])
    return 0


if __name__ == '__main__':
    sys.exit(main())
