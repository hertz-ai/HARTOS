"""C: the agent hears the sounds a child made, beside the words Whisper wrote.

Owner 2026-11-25: "a catalog entry, a tool that runs it alongside
multilingual Whisper, and lifecycle registration so it can borrow GPU memory
and give it back", the On Top of Pasketti phonetic model
(drivendata/pasketti-phonetic: WavLM Large with a CTC head, fp32 only, 16 kHz
mono in, IPA out, trained on English-speaking children).  Design note
design_1125, section C as amended by v3 (N1).

The contract:
  * the model is a catalog row of its own type ('phonetic'), set up on need
    through the orchestrator's 'phonetic' loader, which HARTOS registers on
    every orchestrator, its own and Nunba's;
  * an agent needs phones when its stored goal tags or its own goal text name
    a phones-only word (phonetic_tool.PHONES_WORDS); tool loading never reads
    that list;
  * that agent's turn asks model setup for the model and goes on; while it is
    loaded, each utterance heard on the person's device carries its phones
    beside the voice letter, A: "amma" [a m a], in the person's own turn seed
    only, never a daemon turn's;
  * hearing never starts the model, and a turn never waits for it: no model
    means no phones, and the turn goes on.

No weights, no worker process, no model server: the worker is a fake.

    venv/Scripts/python.exe -m pytest tests/unit/test_the_agent_hears_the_phones.py -v -p no:cacheprovider --no-header
"""
import base64
import json
import re
from types import SimpleNamespace

import pytest

from tests.unit.test_reuse_reply_is_evidence import speech  # noqa: F401  (fixture)

PERSON = 'reply_evidence_test'          # the speech fixture's person (key ..._61)
PROMPT_ID = 61
DEMO_GOAL = ('Provide a comprehensive speech therapy companion for young children '
             'with speech delays, helping them practise sounds and words.')


class _Worker:
    """The phonetic tool's worker at its boundary: alive or not, and what it
    answers to the 'phones' op."""

    def __init__(self, alive=True, said='a m a'):
        self.alive, self.said, self.calls = alive, said, []

    def is_alive(self):
        return self.alive

    def call(self, request):
        self.calls.append(request)
        if isinstance(self.said, dict):
            return dict(self.said)
        return {'phones': self.said}


@pytest.fixture
def vp(monkeypatch):
    """The speaker model at its boundary (the sibling file's fake): one second
    of audio per utterance, a fixed voiceprint per voice."""
    from integrations.audio import voiceprint
    import numpy as np
    voices = {'child': [1.0, 0.0, 0.0], 'parent': [0.0, 1.0, 0.0]}
    monkeypatch.setattr(voiceprint, '_extractor', object())
    monkeypatch.setattr(voiceprint, '_current', 'child', raising=False)
    monkeypatch.setattr(voiceprint, '_read_audio',
                        lambda path: np.full(voiceprint.SAMPLE_RATE, 0.1, dtype='float32'))
    monkeypatch.setattr(voiceprint, '_embed_samples',
                        lambda ex, samples: list(voices[voiceprint._current]))
    voiceprint.forget(PERSON)
    yield voiceprint
    voiceprint.forget(PERSON)


@pytest.fixture
def model(monkeypatch):
    """The phonetic model, loaded (a fake worker that answers 'a m a'), and no
    person marked as talking to an agent that works on speech."""
    from integrations.service_tools import phonetic_tool
    worker = _Worker()
    monkeypatch.setattr(phonetic_tool, '_tool', worker)
    monkeypatch.setattr(phonetic_tool, '_wanted', {}, raising=False)
    return worker


def _wanted(person=PERSON):
    """The person's own turn was with an agent that works on speech."""
    from integrations.service_tools import phonetic_tool
    phonetic_tool.want_phones(person, True)


@pytest.fixture
def asks(monkeypatch):
    """Model setup at the orchestrator's door: the asks it gets."""
    import integrations.service_tools.model_orchestrator as mo
    asked = []

    class _Orch:
        def ensure_loaded_async(self, model_type, **kw):
            asked.append((str(model_type), kw.get('caller')))
    monkeypatch.setattr(mo, 'get_orchestrator', lambda: _Orch())
    return asked


