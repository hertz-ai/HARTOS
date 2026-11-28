"""A worker reports torch's memory only when its own model loaded torch.

MEASURED 2026-11-28 in the installed interpreter: run_worker ran
``import torch`` after every model load to self-report VRAM.  The figure is
torch's own allocator, so a worker whose model never touched torch
(CTranslate2 speech-in, ONNX voices) could only ever report 0.0, and paid
about 1.1 GB of committed memory and several seconds before READY to do it.

Each case runs the real run_worker loop in a fresh interpreter, with a small
package named ``torch`` first on the path, so "was torch imported" is a fact
about the process rather than about a mock.

    python -m pytest tests/unit/test_a_worker_without_torch_does_not_load_it.py -q --noconftest
"""
import json
import os
import subprocess
import sys
import textwrap

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_WORKER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {repo!r})
    sys.path.insert(0, {fake!r})
    from integrations.service_tools.gpu_worker import run_worker
    _before = 'torch' in sys.modules

    def load():
        if {model_uses_torch!r}:
            import torch
        return None

    def handle(model, request):
        return {{'before': _before, 'torch_loaded': 'torch' in sys.modules}}

    run_worker('probe', load, handle)
""")


def _fake_torch(root, body):
    pkg = root / 'torch'
    pkg.mkdir()
    (pkg / '__init__.py').write_text(textwrap.dedent(body), encoding='utf-8')
    return str(root)


def _run(tmp_path, torch_body, model_uses_torch):
    fake = _fake_torch(tmp_path, torch_body)
    script = _WORKER.format(repo=_REPO, fake=fake,
                            model_uses_torch=model_uses_torch)
    done = subprocess.run(
        [sys.executable, '-c', script],
        input='{"op": "ask"}\n{"op": "shutdown"}\n',
        capture_output=True, text=True, timeout=300, cwd=str(tmp_path))
    lines = [ln.strip() for ln in done.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 3, (done.returncode, done.stdout, done.stderr[-2000:])
    assert lines[0].startswith('__WORKER_VRAM_GB__'), lines
    assert lines[1] == '__WORKER_READY__', lines
    return float(lines[0][len('__WORKER_VRAM_GB__'):]), json.loads(lines[2])


_TORCH_WITH_2_GB = """
    class cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def memory_allocated(device):
            return 2 * 1024 ** 3
"""


def test_a_model_that_never_loaded_torch_leaves_it_unloaded(tmp_path):
    vram, answer = _run(tmp_path, _TORCH_WITH_2_GB, model_uses_torch=False)
    assert (answer['before'], answer['torch_loaded']) == (False, False)
    assert vram == 0.0


def test_a_model_that_loaded_torch_still_reports_its_memory(tmp_path):
    vram, answer = _run(tmp_path, _TORCH_WITH_2_GB, model_uses_torch=True)
    assert answer['torch_loaded'] is True
    assert vram == 2.0


def test_torch_without_a_gpu_reports_nothing(tmp_path):
    vram, _ = _run(tmp_path, """
        class cuda:
            @staticmethod
            def is_available():
                return False
    """, model_uses_torch=True)
    assert vram == 0.0


def test_a_torch_that_cannot_answer_reports_nothing_and_the_worker_comes_up(tmp_path):
    vram, answer = _run(tmp_path, """
        class cuda:
            @staticmethod
            def is_available():
                raise RuntimeError('driver went away')
    """, model_uses_torch=True)
    assert vram == 0.0
    assert answer['torch_loaded'] is True
