"""V1: the agent hears which voice said what on the person's device.

Owner 2026-11-19 (ReLiva, Fri 27): "the agent should be able to differentiate a
child's voice and pay attention and hear properly in a multi user real world
envrionmenmt"; "HARTOS can use models as tools, don't bundle is the only rule";
"nothing shd be bespoke all only via hartos and nunba autosetup"; consent:
"Session covers it".

The contract:
  * the speaker model is a catalog row (model_type 'speaker') set up on need
    through the orchestrator's 'speaker' loader, which HARTOS registers; the
    loader fetches only from an allowlisted hub org;
  * every utterance the person's device sends is matched against the voices
    heard in the session: same voice -> same letter, a new voice -> a new letter;
  * the voice window (who said what, last 2 minutes) rides into the person's
    own turn seed beside the camera window, never a daemon turn's;
  * memory only; one place (ResonanceIdentifier.hear_voice); a hint, never a
    gate; no model yet -> no window and model setup is asked, the turn goes on.

Measured 19 Nov 2026 23:30 IST on this desktop with the real model fetched by
model setup (28.28 MB, 7.1 s) and two Windows voices saying two lines each:
A, B, A (0.85), B (0.83); 60-71 ms per utterance on CPU.

    python -m pytest tests/unit/test_the_agent_hears_who_spoke.py -q
"""
import pytest

from tests.unit.test_reuse_reply_is_evidence import (  # noqa: F401  (fixture)
    ASK_LINE, speech)

PERSON = 'reply_evidence_test'


class _FakeExtractor:
    """Stands in for sherpa_onnx's extractor at the model boundary."""


@pytest.fixture
def vp(monkeypatch):
    from integrations.audio import voiceprint
    import numpy as np
    voices = {'child': [1.0, 0.0, 0.0], 'parent': [0.0, 1.0, 0.0],
              'child2': [0.97, 0.05, 0.0]}
    monkeypatch.setattr(voiceprint, '_extractor', _FakeExtractor())
    monkeypatch.setattr(voiceprint, '_read_audio',
                        lambda path: np.full(voiceprint.SAMPLE_RATE, 0.1, dtype='float32')
                        if 'short' not in str(path) else np.zeros(10, dtype='float32'))
    monkeypatch.setattr(voiceprint, '_embed_samples',
                        lambda ex, samples: list(voices[voiceprint._current]))
    voiceprint.forget(PERSON)
    yield voiceprint
    voiceprint.forget(PERSON)


def _hear(vp, who, words, path='a.wav'):
    vp._current = who
    return vp.hear(PERSON, path, words)


class TestVoicesAreToldApart:
    def test_same_voice_same_letter_new_voice_new_letter(self, vp):
        assert _hear(vp, 'child', 'my name is Arun')['voice'] == 'A'
        assert _hear(vp, 'parent', 'wait, one minute')['voice'] == 'B'
        again = _hear(vp, 'child2', 'ball')
        assert again['voice'] == 'A' and again['new'] is False and again['score'] >= vp.SAME_VOICE

    def test_the_window_says_which_voice_said_what(self, vp):
        _hear(vp, 'child', 'my name is Arun')
        _hear(vp, 'parent', 'wait, one minute')
        window = vp.voice_window(PERSON)
        assert window.startswith(vp.VOICE_WINDOW_HEADER)
        assert 'voice A: "my name is Arun"' in window
        assert 'voice B: "wait, one minute"' in window

    def test_a_doubt_is_another_voice(self, vp, monkeypatch):
        monkeypatch.setattr(vp, 'SAME_VOICE', 0.999)
        _hear(vp, 'child', 'my name is Arun')
        assert _hear(vp, 'child2', 'ball')['voice'] == 'B'

    def test_short_audio_is_not_heard(self, vp):
        assert _hear(vp, 'child', 'a', path='short.wav') is None
        assert vp.voice_window(PERSON) == ''

    def test_old_lines_leave_the_window(self, vp, monkeypatch):
        _hear(vp, 'child', 'my name is Arun')
        real = vp.time.time
        monkeypatch.setattr(vp.time, 'time', lambda: real() + vp.WINDOW_S + 5)
        assert vp.voice_window(PERSON) == ''

    def test_another_persons_device_is_separate(self, vp):
        _hear(vp, 'child', 'my name is Arun')
        assert vp.voice_window('someone_else') == ''

    def test_memory_is_bounded_across_devices(self, vp, monkeypatch):
        """hartos-52's review, point 4: a long-running node never grows."""
        monkeypatch.setattr(vp, 'MAX_PERSONS', 3)
        vp._current = 'child'
        for i in range(6):
            vp.hear(f'device-{i}', 'a.wav', 'hi')
        assert len(vp._last_seen) == 3
        assert 'device-0' not in vp._voices and 'device-5' in vp._voices
        for i in range(6):
            vp.forget(f'device-{i}')