@pytest.fixture
def agent(monkeypatch, tmp_path):
    """Writes the agent's record (its prompt file) into a temporary prompts
    folder, the record _reuse_voice_window reads."""
    from hartos import helper
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    monkeypatch.setattr(helper, 'PROMPTS_DIR', str(prompts))

    def write(**fields):
        (prompts / f'{PROMPT_ID}.json').write_text(json.dumps(fields), encoding='utf-8')
    return write


def _hear(vp, who, words):
    vp._current = who
    return vp.hear(PERSON, 'a.wav', words)


class TestAnAgentThatWorksOnSpeechNeedsPhones:
    def test_the_demo_agents_own_goal_text_needs_phones(self):
        """95568353503 stores no goal tags; its goal text is the signal (v3 N1)."""
        from integrations.service_tools.phonetic_tool import needs_phones
        assert needs_phones(None, DEMO_GOAL) is True

    def test_a_stored_goal_tag_needs_phones(self):
        from integrations.service_tools.phonetic_tool import needs_phones
        assert needs_phones(['speech_therapy'], 'Plan my week') is True

    def test_other_agents_need_none(self):
        from integrations.service_tools.phonetic_tool import needs_phones
        assert needs_phones(['marketing'], 'Grow my bakery on Instagram') is False
        assert needs_phones(None, '') is False

    def test_a_stored_value_that_is_not_a_tag_list_reads_as_none(self):
        """The record is user-writable JSON: a bare string is not a tag list."""
        from integrations.service_tools.phonetic_tool import needs_phones
        assert needs_phones('speech therapy', 'Plan my week') is False


class TestThePhonesSitBesideTheVoiceLetter:
    def test_a_loaded_model_writes_the_phones_beside_the_letter(self, vp, model):
        from integrations.service_tools.phonetic_tool import SAMPLE_RATE
        _wanted()
        assert _hear(vp, 'child', 'amma')['voice'] == 'A'
        window = vp.voice_window(PERSON, phones=True)
        assert 'voice A: "amma" [a m a]' in window
        # The worker got the clip as 16 kHz mono PCM16, in memory.
        (request,) = model.calls
        assert request['op'] == 'phones'
        assert len(base64.b64decode(request['pcm16'])) == 2 * SAMPLE_RATE

    def test_the_window_without_phones_reads_as_before(self, vp, model):
        """The open-mic judge and every agent that does not need phones read
        the window without sounds.  (Two voices: a window of one known voice
        and no sounds is not given, see
        test_the_agent_does_not_read_out_the_apps_notes.)"""
        _hear(vp, 'child', 'amma')
        _hear(vp, 'parent', 'say it again')
        window = vp.voice_window(PERSON)
        assert window.startswith(vp.VOICE_WINDOW_HEADER)
        assert window.endswith('voice A: "amma"\nvoice B: "say it again"')
        assert '[' not in window

    def test_hearing_never_starts_the_model(self, vp, model, asks):
        """Only the turn of an agent that needs phones asks for the model; the
        hearing path is the person's alone and never loads it."""
        model.alive = False
        assert _hear(vp, 'child', 'amma')['voice'] == 'A'
        assert model.calls == [] and asks == []
        assert vp.voice_window(PERSON, phones=True) == '', (
            'one known voice with no sounds is the person and their words')
        _hear(vp, 'parent', 'say it again')
        assert 'voice A: "amma"\n' in vp.voice_window(PERSON, phones=True)
        assert model.calls == [] and asks == []

    def test_a_worker_error_leaves_the_letter_without_phones(self, vp, model):
        model.said = {'error': 'phonetic-pasketti crashed', 'transient': True}
        _wanted()
        assert _hear(vp, 'child', 'amma')['voice'] == 'A'
        _hear(vp, 'parent', 'say it again')
        window = vp.voice_window(PERSON, phones=True)
        assert 'voice A: "amma"\n' in window and '[' not in window

    def test_a_clip_longer_than_the_card_allows_is_not_sent(self, model):
        import numpy as np
        from integrations.service_tools import phonetic_tool as pt
        too_long = np.zeros((pt.MAX_SECONDS + 1) * pt.SAMPLE_RATE, dtype='float32')
        _wanted()
        assert pt.phones(PERSON, too_long) == ''
        assert model.calls == []

    def test_phones_are_worked_out_only_for_a_person_whose_agent_needs_them(
            self, speech, vp, model, asks, agent):
        """Review of 5ecf725e8: the phone's transcribe route hears inside the
        request, so phones for every utterance of every person made every
        phone transcription on the node wait for the worker.  The person's
        own latest turn decides: a planner's turn, no phones; a speech
        agent's turn, phones."""
        agent(goal='Plan my week and remind me of meetings', name='Planner')
        run = speech(ask_verdict='pending')
        run.rr._reuse_voice_window(run.key, 'request-9')
        _hear(vp, 'child', 'amma')
        assert model.calls == [], 'phones were worked out for a planner turn'
        agent(goal=DEMO_GOAL, name='Speech Therapy Companion')
        run.rr._reuse_voice_window(run.key, 'request-10')
        _hear(vp, 'child', 'amma')
        assert len(model.calls) == 1
        agent(goal='Plan my week and remind me of meetings', name='Planner')
        run.rr._reuse_voice_window(run.key, 'request-11')
        _hear(vp, 'child', 'amma')
        assert len(model.calls) == 1, 'a later planner turn did not stop the phones'


