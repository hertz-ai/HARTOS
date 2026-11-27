"""Make an AvatarForcing checkout ready for HARTOS's talking-avatar sidecar.

RuntimeToolManager.setup_tool runs this once the checkout is cloned (the row's
``git_prepare``), on its setup thread under the tool's setup lock, never on a reply:

    <python> setup_model.py <model folder>

0. The checkout is put at PINNED_COMMIT, the upstream commit the engine was measured on (the
   engine patches upstream internals; a later upstream commit could break a fresh install).
1. ``models/wav2vec2.py``: newer transformers refuse ``output_attentions`` under SDPA
   attention; the upstream line is guarded so it only applies to eager attention (the value
   is never read: the encoder is called with output_attentions=None).
2. ``<model folder>/.venv``: the model's own Python, with CUDA torch and the engine's packages
   (requirements.txt beside this file).  RuntimeToolManager._resolve_python_for launches the
   sidecar with it, so nothing here touches the app's own packages.  Needs MIN_FREE_BYTES
   free on the drive; pip keeps no cache copy.
3. ``<model folder>/pretrained_dir``: the AvatarForcing checkpoints (the authors' Google Drive
   files, as their download_weights.sh fetches them) and facebook/wav2vec2-base-960h from
   Hugging Face; and the face detector's weights (face_alignment's S3FD) in torch's hub cache,
   where the engine's FaceCropper looks for them.  A file is fetched to a .part name and kept
   only at its known size.

Every step is skipped when its result is already there.  The check that everything is there
(is_ready) reads files (and asks git for the checkout's commit), because every setup of the
model runs this script.  Exit code 0 = ready; otherwise the last line says what failed.
Standard library only: it runs on the interpreter that creates venvs, before the model's own
one exists.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = HERE / 'requirements.txt'

#: upstream TaekyungKi/AvatarForcing "minimal inference" (2026-06-04): the engine's base.
PINNED_COMMIT = '507e3046ad539c938e0412ac7a74cf6e3ba2a61c'
#: The venv with CUDA torch (~4.5 GB) + the checkpoints (~1.2 GB) + room to unpack, on the
#: model folder's drive; pip unpacks its wheels (torch ~2.5 GB) in the temp folder.
MIN_FREE_BYTES = 7 * 2**30
MIN_FREE_TEMP_BYTES = 3 * 2**30
PIP = ['-m', 'pip', 'install', '--no-warn-script-location', '--no-cache-dir']

#: (file in pretrained_dir, Google Drive id, size in bytes) -- download_weights.sh's two ids.
CHECKPOINTS = (
    ('motion_autoencoder.pth', '1a0ZqX9GMrkb3JFQgOBmUB0V-Bpwtv4Hp', 181_034_955),
    ('flow_transformer.pth', '1lchvHiDka0bBQ29bdY6lHxCyNGbGzvS_', 613_993_979),
)
#: face_alignment's S3FD face detector, fetched by its own loader into torch's hub cache.
S3FD_FILE, S3FD_SIZE = 's3fd-619a316812.pth', 89_843_225
WAV2VEC2_REPO = 'facebook/wav2vec2-base-960h'
WAV2VEC2_FILES = ('config.json', 'preprocessor_config.json', 'feature_extractor_config.json',
                  'special_tokens_map.json', 'tokenizer_config.json', 'vocab.json',
                  'model.safetensors')

#: The upstream line, alone on its line at method depth.  The guarded copy is deeper, so a
#: plain substring test would find the old line inside the new one.
W2V_OLD = re.compile(r'^ {8}self\.config\.output_attentions = True$', re.M)
W2V_GUARD = 'if getattr(self.config, "_attn_implementation", "eager") == "eager":'
W2V_NEW = ('        ' + W2V_GUARD + '  '
           '# newer transformers reject this under sdpa; the value is unused '
           '(encoder gets output_attentions=None)\n'
           '            self.config.output_attentions = True\n')

#: The venv check: the packages the engine cannot start without, and a CUDA device.
PROBE = ('import torch, transformers, av, cv2, omegaconf, face_alignment, timm, albumentations, '
         'librosa; assert torch.cuda.is_available(), "no CUDA device"')


def say(msg):
    print(f'[avatarforcing setup] {msg}', flush=True)


def venv_python(model_dir: Path) -> Path:
    v = model_dir / '.venv'
    return v / 'Scripts' / 'python.exe' if os.name == 'nt' else v / 'bin' / 'python'


def run(cmd, timeout):
    """Run a step, its output streamed to ours (the setup log)."""
    say('$ ' + ' '.join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], timeout=timeout, stdin=subprocess.DEVNULL).returncode


def venv_base_ok(model_dir: Path) -> bool:
    """The venv's python and the interpreter it was made from both exist: a venv whose base
    moved or was removed cannot start."""
    cfg = model_dir / '.venv' / 'pyvenv.cfg'
    if not venv_python(model_dir).is_file() or not cfg.is_file():
        return False
    for line in cfg.read_text(encoding='utf-8', errors='replace').splitlines():
        key, _, value = line.partition('=')
        if key.strip() == 'home':
            return Path(value.strip()).is_dir()
    return False


def venv_ready(model_dir: Path) -> bool:
    if not venv_base_ok(model_dir):
        return False
    return subprocess.run([str(venv_python(model_dir)), '-c', PROBE], capture_output=True,
                          timeout=300).returncode == 0


def torch_hub_dir() -> Path:
    """torch.hub.get_dir(), without importing torch: $TORCH_HOME/hub, else
    $XDG_CACHE_HOME/torch/hub, else ~/.cache/torch/hub."""
    if os.environ.get('TORCH_HOME'):
        return Path(os.environ['TORCH_HOME']) / 'hub'
    cache = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(cache) / 'torch' / 'hub'


def s3fd_ready() -> bool:
    f = torch_hub_dir() / 'checkpoints' / S3FD_FILE
    return f.is_file() and f.stat().st_size == S3FD_SIZE


def weights_ready(model_dir: Path) -> bool:
    pre = model_dir / 'pretrained_dir'
    if any(not (pre / n).is_file() or (pre / n).stat().st_size != size for n, _, size in CHECKPOINTS):
        return False
    return all((pre / 'wav2vec2-base-960h' / f).is_file() for f in WAV2VEC2_FILES) and s3fd_ready()


def w2v_patched(model_dir: Path) -> bool:
    f = model_dir / 'models' / 'wav2vec2.py'
    if not f.is_file():
        return False
    text = f.read_text(encoding='utf-8')
    return W2V_GUARD in text and not W2V_OLD.search(text)


def git_head(model_dir: Path):
    """The checkout's commit; None when it is not a git checkout (a copied folder) or git is
    not there to say."""
    if not (model_dir / '.git').exists():
        return None
    try:
        r = subprocess.run(['git', '-C', str(model_dir), 'rev-parse', 'HEAD'], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def is_ready(model_dir) -> bool:
    """Everything this script makes is there (files only: every start of the model asks)."""
    model_dir = Path(model_dir)
    return (w2v_patched(model_dir) and weights_ready(model_dir) and venv_base_ok(model_dir)
            and git_head(model_dir) in (None, PINNED_COMMIT))


def pin_checkout(model_dir: Path) -> None:
    head = git_head(model_dir)
    if head in (None, PINNED_COMMIT):
        return
    say(f'the checkout is at {head[:9]}; moving it to {PINNED_COMMIT[:9]}')
    if run(['git', '-C', model_dir, 'fetch', '--depth', '1', 'origin', PINNED_COMMIT], timeout=600) != 0 \
            or run(['git', '-C', model_dir, 'checkout', '--force', '--detach', PINNED_COMMIT], timeout=300) != 0:
        raise SystemExit(f'could not put the checkout at {PINNED_COMMIT[:9]}')


def check_disk(model_dir: Path) -> None:
    import tempfile
    for where, path, need in (("the model folder's drive", model_dir, MIN_FREE_BYTES),
                              ("the temp folder's drive", tempfile.gettempdir(), MIN_FREE_TEMP_BYTES)):
        free = shutil.disk_usage(path).free
        if free < need:
            raise SystemExit(f'needs {need / 2**30:.0f} GB free on {where}; {free / 2**30:.1f} GB is free')


def patch_wav2vec2(model_dir: Path) -> None:
    f = model_dir / 'models' / 'wav2vec2.py'
    if not f.is_file():
        raise SystemExit('models/wav2vec2.py not found: is this an AvatarForcing checkout?')
    text = f.read_text(encoding='utf-8')
    if W2V_OLD.search(text):
        f.write_text(W2V_OLD.sub(lambda _m: W2V_NEW.rstrip('\n'), text), encoding='utf-8')
        say('models/wav2vec2.py guarded for SDPA attention')
    if not w2v_patched(model_dir):
        raise SystemExit('models/wav2vec2.py is not the upstream file this setup knows')


def make_venv(model_dir: Path) -> None:
    if venv_ready(model_dir):
        say('venv ready')
        return
    py = venv_python(model_dir)
    if not venv_base_ok(model_dir):
        if run([sys.executable, '-m', 'venv', '--clear', model_dir / '.venv'], timeout=600) != 0:
            raise SystemExit('could not create the venv')
    run([py, *PIP, '--upgrade', 'pip'], timeout=600)
    if run([py, *PIP, '-r', REQUIREMENTS], timeout=3600) != 0:
        raise SystemExit('pip could not install the engine packages (see the lines above)')
    if not venv_ready(model_dir):
        raise SystemExit('the venv was built but cannot import the engine packages or see a CUDA GPU')
    say('venv ready')


def fetch_weights(model_dir: Path) -> None:
    pre = model_dir / 'pretrained_dir'
    pre.mkdir(parents=True, exist_ok=True)
    py = venv_python(model_dir)
    for name, drive_id, size in CHECKPOINTS:
        f = pre / name
        if f.is_file() and f.stat().st_size == size:
            continue
        part = pre / (name + '.part')
        if run([py, '-m', 'gdown', drive_id, '-O', part], timeout=3600) != 0 \
                or not part.is_file() or part.stat().st_size != size:
            got = part.stat().st_size if part.is_file() else 0
            part.unlink(missing_ok=True)
            raise SystemExit(f'{name}: download failed or incomplete ({got} of {size} bytes)')
        part.replace(f)
    w2v = pre / 'wav2vec2-base-960h'
    if not all((w2v / x).is_file() for x in WAV2VEC2_FILES):
        # python -c CODE a b c: sys.argv is ['-c', a, b, c]
        code = ('import sys\nfrom huggingface_hub import hf_hub_download\n'
                'folder, repo = sys.argv[1], sys.argv[2]\n'
                'for f in sys.argv[3:]:\n'
                '    hf_hub_download(repo, f, local_dir=folder)\n')
        if run([py, '-c', code, w2v, WAV2VEC2_REPO, *WAV2VEC2_FILES], timeout=1800) != 0:
            raise SystemExit(f'{WAV2VEC2_REPO}: download failed')
    if not s3fd_ready():
        # face_alignment's own loader fetches it into torch's hub cache (the engine's FaceCropper
        # builds the same detector), so the sidecar never downloads anything when it starts.
        code = 'from face_alignment.detection.sfd.sfd_detector import SFDDetector; SFDDetector(device="cpu")'
        if run([py, '-c', code], timeout=1800) != 0 or not s3fd_ready():
            raise SystemExit(f'the face detector ({S3FD_FILE}) did not download')
    if not weights_ready(model_dir):
        raise SystemExit('weights incomplete after download')
    say('weights ready')


def main(argv) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    model_dir = Path(argv[0]).resolve()
    if not (model_dir / 'configs' / 'inference.yaml').is_file():
        say('the model folder is not an AvatarForcing checkout (no configs/inference.yaml)')
        return 2
    if is_ready(model_dir):
        say('ready')
        return 0
    try:
        pin_checkout(model_dir)
        patch_wav2vec2(model_dir)
        if not (venv_base_ok(model_dir) and weights_ready(model_dir)):
            check_disk(model_dir)
        make_venv(model_dir)
        fetch_weights(model_dir)
    except SystemExit as e:
        say(f'FAILED: {e}')
        return 1
    except subprocess.TimeoutExpired as e:
        say(f'FAILED: {Path(str(e.cmd[0])).name} ran past {e.timeout:.0f} s')
        return 1
    except Exception as e:  # the last line is the reason the caller is given
        say(f'FAILED: {type(e).__name__}: {e}')
        return 1
    say('ready')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