class TestModelSetupOnNeed:
    def test_no_model_asks_setup_and_hears_nothing(self, monkeypatch):
        from integrations.audio import voiceprint
        import integrations.service_tools.model_orchestrator as mo
        asked = []

        class _Orch:
            def ensure_loaded_async(self, model_type, **kw):
                asked.append((model_type, kw.get('caller')))
        monkeypatch.setattr(voiceprint, '_extractor', None)
        monkeypatch.setattr(mo, 'get_orchestrator', lambda: _Orch())
        assert voiceprint.hear(PERSON, 'a.wav', 'hi') is None
        assert asked == [('speaker', 'voiceprint')]

    def test_the_catalog_row_is_a_speaker_model_on_the_hub(self):
        from integrations.service_tools.model_catalog import ModelCatalog, ModelType
        from integrations.audio import voiceprint
        cat = ModelCatalog.__new__(ModelCatalog)
        cat._entries = {}
        cat._populators = []
        cat.already_registered = lambda mid: mid in cat._entries
        cat.register = lambda entry, persist=True: cat._entries.__setitem__(entry.id, entry)
        assert cat._populate_speaker_models() == 1
        row = cat._entries[voiceprint.SPEAKER_MODEL_ID]
        assert row.model_type == ModelType.SPEAKER == 'speaker'
        assert row.source == 'huggingface' and row.repo_id == voiceprint.SPEAKER_REPO
        assert row.files == {'model': voiceprint.SPEAKER_FILE}
        assert row.vram_gb == 0 and row.supports_cpu

    def test_hartos_registers_the_speaker_loader(self, monkeypatch):
        import integrations.service_tools.model_orchestrator as mo
        from integrations.audio.voiceprint import SpeakerLoader
        monkeypatch.setattr(mo, '_orchestrator_instance', None)
        monkeypatch.setattr(mo.ModelOrchestrator, '_scan_downloaded', lambda self: None)
        orch = mo.get_orchestrator()
        assert isinstance(orch._loaders.get('speaker'), SpeakerLoader)
        monkeypatch.setattr(mo, '_orchestrator_instance', None)

    def _fetch_writes(self, monkeypatch, tmp_path, content):
        from integrations.audio import voiceprint
        import huggingface_hub
        calls = []
        monkeypatch.setattr(voiceprint, 'model_dir', lambda: tmp_path)

        def fake_download(**kw):
            calls.append(kw)
            (tmp_path / kw['filename']).write_bytes(content)
        monkeypatch.setattr(huggingface_hub, 'hf_hub_download', fake_download)
        return voiceprint, calls

    def test_only_the_pinned_file_is_kept(self, monkeypatch, tmp_path):
        """hartos-52's review: the mirror is one person's account, so trust is
        this file's content, never the whole account on every install."""
        import hashlib
        voiceprint, calls = self._fetch_writes(monkeypatch, tmp_path, b'not the model')
        assert voiceprint.SpeakerLoader().download(None) is False
        assert calls and calls[0]['repo_id'] == voiceprint.SPEAKER_REPO
        assert calls[0]['filename'] == voiceprint.SPEAKER_FILE
        assert not voiceprint.model_path().exists(), 'an unpinned file was left on disk'
        good = b'the pinned model'
        monkeypatch.setattr(voiceprint, 'SPEAKER_SHA256', hashlib.sha256(good).hexdigest())
        voiceprint, _ = self._fetch_writes(monkeypatch, tmp_path, good)
        assert voiceprint.SpeakerLoader().download(None) is True

    def test_a_swapped_file_on_disk_is_not_loaded(self, monkeypatch, tmp_path):
        from integrations.audio import voiceprint
        monkeypatch.setattr(voiceprint, 'model_dir', lambda: tmp_path)
        voiceprint.model_path().write_bytes(b'swapped after download')
        built = []
        monkeypatch.setattr(voiceprint, '_build_extractor', lambda p: built.append(p))
        assert voiceprint.SpeakerLoader().load(None) is False
        assert built == [] and not voiceprint.model_path().exists()
        voiceprint.model_path().write_bytes(b'swapped after download')
        assert voiceprint.SpeakerLoader().download(None) is False, (
            'download() said ready for a swapped file already on disk')
        assert not voiceprint.model_path().exists()

    def test_the_mirror_is_not_trusted_as_an_org(self):
        from core.hub_allowlist import DEFAULT_TRUSTED_ORGS
        from integrations.audio import voiceprint
        assert voiceprint.SPEAKER_REPO.split('/')[0] not in DEFAULT_TRUSTED_ORGS

    def test_the_row_records_the_weights_licence(self):
        from integrations.service_tools.model_catalog import ModelCatalog
        from integrations.audio import voiceprint
        cat = ModelCatalog.__new__(ModelCatalog)
        cat._entries = {}
        cat.already_registered = lambda mid: mid in cat._entries
        cat.register = lambda entry, persist=True: cat._entries.__setitem__(entry.id, entry)
        cat._populate_speaker_models()
        assert 'license:apache-2.0' in cat._entries[voiceprint.SPEAKER_MODEL_ID].tags