class TestThePhonesRideOnlyTheirAgentsOwnTurn:
    def test_a_speech_agents_turn_carries_the_phones(self, speech, vp, model, asks, agent):
        agent(goal=DEMO_GOAL, name='Speech Therapy Companion')
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        _hear(vp, 'child', 'amma')
        _hear(vp, 'parent', 'say it again')
        since = len(run.state['steers'])
        run.turn('amma', 'request-2', kind='reply')
        seed = next(s for s in run.state['steers'][since:]
                    if s.startswith(f'{run.rr._REUSE_ACTION_MESSAGE_PREFIX}1:'))
        assert 'voice A: "amma" [a m a]' in seed, seed
        assert 'voice B: "say it again" [a m a]' in seed
        assert asks == [], 'a loaded model was asked for again'

    def test_another_agents_turn_reads_the_window_without_phones(self, speech, vp, model,
                                                                 asks, agent):
        agent(goal='Plan my week and remind me of meetings', name='Planner')
        run = speech(ask_verdict='pending')
        _hear(vp, 'child', 'amma')
        _hear(vp, 'parent', 'say it again')
        window = run.rr._reuse_voice_window(run.key, 'request-9')
        assert window.endswith('voice A: "amma"\nvoice B: "say it again"'), window
        assert asks == []

    def test_a_daemon_turn_reads_nothing_and_asks_nothing(self, speech, vp, model, asks, agent):
        agent(goal=DEMO_GOAL)
        run = speech(ask_verdict='pending')
        _hear(vp, 'child', 'amma')
        assert run.rr._reuse_voice_window(run.key, 'daemon_goal_turn') == ''
        assert asks == []

    def test_no_model_asks_setup_once_and_the_turn_goes_on(self, speech, vp, model, asks,
                                                           agent):
        """No model yet: no phones, model setup asked once by this turn, and
        the window and the turn go on as before (never a gate)."""
        agent(goal=DEMO_GOAL)
        model.alive = False
        run = speech(ask_verdict='pending')
        _hear(vp, 'child', 'amma')
        _hear(vp, 'parent', 'say it again')
        window = run.rr._reuse_voice_window(run.key, 'request-9')
        assert window.endswith('voice A: "amma"\nvoice B: "say it again"'), window
        assert asks == [('phonetic', 'phonetic')]
        since = len(run.state['steers'])
        run.turn('hi, I am ready', 'request-1')
        assert any(vp.VOICE_WINDOW_HEADER in s for s in run.state['steers'][since:])


@pytest.fixture
def pinned(monkeypatch, tmp_path):
    """The pinned file list at test sizes, in a temporary model folder."""
    from integrations.service_tools import phonetic_tool
    files = {'model.safetensors': 64, 'config.json': 8}
    monkeypatch.setattr(phonetic_tool, 'PHONETIC_FILES', files)
    monkeypatch.setattr(phonetic_tool, 'model_dir', lambda: tmp_path)
    return phonetic_tool, files, tmp_path


def _write(folder, files):
    for name, size in files.items():
        (folder / name).write_bytes(b'x' * size)


