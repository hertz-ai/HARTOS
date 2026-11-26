"""The live mic never waits on a model download, and never hears another
language through an English-only model (hartos-17 on 6811f6467; owner rule
via the coordinator, 2026-11-26).

6811f6467 made the STT pick multilingual.  On the sherpa path (faster-whisper
cannot run) with only Moonshine Tiny on disk, the cached-model search then
skipped Moonshine and handed back the uncached Whisper pick "(will trigger a
download)": the download ran inside the request, the case #677 says crashes
the worker, so the mic was deaf until it finished.  The CPU fallback still
answered 'moonshine-tiny'.  Serving Moonshine to a Tamil speaker is no answer
either: it writes the Tamil as English words, and Whisper on unfit audio made
the night's phantom open-mic finals.

So: Moonshine serves only an English request, at once, while the
multilingual pick downloads in the background.  Any other language, or none
(the page's own mic names none), gets no transcript, a plain-words notice
that the voice model is still downloading, and the background download.
Where no multilingual model fits the computer at all, such a request gets
the notice that this computer cannot run a voice model for that language
yet, and nothing is fetched.  Nothing is ever fetched inside the request.

    venv/Scripts/python.exe -m pytest tests/unit/test_stt_never_downloads_inside_the_request.py -q --noconftest
"""
import array
import asyncio
import io
import json
import sys
import threading
import types

import pytest

import integrations.service_tools.whisper_tool as wt

#: The catalog's ranking on the box: the multilingual sherpa Whisper models,
#: then Moonshine.  Each entry's capabilities come from the real catalog rows
#: (populate_stt_catalog), so "Moonshine is English only" is the catalog's
#: own word, applied the way select_best applies require_capability.
RANKING = ['stt-sherpa-whisper-small', 'stt-sherpa-whisper-base',
           'stt-sherpa-whisper-tiny', 'stt-sherpa-moonshine-tiny',
           'stt-sherpa-moonshine-base']

NOTICE_WORDS = 'still downloading'
NO_MODEL_WORDS = 'cannot run a voice model for that language'


def _orchestrator(catalog, fits=lambda entry: True):
    """The catalog's ranking for the box (RANKING), filtered by what fits this
    computer and by require_capability, as select_best filters them."""
    class _Orchestrator:
        def select_best(self, model_type, exclude=None, require_capability=None, **_kw):
            for cid in RANKING:
                entry = catalog.get(cid)
                if cid in (exclude or ()) or not fits(entry):
                    continue
                if require_capability and any(
                        entry.capabilities.get(k) != v
                        for k, v in require_capability.items()):
                    continue
                return entry
            return None
    return _Orchestrator()


def _put_on_disk(stt_dir, key):
    """A complete sherpa model folder: every file the recognizer loads."""
    cfg = wt._SHERPA_MODELS[key]
    folder = stt_dir / cfg['dir']
    folder.mkdir(parents=True)
    for name in cfg['files'].values():
        (folder / name).write_bytes(b'x')


def _fetches_done():
    """Wait for every background fetch thread to finish."""
    for t in threading.enumerate():
        if t.name.startswith('stt-download-'):
            t.join(5)


@pytest.fixture
def box(monkeypatch, tmp_path):
    """The sherpa path on a box whose only model on disk is Moonshine Tiny."""
    from integrations.service_tools.model_catalog import ModelCatalog
    catalog = ModelCatalog(catalog_path=str(tmp_path / 'catalog.json'))
    wt.populate_stt_catalog(catalog)
    orchestrator = _orchestrator(catalog)
    monkeypatch.setattr(
        'integrations.service_tools.model_orchestrator.get_orchestrator',
        lambda: orchestrator)

    stt_dir = tmp_path / 'stt'
    monkeypatch.setattr(wt, '_get_stt_dir', lambda: stt_dir)
    _put_on_disk(stt_dir, 'moonshine-tiny')

    # faster-whisper cannot run here, sherpa-onnx can; openai-whisper is not
    # installed (the shipped build).
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', types.ModuleType('sherpa_onnx'))
    monkeypatch.setattr(wt, '_legacy_transcribe', lambda *_a, **_k: None)

    # The decode is the engine boundary.  A model not on disk is fetched BY
    # the decode (_get_sherpa_recognizer -> _download_model), inside the
    # request, so each decode records whether its model was on disk.
    decoded = []

    def decode(audio_path, model_name):
        decoded.append((model_name, wt._sherpa_model_cached(model_name)))
        # What Moonshine writes for any speech: English words.
        return json.dumps({'text': 'the middle river', 'language': 'en'})
    monkeypatch.setattr(wt, '_sherpa_transcribe', decode)

    # The network fetch: record the model and the thread it ran on.
    fetches = []
    monkeypatch.setattr(wt, '_download_model', lambda name: fetches.append(
        (name, threading.current_thread().name)))
    monkeypatch.setattr(wt, '_background_downloads', set())
    monkeypatch.setattr(wt, '_background_failures', {})
    monkeypatch.setattr(wt, '_stt_last_notice', None, raising=False)
    return types.SimpleNamespace(decoded=decoded, fetches=fetches, catalog=catalog)


