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
    AVATARFORCING_OFFLOAD    -- 'gpu' only (the engine is CUDA-only, ~1 GB)
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


def main() -> int:
    model_dir = _model_dir()
    if not (model_dir / 'configs' / 'inference.yaml').is_file():
        print(f"No AvatarForcing checkout in {model_dir}", file=sys.stderr, flush=True)
        return 2
    if os.environ.get('AVATARFORCING_OFFLOAD', 'gpu') not in ('gpu', ''):
        print("AvatarForcing runs on the GPU only; refusing a CPU mode start",
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
                        '--avatar_audio', 'data/avatar.wav'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
