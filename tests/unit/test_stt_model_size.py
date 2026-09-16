"""The faster-whisper model size follows the catalog (2026-09-16 RCA).

Owner: "STT works but it's not accurate and does not work for all
languages".  Measured on an RTX 3070 box: the engine that runs
(faster-whisper) loaded 'base' on CPU int8 on every boot.  The size came from
``HEVOLVE_STT_MODEL_SIZE`` with default 'base'; the comment beside it said the
admin Model Management UI sets that env, but nothing in either repo ever did,
no STT loader is registered with the orchestrator, and the catalog's own pick
for the box (``select_whisper_model()`` -> whisper medium) was consulted only
by the sherpa-onnx leg, which never runs while faster-whisper imports.

The rule now: ``faster_whisper_model_size()`` is the ONE resolver -- the env
override when an admin set it, else the catalog's best faster-whisper entry
for this hardware (``ModelOrchestrator.select_best('stt')`` with the
sherpa-onnx ids excluded, mapped through ``_CATALOG_ID_TO_FASTER_WHISPER_SIZE``),
else the CPU default.  The parent decides once per worker life -- at the
request that (re)spawns the worker -- aligns the worker's VRAM budget key to
that size, and stamps ``model_size`` on every request, so the worker never
flips sizes mid-life as free VRAM moves (a flip = a reload, and for a size
never fetched a download inside the request window, #677).

Behavioural where it can be: the real resolver against a fake orchestrator,
the real ``_stt_call`` against a fake ToolWorker, the real worker dispatch
against a fake engine.

    python -m pytest tests/unit/test_stt_model_size.py --noconftest -q
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import integrations.service_tools.whisper_tool as wt


def _orch(pick_id):
    """A fake orchestrator whose select_best records its arguments."""
    calls = []

    class _Orch:
        def select_best(self, model_type, **kw):
            calls.append((model_type, kw))
            return SimpleNamespace(id=pick_id) if pick_id else None
    return _Orch(), calls


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv('HEVOLVE_STT_MODEL_SIZE', raising=False)


# ── the resolver ───────────────────────────────────────────────────────────

def test_size_is_the_catalogs_best_faster_whisper_entry():
    orch, calls = _orch('stt-faster-whisper-medium')
    with patch('integrations.service_tools.model_orchestrator.get_orchestrator',
               return_value=orch):
        assert wt.faster_whisper_model_size() == 'medium'
    (model_type, kw), = calls
    assert model_type == 'stt'
    # The sherpa-onnx entries are another engine's namespace: excluded so the
    # catalog answers with a size THIS engine can load.
    assert set(kw['exclude']) == set(wt._CATALOG_ID_TO_SHERPA)


def test_env_override_wins_and_skips_the_catalog():
    orch, calls = _orch('stt-faster-whisper-medium')
    with patch.dict('os.environ', {'HEVOLVE_STT_MODEL_SIZE': 'large-v3'}), \
         patch('integrations.service_tools.model_orchestrator.get_orchestrator',
               return_value=orch):
        assert wt.faster_whisper_model_size() == 'large-v3'
    assert calls == []


def test_no_fitting_entry_or_a_broken_catalog_gives_the_cpu_default():
    orch, _ = _orch(None)
    with patch('integrations.service_tools.model_orchestrator.get_orchestrator',
               return_value=orch):
        assert wt.faster_whisper_model_size() == wt.STT_CPU_MODEL_SIZE
    with patch('integrations.service_tools.model_orchestrator.get_orchestrator',
               side_effect=RuntimeError('catalog unreadable')):
        assert wt.faster_whisper_model_size() == wt.STT_CPU_MODEL_SIZE


def test_every_catalog_size_has_a_vram_budget_key():
    """The parent books VRAM under a key that matches the size it will load
    (was: 'whisper_base' for every size -- 0.2 GB booked for a 1.5 GB medium)."""
    from integrations.service_tools.vram_manager import VRAM_BUDGETS
    for size in wt._CATALOG_ID_TO_FASTER_WHISPER_SIZE.values():
        key = wt._vram_key_for_size(size)
        assert key in VRAM_BUDGETS, (size, key)
    assert wt._vram_key_for_size('base') == 'whisper_base'
    assert wt._vram_key_for_size('medium') == 'whisper_medium'
    assert wt._vram_key_for_size('large-v3') == 'whisper_large'
    # An admin-typed size the catalog does not know books the CPU default.
    assert wt._vram_key_for_size('distil-large-v3') == 'whisper_base'


# ── the parent's one call site ─────────────────────────────────────────────

class _FakeTool:
    """ToolWorker double: alive-ness is scripted, requests are recorded."""

    def __init__(self, alive):
        self._alive = list(alive)
        self.vram_budget = 'whisper_base'
        self.requests = []

    def is_alive(self):
        return self._alive.pop(0) if self._alive else True

    def call(self, req):
        self.requests.append(dict(req))
        return {'raw_json': json.dumps({'text': 'ok', 'language': 'en'}),
                'text': 'ok', 'language': 'en'}


def test_size_is_decided_at_spawn_and_stamped_on_every_request(monkeypatch):
    tool = _FakeTool(alive=[False, True, True])
    monkeypatch.setattr(wt, '_stt_tool', tool)
    monkeypatch.setattr(wt, '_stt_worker_size', None)
    sizes = iter(['medium', 'small', 'small'])
    monkeypatch.setattr(wt, 'faster_whisper_model_size', lambda: next(sizes))

    wt._stt_call({'op': 'transcribe', 'audio_path': 'a.wav', 'language': None})
    wt._stt_call({'op': 'detect_language', 'audio_path': 'b.wav'})
    wt._stt_call({'op': 'transcribe', 'audio_path': 'c.wav', 'language': 'ta'})

    # Decided once (the worker was down for the first call only) ...
    assert [r['model_size'] for r in tool.requests] == ['medium', 'medium', 'medium']
    # ... and the VRAM booking key follows that decision.
    assert tool.vram_budget == 'whisper_medium'


def test_a_respawn_decides_again(monkeypatch):
    tool = _FakeTool(alive=[False, False])
    monkeypatch.setattr(wt, '_stt_tool', tool)
    monkeypatch.setattr(wt, '_stt_worker_size', None)
    sizes = iter(['medium', 'small'])
    monkeypatch.setattr(wt, 'faster_whisper_model_size', lambda: next(sizes))

    wt._stt_call({'op': 'transcribe', 'audio_path': 'a.wav'})
    wt._stt_call({'op': 'transcribe', 'audio_path': 'b.wav'})
    assert [r['model_size'] for r in tool.requests] == ['medium', 'small']
    assert tool.vram_budget == 'whisper_small'


def test_all_three_public_producers_go_through_stt_call(monkeypatch):
    """whisper_transcribe, whisper_detect_language and the streaming buffer
    are the only producers of STT requests; each must carry the size."""
    tool = _FakeTool(alive=[False])
    monkeypatch.setattr(wt, '_stt_tool', tool)
    monkeypatch.setattr(wt, '_stt_worker_size', None)
    monkeypatch.setattr(wt, 'faster_whisper_model_size', lambda: 'small')

    wt.whisper_transcribe('a.wav', 'en')
    wt.whisper_detect_language('a.wav')
    import io
    buf = io.BytesIO(b'\x00\x01' * wt.STREAM_SAMPLE_RATE * 2)   # 2 s of audio
    wt._transcribe_buffer(buf, language=None)

    assert [r['op'] for r in tool.requests] == ['transcribe', 'detect_language', 'transcribe']
    assert all(r['model_size'] == 'small' for r in tool.requests)


def test_no_other_direct_caller_of_the_worker():
    """AST guard: ``_stt_tool.call(`` appears exactly once in the module -- inside
    ``_stt_call``.  A second direct caller would ship a request with no size."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(wt))
    sites = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'call'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == '_stt_tool'):
            sites.append(node.lineno)
    assert len(sites) == 1, sites
    src_lines = inspect.getsource(wt).splitlines()
    # The one site lives inside _stt_call.
    start = next(i for i, line in enumerate(src_lines, 1)
                 if line.startswith('def _stt_call('))
    assert sites[0] > start


