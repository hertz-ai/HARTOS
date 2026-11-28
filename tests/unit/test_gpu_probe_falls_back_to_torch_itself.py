"""When the vendor tool gives nothing, the GPU probe asks torch itself.

Until 2026-11-28 core's import guard loaded transformers and torch in every
process, so the probe's "PyTorch, only if already imported" stage always had
torch.  With that side effect gone (5d5c17dda), an nvidia-smi that times out
under load would have read as "no GPU" on a machine that has one (review of
5d5c17dda).  The stage now imports torch when it is installed.  A machine
with no vendor tool at all never reaches it, so it still imports nothing.

Each case runs in a fresh interpreter with a small package named ``torch``
first on the path, so "was torch imported" is a fact about the process.

    python -m pytest tests/unit/test_gpu_probe_falls_back_to_torch_itself.py -q --noconftest
"""
import json
import os
import subprocess
import sys
import textwrap

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TORCH = """
    class _Props:
        total_memory = 8 * 1024 ** 3

    class cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def get_device_properties(index):
            return _Props()

        @staticmethod
        def memory_allocated(index):
            return 2 * 1024 ** 3

        @staticmethod
        def get_device_name(index):
            return 'Fake GPU'
{device_wide}

    __version__ = '9.9'
"""

_SCRIPT = """
    import json, subprocess, sys
    sys.path.insert(0, {repo!r})
    sys.path.insert(0, {fake!r})
    import integrations.service_tools.vram_manager   # the package attribute
    vm = sys.modules['integrations.service_tools.vram_manager']  # is the singleton
    before = 'torch' in sys.modules

    def tool_gives_nothing(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='vendor tool', timeout=15)
    import core.subprocess_safe            # the probe imports it from here
    core.subprocess_safe.run_bounded = tool_gives_nothing

    mgr = vm.VRAMManager.__new__(vm.VRAMManager)
    mgr._gpu_info = None
    mgr._vendor_tools_absent = {absent!r}
    info = mgr._probe_gpu()
    print(json.dumps({{'before': before, 'after': 'torch' in sys.modules,
                      'name': info.get('name'), 'total_gb': info.get('total_gb'),
                      'free_gb': info.get('free_gb'),
                      'cuda': info.get('cuda_available')}}))
"""


# Other processes hold 7 of the 8 GB; this one holds 2 of those.
_DEVICE_WIDE = """
        @staticmethod
        def mem_get_info(index):
            return (1 * 1024 ** 3, 8 * 1024 ** 3)
"""

_DEVICE_WIDE_FAILS = """
        @staticmethod
        def mem_get_info(index):
            raise RuntimeError('driver cannot be asked')
"""


def _probe(tmp_path, vendor_tools_absent, device_wide=''):
    pkg = tmp_path / 'torch'
    pkg.mkdir()
    (pkg / '__init__.py').write_text(
        textwrap.dedent(_TORCH.replace('{device_wide}', device_wide)),
        encoding='utf-8')
    script = textwrap.dedent(_SCRIPT).format(
        repo=_REPO, fake=str(tmp_path), absent=vendor_tools_absent)
    done = subprocess.run([sys.executable, '-c', script], capture_output=True,
                          text=True, timeout=300, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_vendor_tool_that_times_out_still_finds_the_gpu(tmp_path):
    got = _probe(tmp_path, vendor_tools_absent=False)
    assert got['before'] is False
    # This fake torch has no mem_get_info: free memory is not known.
    assert (got['name'], got['total_gb'], got['free_gb'], got['cuda']) == (
        'Fake GPU', 8.0, 0.0, True)


def test_a_machine_with_no_vendor_tool_imports_nothing(tmp_path):
    got = _probe(tmp_path, vendor_tools_absent=True)
    assert (got['before'], got['after']) == (False, False)
    assert (got['name'], got['cuda']) == (None, False)


def test_free_memory_is_the_devices_not_this_processes(tmp_path):
    """A card other processes have filled is not read as free."""
    got = _probe(tmp_path, vendor_tools_absent=False, device_wide=_DEVICE_WIDE)
    assert (got['total_gb'], got['free_gb']) == (8.0, 1.0)


def test_a_driver_that_cannot_be_asked_reports_no_free_memory(tmp_path):
    """Not this process's own count: that reads a full card as free."""
    got = _probe(tmp_path, vendor_tools_absent=False,
                 device_wide=_DEVICE_WIDE_FAILS)
    assert (got['name'], got['total_gb'], got['free_gb']) == ('Fake GPU', 8.0, 0.0)
