"""An unload stops the sidecar a load started (build #6, 2026-11-27 02:36).

POST /api/admin/models/video_gen-avatarforcing/unload answered success and logged "Unloaded
video_gen-avatarforcing", while the face's sidecar kept 4.8 GB of commit until its process tree was
killed by hand.  The row has no registered loader (Nunba registers LLM, TTS, STT and VLM loaders only),
so it loads through _load_generic (RuntimeToolManager.setup_tool), and its unload reached nothing.
Now an unload with no loader stops the same sidecar, by the same name rule.

The orchestrator is built on a fake catalog, so no catalog file in the person's home is read or written.
"""
from types import SimpleNamespace

from integrations.service_tools import runtime_manager as rm
from integrations.service_tools.model_orchestrator import ModelLoader, ModelOrchestrator


class _Catalog:
    def __init__(self, rows):
        self._rows = rows

    def list_all(self):
        return list(self._rows)

    def list_by_type(self, model_type):
        return [r for r in self._rows if r.model_type == model_type]


class _Loader(ModelLoader):
    def __init__(self):
        self.unloaded = []

    def load(self, entry, run_mode):
        return False

    def unload(self, entry):
        self.unloaded.append(entry.id)


def _row(row_id, model_type, source='github'):
    return SimpleNamespace(id=row_id, model_type=model_type, source=source, downloaded=True,
                           loaded=True, files={}, repo_id='')


def test_a_row_with_no_loader_stops_the_sidecar_its_load_started(monkeypatch):
    stopped = []
    monkeypatch.setattr(rm.runtime_tool_manager, 'stop_tool', lambda name: stopped.append(name))
    face = _row('video_gen-avatarforcing', 'video_gen')
    orch = ModelOrchestrator(catalog=_Catalog([face]))
    orch._dispatch_unload(face)
    assert stopped == ['avatarforcing']


def test_a_row_with_a_loader_is_unloaded_by_it_and_no_sidecar_is_stopped(monkeypatch):
    stopped = []
    monkeypatch.setattr(rm.runtime_tool_manager, 'stop_tool', lambda name: stopped.append(name))
    stt = _row('stt-whisper', 'stt')
    orch = ModelOrchestrator(catalog=_Catalog([stt]))
    loader = _Loader()
    orch.register_loader('stt', loader)
    orch._dispatch_unload(stt)
    assert loader.unloaded == ['stt-whisper'] and stopped == []


def test_a_row_naming_no_runtime_tool_stops_nothing(monkeypatch):
    stopped = []
    monkeypatch.setattr(rm.runtime_tool_manager, 'stop_tool', lambda name: stopped.append(name))
    other = _row('video_gen-not-a-tool', 'video_gen', source='pip')
    orch = ModelOrchestrator(catalog=_Catalog([other]))
    orch._dispatch_unload(other)
    assert stopped == []


def test_a_stop_that_fails_never_breaks_the_unload(monkeypatch):
    def boom(name):
        raise RuntimeError('gone')
    monkeypatch.setattr(rm.runtime_tool_manager, 'stop_tool', boom)
    face = _row('video_gen-avatarforcing', 'video_gen')
    orch = ModelOrchestrator(catalog=_Catalog([face]))
    orch._dispatch_unload(face)  # logged, not raised