class TestModelSetupOnNeed:
    def test_the_loader_fetches_the_pinned_files_from_the_one_repo(self, pinned, monkeypatch):
        pt, files, folder = pinned
        from integrations.service_tools.model_storage import model_storage
        fetched = []

        def fetch(tool_name, repo_id, **kw):
            fetched.append((tool_name, repo_id, kw))
            _write(folder, files)
            return folder
        monkeypatch.setattr(model_storage, 'download_hf_model', fetch)
        assert pt.PhoneticLoader().download(None) is True
        assert fetched == [(pt.PHONETIC_MODEL_ID, 'drivendata/pasketti-phonetic',
                            {'revision': pt.PHONETIC_REVISION,
                             'allow_patterns': sorted(files)})]
        assert re.fullmatch(r'[0-9a-f]{40}', pt.PHONETIC_REVISION), 'not a pinned commit'

    def test_a_fetch_bigger_than_its_share_of_the_free_disk_is_not_started(
            self, pinned, monkeypatch):
        pt, files, _folder = pinned
        from core import platform_paths
        from integrations.service_tools.model_storage import model_storage
        need = sum(files.values())
        fetched = []
        monkeypatch.setattr(model_storage, 'download_hf_model',
                            lambda *a, **kw: fetched.append(a))
        monkeypatch.setattr(platform_paths, 'free_bytes',
                            lambda path: int(need / pt.FETCH_FREE_SHARE) - 1)
        assert pt.PhoneticLoader().download(None) is False
        assert pt.PhoneticLoader().load(SimpleNamespace(idle_timeout_s=1), 'cpu') is False
        assert fetched == []

    def test_a_file_not_at_its_pinned_size_is_not_on_disk(self, pinned):
        pt, files, folder = pinned
        _write(folder, files)
        assert pt.PhoneticLoader().is_downloaded(None) is True
        (folder / 'model.safetensors').write_bytes(b'x' * 10)   # a cut-off fetch
        assert pt.PhoneticLoader().is_downloaded(None) is False

    def test_load_starts_the_worker_on_the_orchestrators_device_with_the_session_window(
            self, pinned, monkeypatch):
        pt, files, folder = pinned
        from integrations.audio.voiceprint import IDLE_S
        _write(folder, files)
        seen = []

        class _Tool:
            run_mode = None
            alive = False

            def _get_or_start(self):
                seen.append(('start', self.run_mode))
                self.alive = True

            def set_idle_timeout(self, seconds):
                seen.append(('idle', seconds, self.alive))

            def is_alive(self):
                return self.alive

            def stop(self):
                seen.append(('stop',))
                self.alive = False
        monkeypatch.setattr(pt, '_tool', _Tool())
        entry = SimpleNamespace(id=pt.PHONETIC_MODEL_ID, idle_timeout_s=IDLE_S)
        loader = pt.PhoneticLoader()
        assert loader.load(entry, 'cpu') is True
        # The idle timer is set once the worker runs, so it is armed now.
        assert seen == [('start', 'cpu'), ('idle', float(IDLE_S), True)]
        assert loader.is_loaded(entry) is True
        loader.unload(entry)
        assert seen[-1] == ('stop',) and loader.is_loaded(entry) is False

    def test_a_gpu_start_on_a_cpu_torch_gives_its_booking_back(self, pinned, monkeypatch):
        """Review of 5ecf725e8: the orchestrator picks the GPU by free memory,
        but the installed python-embed has a CPU build of torch, so a 'gpu'
        worker runs on the CPU while its VRAM stays booked.  The worker says
        where it loaded; a CPU answer gives the booking back."""
        pt, files, folder = pinned
        from integrations.service_tools.vram_manager import vram_manager
        _write(folder, files)
        released = []
        monkeypatch.setattr(vram_manager, 'release', lambda name: released.append(name))

        class _Tool:
            run_mode = None
            alive = False

            def __init__(self, device):
                self.device = device

            def _get_or_start(self):
                self.alive = True

            def call(self, request):
                return {'phones': '', 'device': self.device}

            def set_idle_timeout(self, seconds):
                pass

            def is_alive(self):
                return self.alive

        on_cpu = _Tool('cpu')
        monkeypatch.setattr(pt, '_tool', on_cpu)
        assert pt.PhoneticLoader().load(SimpleNamespace(idle_timeout_s=1), 'gpu') is True
        assert released == [pt.PHONETIC_MODEL_ID]
        assert on_cpu.run_mode == 'cpu', 'a restart would book the GPU again'

        released.clear()
        on_gpu = _Tool('cuda')
        monkeypatch.setattr(pt, '_tool', on_gpu)
        assert pt.PhoneticLoader().load(SimpleNamespace(idle_timeout_s=1), 'gpu') is True
        assert released == [] and on_gpu.run_mode == 'gpu'


