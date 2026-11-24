"""The catalog's ``downloaded`` flag is filled by the loader that owns the row.

Owner, 2026-11-24: "on a fresh install what gets auto setup shd be tested,
it shd be sensible defaults with single source of truth of what exists in
disk, what was setup already".

The flag is meant to be that one answer.  ``ModelOrchestrator.__init__``
scans it through the registered loaders, but no loader is registered yet at
that point (Nunba registers its LLM/TTS/STT/VLM loaders after building the
orchestrator), ``register_loader`` never scanned, and ``ModelEntry.to_dict``
drops the flag on save.  So after every boot every non-api, non-pip row read
``downloaded=False`` whatever was on disk, and ``select_best``'s downloaded
bonus ranked on nothing.  Live 2026-11-24: the desktop's speech pick was a
faster-whisper size that was not on disk while Whisper small and medium sat
on disk unused.

The orchestrator is built on a fake catalog, so no catalog file in the
person's home is read or written.
"""
from types import SimpleNamespace

from integrations.service_tools.model_orchestrator import (
    ModelLoader, ModelOrchestrator,
)


class _Catalog:
    """The two reads the orchestrator's scan makes, over a fixed row list."""

    def __init__(self, rows):
        self._rows = rows

    def list_all(self):
        return list(self._rows)

    def list_by_type(self, model_type):
        return [r for r in self._rows if r.model_type == model_type]


class _DiskLoader(ModelLoader):
    """Answers "on disk" from a fixed set of row ids, the way a real loader
    answers from the row's files."""

    def __init__(self, on_disk):
        self.on_disk = set(on_disk)
        self.asked = []

    def load(self, entry, run_mode):
        return False

    def unload(self, entry):
        pass

    def is_downloaded(self, entry):
        self.asked.append(entry.id)
        return entry.id in self.on_disk


def _row(row_id, model_type, downloaded=False, source='huggingface'):
    return SimpleNamespace(id=row_id, model_type=model_type, source=source,
                           downloaded=downloaded, files={}, repo_id='')


def test_a_loader_registered_after_the_orchestrator_fills_its_rows():
    small = _row('stt-small', 'stt')
    medium = _row('stt-medium', 'stt')
    orch = ModelOrchestrator(catalog=_Catalog([small, medium]))

    orch.register_loader('stt', _DiskLoader(on_disk={'stt-small'}))

    assert small.downloaded is True
    assert medium.downloaded is False


def test_a_row_the_loader_finds_gone_reads_not_downloaded():
    """A flag left True by an earlier answer is replaced by the loader's."""
    gone = _row('stt-gone', 'stt', downloaded=True)
    orch = ModelOrchestrator(catalog=_Catalog([gone]))

    orch.register_loader('stt', _DiskLoader(on_disk=set()))

    assert gone.downloaded is False


def test_only_the_registered_type_is_asked():
    stt = _row('stt-a', 'stt')
    tts = _row('tts-a', 'tts', downloaded=True)
    orch = ModelOrchestrator(catalog=_Catalog([stt, tts]))
    loader = _DiskLoader(on_disk={'stt-a', 'tts-a'})

    orch.register_loader('stt', loader)

    assert loader.asked == ['stt-a']
    assert tts.downloaded is True


def test_an_api_row_stays_downloaded():
    """API rows have nothing on disk to find; the scan has always called
    them downloaded, and registering a loader must not change that."""
    api = _row('stt-api', 'stt', source='api', downloaded=True)
    orch = ModelOrchestrator(catalog=_Catalog([api]))

    orch.register_loader('stt', _DiskLoader(on_disk=set()))

    assert api.downloaded is True
