"""A voice the engine registry says also runs on the CPU loads on the CPU when
the GPU has no room for it.

WHAT WENT WRONG (review of Nunba f885318e, 15 Nov 2026)
  ENGINE_REGISTRY marks kokoro, melotts and mms_tts GPU_PREFERRED ("CPU works,
  GPU faster"), and the router's own rule reads it that way: on a GPU without
  room the engine's device is 'cpu' (tts_router: 'gpu' if has_gpu and
  _can_fit_on_gpu(eid) else 'cpu').  But the workers' loaders took CUDA
  whenever torch saw a GPU (kokoro_tool, melotts_tool, mms_tts_tool _load), so
  on the owner's desktop, with the local LLMs holding all but 1 GB of an 8 GB
  GPU, an engine the registry promised would run on the CPU loaded onto the
  full GPU instead.  mms_tts alone fell back, and only when moving the model
  raised.

THE RULE
  A GPU_PREFERRED worker loads on CUDA only when the GPU has room for it
  (tts_router._can_fit_on_gpu, the router's own check); otherwise on the CPU.
  With room, nothing changes.

    python -m pytest tests/unit/test_a_gpu_preferred_voice_loads_on_the_cpu_when_the_gpu_is_full.py -q --noconftest
"""
import sys
import types

import pytest


@pytest.fixture
def gpu(monkeypatch):
    """A GPU torch can see; whether it has room is set per test."""
    import torch
    import integrations.channels.media.tts_router as router
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: True)
    room = {'fits': False, 'asked': []}

    def _fits(engine_id):
        room['asked'].append(engine_id)
        return room['fits']

    monkeypatch.setattr(router, '_can_fit_on_gpu', _fits)
    return room


def _fake_kokoro(monkeypatch):
    seen = {}

    class KPipeline:
        def __init__(self, lang_code='a', device=None):
            seen['device'] = device

    mod = types.ModuleType('kokoro')
    mod.KPipeline = KPipeline
    monkeypatch.setitem(sys.modules, 'kokoro', mod)
    return seen


def _fake_melo(monkeypatch):
    seen = {}

    class TTS:
        def __init__(self, language='EN', device=None):
            seen['device'] = device

    melo = types.ModuleType('melo')
    api = types.ModuleType('melo.api')
    api.TTS = TTS
    melo.api = api
    monkeypatch.setitem(sys.modules, 'melo', melo)
    monkeypatch.setitem(sys.modules, 'melo.api', api)
    return seen


def _fake_vits(monkeypatch):
    moved = []

    class _Model:
        def to(self, device):
            moved.append(device)
            return self

    class VitsModel:
        @staticmethod
        def from_pretrained(repo):
            return _Model()

    class VitsTokenizer:
        @staticmethod
        def from_pretrained(repo):
            return object()

    # A stand-in module: the loader imports only these two names, and the
    # real package's import needs a matching huggingface_hub.
    mod = types.ModuleType('transformers')
    mod.VitsModel = VitsModel
    mod.VitsTokenizer = VitsTokenizer
    monkeypatch.setitem(sys.modules, 'transformers', mod)
    return moved


def test_kokoro_loads_on_the_cpu_when_the_gpu_is_full(monkeypatch, gpu):
    from integrations.service_tools import kokoro_tool
    seen = _fake_kokoro(monkeypatch)
    state = kokoro_tool._load()
    assert seen['device'] == 'cpu' and state['device'] == 'cpu', (
        f"kokoro loaded on {seen['device']} with no room on the GPU")
    assert 'kokoro' in gpu['asked']


def test_melotts_loads_on_the_cpu_when_the_gpu_is_full(monkeypatch, gpu):
    from integrations.service_tools import melotts_tool
    seen = _fake_melo(monkeypatch)
    melotts_tool._load()
    assert seen['device'] == 'cpu', f"melotts loaded on {seen['device']} with no room on the GPU"
    assert 'melotts' in gpu['asked']


def test_mms_tts_stays_on_the_cpu_when_the_gpu_is_full(monkeypatch, gpu):
    from integrations.service_tools import mms_tts_tool
    moved = _fake_vits(monkeypatch)
    state = mms_tts_tool._load()
    assert 'cuda' not in moved, "mms_tts moved its model onto a full GPU"
    assert getattr(state, 'device', None) == 'cpu'
    assert 'mms_tts' in gpu['asked']


def test_with_room_on_the_gpu_each_voice_loads_on_cuda(monkeypatch, gpu):
    from integrations.service_tools import kokoro_tool, melotts_tool, mms_tts_tool
    gpu['fits'] = True
    k = _fake_kokoro(monkeypatch)
    m = _fake_melo(monkeypatch)
    moved = _fake_vits(monkeypatch)
    kokoro_tool._load()
    melotts_tool._load()
    state = mms_tts_tool._load()
    assert (k['device'], m['device']) == ('cuda', 'cuda')
    # mms_tts loads no voice at start; the voice a request asks for goes
    # onto cuda (test_a_cold_voice_worker_loads_only_the_voice_asked_for).
    assert state.device == 'cuda' and moved == []
