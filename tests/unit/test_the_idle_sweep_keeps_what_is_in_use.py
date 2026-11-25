"""The idle sweep unloads only what sat idle for its own row's time, and only
through the loaders HARTOS owns (review of 5ecf725e8, hartos-17).

5ecf725e8 sent every catalog row whose type has a loader through that loader
on eviction (model_lifecycle._do_unload), while every lifecycle record got a
flat 300 s and nothing stamped use.  The reviewer's fake-clock probe
(D:/claude_scratch/rv_5ecf/probe_idle.py) measured it: at t+310 s the speaker,
the phonetic AND the stt-faster-whisper-small rows were unloaded through their
loaders.  So about 300 s after the Agent page loaded speech-to-text, Whisper
was stopped, even mid-session, and the speaker model was dropped every five
minutes.

The contract:
  * a lifecycle record takes its row's idle_timeout_s, and 0 means no idle
    sweep (the speaker row);
  * the speaker and phonetic models stamp their own use on their record, so a
    model in use never reads as idle;
  * only rows of the loaders HARTOS registers (speaker, phonetic) are unloaded
    through their loader; Nunba's STT, TTS, VLM and LLM rows go to stop_tool
    as before.

Real _register_lifecycle, _update_priorities, _evict_idle_models, _do_unload
and ModelOrchestrator.unload; fake catalog, fake loaders, fake clock.

    venv/Scripts/python.exe -m pytest tests/unit/test_the_idle_sweep_keeps_what_is_in_use.py -v -p no:cacheprovider --no-header
"""
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

from integrations.service_tools import model_lifecycle as ml
from integrations.service_tools import model_orchestrator as mo

SPEAKER = 'speaker-campplus-zh-en'
PHONETIC = 'phonetic-pasketti'
WHISPER = 'stt-faster-whisper-small'
T0 = 1_000_000.0


class _Catalog:
    def __init__(self, rows):
        self.rows = {r.id: r for r in rows}

    def get(self, model_id):
        return self.rows.get(model_id)

    def list_all(self):
        return list(self.rows.values())

    def list_by_type(self, model_type):
        return [r for r in self.rows.values() if r.model_type == model_type]

    def mark_unloaded(self, model_id):
        self.rows[model_id].loaded = False


def _row(model_id, model_type, idle):
    return types.SimpleNamespace(id=model_id, model_type=model_type, loaded=True,
                                 device='cpu', purposes=[], vram_gb=0.0,
                                 idle_timeout_s=idle, files={}, source='huggingface')


class _Loader(mo.ModelLoader):
    def __init__(self, unloaded):
        self.unloaded = unloaded

    def load(self, entry, run_mode):
        return True

    def unload(self, entry):
        self.unloaded.append(entry.id)


@pytest.fixture
def node(monkeypatch):
    """The three rows as their populators write them (speaker 0, phonetic the
    speaker's session window, faster-whisper small 300), each with a loader
    registered, loaded at T0."""
    from integrations.audio.voiceprint import IDLE_S
    rows = [_row(SPEAKER, 'speaker', 0), _row(PHONETIC, 'phonetic', IDLE_S),
            _row(WHISPER, 'stt', 300)]
    orch = mo.ModelOrchestrator(catalog=_Catalog(rows))
    unloaded = []
    for model_type in ('speaker', 'phonetic', 'stt'):
        orch._loaders[model_type] = _Loader(unloaded)
    monkeypatch.setattr(mo, '_orchestrator_instance', orch)
    mlm = ml.ModelLifecycleManager()
    mlm._persist_state_to_disk = MagicMock()
    mlm._emit_event = MagicMock()
    mlm._get_model_capabilities = MagicMock(return_value={})
    monkeypatch.setattr(ml, 'get_model_lifecycle_manager', lambda: mlm)
    rtm = MagicMock()
    monkeypatch.setattr('integrations.service_tools.runtime_manager.runtime_tool_manager', rtm)
    clock = {'now': T0}
    monkeypatch.setattr(ml, 'time', types.SimpleNamespace(
        time=lambda: clock['now'], monotonic=lambda: clock['now'], sleep=lambda s: None))
    for r in rows:
        orch._register_lifecycle(r)

    def at(seconds):
        clock['now'] = T0 + seconds
        mlm._update_priorities()
        mlm._evict_idle_models()
    return types.SimpleNamespace(mlm=mlm, unloaded=unloaded, rtm=rtm, at=at, clock=clock,
                                 idle_s=IDLE_S)


def test_whisper_is_not_stopped_by_the_sweep(node):
    node.at(310)
    assert WHISPER not in node.unloaded, 'the idle sweep stopped Whisper through its loader'
    # Its record goes to stop_tool as before, which stops no process for it.
    node.rtm.stop_tool.assert_called_once_with(WHISPER)


def test_a_row_with_no_idle_timeout_is_never_swept(node):
    for seconds in (310, 5_000, 100_000):
        node.at(seconds)
    assert SPEAKER not in node.unloaded
    assert node.mlm._models[SPEAKER].device != ml.ModelDevice.UNLOADED


def test_an_unused_phonetic_row_goes_after_its_own_idle_timeout(node):
    node.at(310)
    node.at(node.idle_s - 10)
    assert PHONETIC not in node.unloaded
    node.at(node.idle_s + 10)
    assert PHONETIC in node.unloaded


def test_a_phonetic_row_in_use_is_not_idle(node, monkeypatch):
    """Its use is stamped by the tool itself (phonetic_tool.phones)."""
    from integrations.service_tools import phonetic_tool
    monkeypatch.setattr(phonetic_tool, '_wanted', {}, raising=False)
    monkeypatch.setattr(phonetic_tool, '_tool', types.SimpleNamespace(
        is_alive=lambda: True, call=lambda request: {'phones': 'a m a'}))
    phonetic_tool.want_phones('person', True)
    node.at(node.idle_s - 100)
    assert phonetic_tool.phones('person', np.zeros(16000, dtype='float32')) == 'a m a'
    node.at(node.idle_s + 10)        # past its timeout from the load, 110 s from the use
    assert PHONETIC not in node.unloaded
    node.at(2 * node.idle_s)         # its timeout from the use
    assert PHONETIC in node.unloaded


def test_hearing_a_voice_stamps_the_speaker_record(node, monkeypatch):
    from integrations.audio import voiceprint
    monkeypatch.setattr(voiceprint, '_extractor', object())
    monkeypatch.setattr(voiceprint, '_read_audio',
                        lambda path: np.full(voiceprint.SAMPLE_RATE, 0.1, dtype='float32'))
    monkeypatch.setattr(voiceprint, '_embed_samples', lambda ex, samples: [1.0, 0.0, 0.0])
    voiceprint.forget('person')
    try:
        node.at(500)
        assert voiceprint.hear('person', 'a.wav', 'hi')['voice'] == 'A'
        assert node.mlm._models[SPEAKER].last_access_time == node.clock['now']
    finally:
        voiceprint.forget('person')