# ── the worker side ────────────────────────────────────────────────────────

def test_worker_dispatch_hands_the_stamped_size_to_the_engine(monkeypatch):
    seen = {}

    def _impl(audio_path, language=None, model_size=None):
        seen['transcribe'] = (audio_path, language, model_size)
        return json.dumps({'text': 'hi', 'language': 'en'})

    def _detect(audio_path, model_size=None):
        seen['detect'] = (audio_path, model_size)
        return json.dumps({'language': 'ta', 'probability': 0.9})

    monkeypatch.setattr(wt, '_transcribe_impl', _impl)
    monkeypatch.setattr(wt, '_detect_language_impl', _detect)
    wt._synthesize(None, {'op': 'transcribe', 'audio_path': 'a.wav',
                          'language': None, 'model_size': 'medium'})
    wt._synthesize(None, {'op': 'detect_language', 'audio_path': 'b.wav',
                          'model_size': 'medium'})
    assert seen['transcribe'] == ('a.wav', None, 'medium')
    assert seen['detect'] == ('b.wav', 'medium')


def test_engine_loads_the_stamped_size_and_resolves_when_unstamped(monkeypatch):
    loads = []

    class _Model:
        def transcribe(self, path, **kw):
            return iter([]), SimpleNamespace(language='en')

    def _get(model_size):
        loads.append(model_size)
        return _Model()

    monkeypatch.setattr(wt, '_get_faster_whisper_model', _get)
    monkeypatch.setattr(wt, '_whisper_load_breaker', None)
    monkeypatch.setattr(wt, 'faster_whisper_model_size', lambda: 'small')
    wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
    wt._faster_whisper_transcribe('a.wav', None)          # a legacy caller
    assert loads == ['medium', 'small']