@pytest.fixture
def only_english_fits(box, monkeypatch):
    """The same box on a CPU where no multilingual model fits, only Moonshine:
    the case hartos-17 probed, where the pick still answered 'moonshine-tiny'
    (the CPU fallback, whisper_tool ~:1409)."""
    orchestrator = _orchestrator(
        box.catalog, fits=lambda entry: not entry.capabilities.get('multilingual'))
    monkeypatch.setattr(
        'integrations.service_tools.model_orchestrator.get_orchestrator',
        lambda: orchestrator)
    from integrations.service_tools.vram_manager import vram_manager
    monkeypatch.setattr(vram_manager, 'detect_gpu',
                        lambda *_a, **_k: {'cuda_available': False})
    return box


@pytest.fixture
def no_catalog(box, monkeypatch):
    """The same box when the catalog cannot answer (select_whisper_model's
    own fallback), with no CUDA."""
    class _Down:
        def select_best(self, *_a, **_k):
            raise RuntimeError('catalog unavailable')
    monkeypatch.setattr(
        'integrations.service_tools.model_orchestrator.get_orchestrator',
        lambda: _Down())
    from integrations.service_tools.vram_manager import vram_manager
    monkeypatch.setattr(vram_manager, 'detect_gpu',
                        lambda *_a, **_k: {'cuda_available': False})
    return box


def _transcribe(language):
    out = json.loads(wt._run_engine_chain('utterance.wav', language=language))
    _fetches_done()
    return out


def test_a_tamil_speaker_gets_no_english_words_and_is_told_the_model_is_downloading(box):
    out = _transcribe('ta')
    assert box.decoded == [], 'decoded with %r' % (box.decoded,)
    assert out.get('text', '') == ''
    assert NOTICE_WORDS in out.get('notice', '')
    # The multilingual pick downloads, off the request's thread.
    assert box.fetches == [('whisper-small', 'stt-download-whisper-small')]


def test_the_page_mic_names_no_language_and_is_not_taken_for_english(box):
    """The page's own mic sends no language (Whisper detects it), so nothing
    says the speaker is English: Moonshine does not hear them either."""
    out = _transcribe(None)
    assert box.decoded == []
    assert out.get('text', '') == ''
    assert NOTICE_WORDS in out.get('notice', '')
    assert box.fetches == [('whisper-small', 'stt-download-whisper-small')]


def test_an_english_speaker_is_heard_by_moonshine_at_once(box):
    out = _transcribe('en-IN')
    assert box.decoded == [('moonshine-tiny', True)]
    assert out['text'] == 'the middle river'
    assert 'notice' not in out
    # The better pick still comes, in the background.
    assert box.fetches == [('whisper-small', 'stt-download-whisper-small')]


def test_without_the_catalog_the_page_mic_is_not_given_moonshine(no_catalog):
    """hartos-17: the CPU fallback still answered 'moonshine-tiny'."""
    out = _transcribe(None)
    assert no_catalog.decoded == []
    assert NOTICE_WORDS in out.get('notice', '')
    assert no_catalog.fetches == [('whisper-tiny', 'stt-download-whisper-tiny')]


def test_without_the_catalog_a_tamil_speaker_waits_for_whisper_off_the_request(no_catalog):
    out = _transcribe('ta')
    assert no_catalog.decoded == []
    assert NOTICE_WORDS in out.get('notice', '')
    assert no_catalog.fetches == [('whisper-tiny', 'stt-download-whisper-tiny')]


def test_without_the_catalog_an_english_speaker_is_heard_while_whisper_downloads(no_catalog):
    out = _transcribe('en')
    assert no_catalog.decoded == [('moonshine-tiny', True)]
    assert out['text'] == 'the middle river'
    assert no_catalog.fetches == [('whisper-tiny', 'stt-download-whisper-tiny')]


def test_where_no_multilingual_model_fits_a_tamil_speaker_is_told_so(only_english_fits):
    """No English words, and no fetch of a model this computer cannot run."""
    out = _transcribe('ta')
    assert only_english_fits.decoded == [], 'decoded with %r' % (only_english_fits.decoded,)
    assert out.get('text', '') == ''
    assert NO_MODEL_WORDS in out.get('notice', '')
    assert only_english_fits.fetches == []


def test_where_no_multilingual_model_fits_the_page_mic_is_not_given_moonshine(only_english_fits):
    out = _transcribe(None)
    assert only_english_fits.decoded == []
    assert NO_MODEL_WORDS in out.get('notice', '')
    assert only_english_fits.fetches == []


def test_where_no_multilingual_model_fits_an_english_speaker_is_still_heard(only_english_fits):
    out = _transcribe('en')
    assert only_english_fits.decoded == [('moonshine-tiny', True)]
    assert out['text'] == 'the middle river'
    assert only_english_fits.fetches == []


def test_the_page_is_told_in_plain_words_on_the_mic_socket(box, monkeypatch):
    """The live mic end to end: the worker's own dispatch (run here, not in
    its subprocess) answers the stream's final, and the page gets the notice
    as its own message, never as a transcript."""
    monkeypatch.setattr(wt, '_stt_call', lambda req: wt._synthesize(None, req))
    sent = []

    class _Socket:
        async def send(self, message):
            sent.append(json.loads(message))

    # Two seconds of loud speech-level audio from the page's own mic.
    pcm = array.array('h', [8000, -8000] * wt.STREAM_SAMPLE_RATE).tobytes()
    heard = asyncio.run(wt._emit_final(_Socket(), io.BytesIO(pcm), None, None, None))
    _fetches_done()

    assert heard is False
    assert len(sent) == 1 and set(sent[0]) == {'notice'}, sent
    assert NOTICE_WORDS in sent[0]['notice']
    assert box.decoded == []
    assert box.fetches == [('whisper-small', 'stt-download-whisper-small')]