class _Catalog:
    def list_all(self):
        return []

    def list_by_type(self, model_type):
        return []


class TestHartosRegistersItsLoadersEverywhere:
    def test_one_function_registers_the_speaker_and_the_phonetic_loader(self):
        """Design 1125 H3: Nunba's get_orchestrator calls the same function,
        so the app process has them too."""
        from integrations.audio.voiceprint import SpeakerLoader
        from integrations.service_tools.model_orchestrator import (
            ModelOrchestrator, register_hartos_loaders)
        from integrations.service_tools.phonetic_tool import PhoneticLoader
        orch = ModelOrchestrator(catalog=_Catalog())
        register_hartos_loaders(orch)
        assert isinstance(orch._loaders.get('speaker'), SpeakerLoader)
        assert isinstance(orch._loaders.get('phonetic'), PhoneticLoader)

    def test_hartos_get_orchestrator_registers_both(self, monkeypatch):
        import integrations.service_tools.model_orchestrator as mo
        from integrations.service_tools.phonetic_tool import PhoneticLoader
        monkeypatch.setattr(mo, '_orchestrator_instance', None)
        monkeypatch.setattr(mo.ModelOrchestrator, '_scan_downloaded',
                            lambda self, model_type=None: None)
        orch = mo.get_orchestrator()
        assert isinstance(orch._loaders.get('phonetic'), PhoneticLoader)
        assert 'speaker' in orch._loaders


class TestOneNameEverywhere:
    def test_the_row_the_worker_the_budget_and_the_lifecycle_share_one_name(self, monkeypatch):
        """Measured use is recorded under the worker's tool name, so the
        catalog id, the worker, the budget key and the lifecycle record are
        one name (the mms mismatch is what this avoids)."""
        from integrations.service_tools import model_lifecycle
        from integrations.service_tools.model_catalog import ModelCatalog
        from integrations.service_tools.model_orchestrator import ModelOrchestrator
        from integrations.service_tools.phonetic_tool import PHONETIC_MODEL_ID, _tool
        from integrations.service_tools.vram_manager import VRAM_BUDGETS
        cat = ModelCatalog.__new__(ModelCatalog)
        cat._entries = {}
        cat.already_registered = lambda mid: mid in cat._entries
        cat.register = lambda entry, persist=True: cat._entries.__setitem__(entry.id, entry)
        cat._populate_phonetic_models()
        row = cat._entries[PHONETIC_MODEL_ID]
        orch = ModelOrchestrator(catalog=_Catalog())
        assert _tool.tool_name == _tool.vram_budget == PHONETIC_MODEL_ID
        assert orch._vram_key(row) == PHONETIC_MODEL_ID
        assert PHONETIC_MODEL_ID in VRAM_BUDGETS
        assert PHONETIC_MODEL_ID not in ModelOrchestrator._CATALOG_TO_VRAM_KEY
        mlm = model_lifecycle.ModelLifecycleManager()
        monkeypatch.setattr(model_lifecycle, 'get_model_lifecycle_manager', lambda: mlm)
        orch._register_lifecycle(row)
        assert PHONETIC_MODEL_ID in mlm._models


class TestTheChecklistListsEveryType:
    def test_the_onboarding_checklist_lists_every_model_type(self, monkeypatch):
        """Design 1125 C1: the checklist reads the catalog's own type list; its
        hand-kept eight missed speaker and embodied, and would miss phonetic."""
        from flask import Flask
        from integrations.service_tools import model_catalog, model_onboarding
        monkeypatch.setattr(model_catalog, 'get_catalog', lambda: _Catalog())
        app = Flask(__name__)
        app.register_blueprint(model_onboarding._create_blueprint())
        got = app.test_client().get('/api/models/checklist').get_json()
        types = [c['model_type'] for c in got['checklist']]
        assert types == list(model_catalog.MODEL_TYPES)
        assert {'phonetic', 'speaker', 'embodied'} <= set(types)
