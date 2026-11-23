"""A voice worker started for a reply loads the voice that reply asks for,
and no other.

WHAT WENT WRONG (live walk, 23 Nov 2026, installed Nunba, local 4B)
  The speech agent's first Tamil reply was heard 60 s after its text.  The
  Tamil voice (facebook/mms-tts-tam) was already on disk: the mms_tts worker
  was starting cold, and its _load loaded the English voice first
  ("loaded in 31.1s"), then the request loaded Tamil.

THE RULE
  _load picks the device and loads no voice.  A request loads the voice for
  its own language, once; the next request in that language reuses it.  With
  room on the GPU that voice goes onto cuda; a voice that cannot move there
  runs the worker on the CPU, as the English voice did in _load.

    python -m pytest tests/unit/test_a_cold_voice_worker_loads_only_the_voice_asked_for.py -q --noconftest
"""
import sys
import types

import pytest

TAMIL = 'அர்ஜுன்!'
ENGLISH = 'Arjun, look here!'


class _Waveform:
    """outputs.waveform[0].detach().cpu().numpy()"""

    def __getitem__(self, _i):
        return self

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return [0.0] * 1600


@pytest.fixture
def vits(monkeypatch):
    """transformers' VITS classes, recording which voices load and where
    each model and its inputs go; soundfile, recording what is written."""
    seen = {'loaded': [], 'moved': [], 'inputs_on': [], 'written': [],
            'gpu_refuses': False}

    class _Model:
        config = types.SimpleNamespace(sampling_rate=16000)

        def __init__(self, repo):
            self.repo = repo

        def to(self, device):
            seen['moved'].append((self.repo, device))
            if seen['gpu_refuses']:
                raise RuntimeError('CUDA error: out of memory')
            return self

        def __call__(self, **_inputs):
            return types.SimpleNamespace(waveform=_Waveform())

    class VitsModel:
        @staticmethod
        def from_pretrained(repo):
            seen['loaded'].append(repo)
            return _Model(repo)

    class _Ids:
        def to(self, device):
            seen['inputs_on'].append(device)
            return self

    class _Tokenizer:
        is_uroman = False

        def __call__(self, text, return_tensors=None):
            return {'input_ids': _Ids()}

    class VitsTokenizer:
        @staticmethod
        def from_pretrained(repo):
            return _Tokenizer()

    # Stand-ins: the worker imports only these names, and the real
    # package's import needs a matching huggingface_hub.
    tf = types.ModuleType('transformers')
    tf.VitsModel = VitsModel
    tf.VitsTokenizer = VitsTokenizer
    monkeypatch.setitem(sys.modules, 'transformers', tf)
    sf = types.ModuleType('soundfile')
    sf.write = lambda path, wave, sr: seen['written'].append((path, sr))
    monkeypatch.setitem(sys.modules, 'soundfile', sf)
    return seen


def _device(monkeypatch, device):
    """Where the router says mms_tts runs: 'cuda' with room, else 'cpu'."""
    import integrations.channels.media.tts_router as router
    monkeypatch.setattr(router, 'gpu_preferred_device', lambda _engine: device)


def _speak(state, text, language, tmp_path):
    from integrations.service_tools import mms_tts_tool
    return mms_tts_tool._synthesize(state, {
        'text': text, 'language': language,
        'output_path': str(tmp_path / f'{language}.wav')})


def test_a_cold_worker_loads_no_voice_before_it_is_asked(monkeypatch, vits):
    _device(monkeypatch, 'cpu')
    from integrations.service_tools import mms_tts_tool
    state = mms_tts_tool._load()
    assert vits['loaded'] == [], (
        f"the worker loaded {vits['loaded']} before any reply asked for a voice")
    assert state.cache == {} and state.device == 'cpu'


def test_a_tamil_reply_loads_only_the_tamil_voice(monkeypatch, vits, tmp_path):
    _device(monkeypatch, 'cpu')
    from integrations.service_tools import mms_tts_tool
    state = mms_tts_tool._load()
    out = _speak(state, TAMIL, 'ta', tmp_path)
    assert 'error' not in out, out
    assert out['iso3'] == 'tam' and vits['written'], out
    assert vits['loaded'] == ['facebook/mms-tts-tam'], (
        f"a Tamil reply on a cold worker loaded {vits['loaded']}")


def test_an_english_reply_loads_only_the_english_voice(monkeypatch, vits, tmp_path):
    _device(monkeypatch, 'cpu')
    from integrations.service_tools import mms_tts_tool
    state = mms_tts_tool._load()
    out = _speak(state, ENGLISH, 'en', tmp_path)
    assert 'error' not in out, out
    assert vits['loaded'] == ['facebook/mms-tts-eng']


def test_the_next_line_in_that_language_reuses_its_voice(monkeypatch, vits, tmp_path):
    _device(monkeypatch, 'cpu')
    from integrations.service_tools import mms_tts_tool
    state = mms_tts_tool._load()
    _speak(state, TAMIL, 'ta', tmp_path)
    _speak(state, TAMIL, 'ta', tmp_path)
    assert vits['loaded'] == ['facebook/mms-tts-tam']
    _speak(state, ENGLISH, 'en', tmp_path)
    assert vits['loaded'] == ['facebook/mms-tts-tam', 'facebook/mms-tts-eng']
    assert set(state.cache) == {'tam', 'eng'}


def test_with_room_on_the_gpu_the_voice_asked_for_goes_onto_cuda(monkeypatch, vits, tmp_path):
    _device(monkeypatch, 'cuda')
    from integrations.service_tools import mms_tts_tool
    state = mms_tts_tool._load()
    out = _speak(state, TAMIL, 'ta', tmp_path)
    assert 'error' not in out, out
    assert vits['moved'] == [('facebook/mms-tts-tam', 'cuda')]
    assert state.device == 'cuda' and out['device'] == 'cuda'
    assert vits['inputs_on'] == ['cuda']


def test_a_voice_that_cannot_move_to_the_gpu_runs_the_worker_on_the_cpu(
        monkeypatch, vits, tmp_path):
    _device(monkeypatch, 'cuda')
    vits['gpu_refuses'] = True
    from integrations.service_tools import mms_tts_tool
    state = mms_tts_tool._load()
    out = _speak(state, TAMIL, 'ta', tmp_path)
    assert 'error' not in out, out
    assert state.device == 'cpu' and out['device'] == 'cpu'
    # The model stayed on the CPU, so its inputs must too.
    assert vits['inputs_on'] == []
