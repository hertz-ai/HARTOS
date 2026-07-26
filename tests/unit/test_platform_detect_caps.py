"""Behavioral test for hart_sdk.platform_detect.get_capabilities() GPU branch (#135-residual).

vram_manager.detect_gpu() returns the key `cuda_available` (not `available`).
get_capabilities() previously read `gpu.get('available')`, which is always
False, so a real GPU node never advertised the `vision` capability. These tests
inject a fake vram_manager (robust to torch not being installed in CI) and
assert the observable capability list.
"""
import sys
import types
import importlib


def _install_fake_vram(monkeypatch, cuda_available):
    mod = types.ModuleType('integrations.service_tools.vram_manager')

    class _VM:
        @staticmethod
        def detect_gpu():
            return {
                'name': 'RTX 3070' if cuda_available else 'cpu',
                'total_gb': 8.0 if cuda_available else 0.0,
                'free_gb': 3.0 if cuda_available else 0.0,
                'cuda_available': cuda_available,
            }

    mod.vram_manager = _VM()
    # get_capabilities does `from integrations.service_tools.vram_manager import vram_manager`
    # at call time, so swapping the module in sys.modules is sufficient and avoids
    # importing the real (torch-heavy) module.
    monkeypatch.setitem(sys.modules, 'integrations.service_tools.vram_manager', mod)


def _fresh_module():
    sys.modules.pop('hart_sdk.platform_detect', None)
    return importlib.import_module('hart_sdk.platform_detect')


def test_vision_advertised_when_gpu_present(monkeypatch):
    monkeypatch.setenv('HART_FORCE_CPU', '')
    _install_fake_vram(monkeypatch, cuda_available=True)
    pd = _fresh_module()
    caps = pd.get_capabilities()
    assert 'vision' in caps, f"GPU node must advertise 'vision'; got {caps}"


def test_vision_absent_when_cpu_only(monkeypatch):
    monkeypatch.setenv('HART_FORCE_CPU', '')
    _install_fake_vram(monkeypatch, cuda_available=False)
    pd = _fresh_module()
    caps = pd.get_capabilities()
    assert 'vision' not in caps, f"CPU-only node must not advertise 'vision'; got {caps}"