class TestOnePlace:
    def test_resonance_identifier_hears_through_the_voiceprint_tool(self, vp):
        from core.resonance_identifier import ResonanceIdentifier
        vp._current = 'child'
        got = ResonanceIdentifier().hear_voice(PERSON, 'a.wav', 'my name is Arun')
        assert got['voice'] == 'A'
        _hear(vp, 'parent', 'wait, one minute')
        assert 'voice A: "my name is Arun"' in vp.voice_window(PERSON)


class TestTheTranscribeRouteHearsTheSignedInPerson:
    """/api/voice/transcribe, what the phone (DesktopTranscriber) calls."""

    @pytest.fixture
    def client(self):
        from hart_intelligence_entry import app
        if not any(r.rule == '/chat' for r in app.url_map.iter_rules()):
            pytest.skip("Flask app has no routes (partial import)")
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def _post(self, client, headers):
        import io
        return client.post('/api/voice/transcribe', data={
            'audio': (io.BytesIO(b'RIFF....WAVE'), 'call.wav')},
            content_type='multipart/form-data', headers=headers)

    def test_the_speaker_rides_the_reply_and_the_session_is_closed(
            self, client, monkeypatch):
        import json
        from types import SimpleNamespace
        from integrations.service_tools import whisper_tool
        from integrations.social import auth
        from core.resonance_identifier import ResonanceIdentifier
        closed, heard = [], []

        class _Db:
            def close(self):
                closed.append(True)
        monkeypatch.setattr(whisper_tool, 'whisper_transcribe',
                            lambda path, lang: json.dumps({'text': 'ball'}))
        monkeypatch.setattr(auth, '_get_user_from_token',
                            lambda token: (SimpleNamespace(id=42), _Db()))
        monkeypatch.setattr(ResonanceIdentifier, 'hear_voice',
                            lambda self, uid, path, words: heard.append((uid, words))
                            or {'voice': 'A', 'score': 0.9, 'new': False})
        resp = self._post(client, {'Authorization': 'Bearer t'})
        assert resp.status_code == 200
        assert resp.get_json()['speaker']['voice'] == 'A'
        assert heard == [('42', 'ball')]
        assert closed == [True], 'the token lookup session was left open'

    def test_a_token_that_names_nobody_hears_nothing(self, client, monkeypatch):
        """The network gate admits the device (a request with no credential
        at all is refused before the route); a token that names no person
        hears nothing, and the lookup's session is still closed."""
        import json
        from integrations.service_tools import whisper_tool
        from integrations.social import auth
        from core.resonance_identifier import ResonanceIdentifier
        closed, heard = [], []

        class _Db:
            def close(self):
                closed.append(True)
        monkeypatch.setattr(whisper_tool, 'whisper_transcribe',
                            lambda path, lang: json.dumps({'text': 'ball'}))
        monkeypatch.setattr(auth, '_get_user_from_token',
                            lambda token: (None, _Db()))
        monkeypatch.setattr(ResonanceIdentifier, 'hear_voice',
                            lambda self, *a: heard.append(a))
        resp = self._post(client, {'Authorization': 'Bearer phone-device-token'})
        assert resp.status_code == 200 and 'speaker' not in resp.get_json()
        assert heard == [] and closed == [True]


class TestTheWindowRidesThePersonsTurn:
    def test_the_seed_carries_the_voice_window(self, speech, vp):
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        _hear(vp, 'child', 'my name is Arun')
        _hear(vp, 'parent', 'wait, one minute')
        since = len(run.state['steers'])
        run.turn('ba', 'request-2', kind='reply')
        seed = next(s for s in run.state['steers'][since:]
                    if s.startswith(f'{run.rr._REUSE_ACTION_MESSAGE_PREFIX}1:'))
        assert vp.VOICE_WINDOW_HEADER in seed, seed
        assert 'voice B: "wait, one minute"' in seed

    def test_a_daemon_turn_reads_no_voices(self, speech, vp):
        run = speech(ask_verdict='pending')
        _hear(vp, 'child', 'my name is Arun')
        _hear(vp, 'parent', 'wait, one minute')
        assert run.rr._reuse_voice_window(run.key, 'daemon_goal_turn') == ''
        assert vp.VOICE_WINDOW_HEADER in run.rr._reuse_voice_window(run.key, 'request-9')

    def test_no_voices_leaves_the_seed_as_before(self, speech, vp):
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        assert not any(vp.VOICE_WINDOW_HEADER in s for s in run.state['steers'])
