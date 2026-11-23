"""Speaker voiceprints: which voice said each thing the person's device heard.

Owner 2026-11-19: "the agent should be able to differentiate a child's voice and
pay attention and hear properly in a multi user real world environment", and
"HARTOS can use models as tools, don't bundle is the only rule".  So this is a
model used as a tool:

  * the weights are a model-catalog row (SPEAKER_MODEL_ID, model_type 'speaker')
    that model setup fetches on need through the orchestrator's 'speaker'
    loader (SpeakerLoader below); nothing is bundled and nothing is downloaded
    by hand (owner: "nothing shd be bespoke all only via hartos and nunba
    autosetup");
  * ONE place for speaker identity: core.resonance_identifier calls this, and
    the transcribe route calls ResonanceIdentifier (hartos-52's review: no
    second speaker pipeline);
  * voices are told apart, not named.  Each utterance's voiceprint is matched
    against the voices this person's device heard in the session ("voice A",
    "voice B"); the window that rides into the agent's turn says which voice
    said what, and the agent works out who voice A is from the conversation
    (the user's ruling of 19 Nov: the agent understands the context);
  * memory only.  Voiceprints and the window live in this process; nothing but
    the weights is written to disk (owner: session consent covers it, child
    audio retention N days still pending);
  * a hint to the model, never a gate: no consent, permission, payment or flow
    move may read a voice letter (hartos-52's review, point 6).
"""
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SPEAKER_MODEL_ID = 'speaker-campplus-zh-en'
SPEAKER_REPO = 'csukuangfj/speaker-embedding-models'
SPEAKER_FILE = '3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx'
#: The one file trusted, by content: the mirror is one person's account, so the
#: org is NOT put on the hub allowlist (that would trust every repo it ever
#: publishes); this exact file is (hartos-52's review).  sha256 of the copy
#: model setup fetched 19 Nov 2026.  Weights licence: Apache License 2.0 on the
#: weights' own card (ModelScope iic/speech_campplus_sv_zh_en_16k-common_advanced).
SPEAKER_SHA256 = '53cab20ba8f2eb2f6e2d89ecc18e88ef3222169b61b4419cbc13666744a4574b'
SPEAKER_LICENSE = 'apache-2.0'
SAMPLE_RATE = 16000
#: Cosine similarity at or above which two utterances are the same voice.  Not
#: measured on children yet; below it the voice is a new letter, so a doubt
#: reads as "another voice" (hartos-52's review, point c).
SAME_VOICE = 0.6
#: The window the turn reads: who spoke in the last WINDOW_S seconds.
WINDOW_S = 120
MAX_VOICES = 8
MAX_HEARD = 20
#: People remembered at once (the key is the signed-in person, not the device);
#: the one heard longest ago is dropped first, so a long-running node never
#: grows without bound (hartos-52's review, point 4).
MAX_PERSONS = 64
#: A person silent this long starts a fresh set of voices.
IDLE_S = 1800
#: Shorter audio than this carries no usable voiceprint.
MIN_SECONDS = 0.5

_lock = threading.Lock()
_extractor = None
_voices: Dict[str, List[dict]] = {}
_heard: Dict[str, deque] = {}
_last_seen: Dict[str, float] = {}


def model_dir() -> Path:
    return Path.home() / '.hevolve' / 'models' / 'speaker'


def model_path() -> Path:
    return model_dir() / SPEAKER_FILE


def _build_extractor(path: Path):
    import sherpa_onnx
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(path), num_threads=1, provider='cpu')
    if not config.validate():
        raise RuntimeError(f'speaker model config is not valid: {path}')
    return sherpa_onnx.SpeakerEmbeddingExtractor(config)


def _embed_samples(extractor, samples) -> Optional[List[float]]:
    stream = extractor.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    stream.input_finished()
    if not extractor.is_ready(stream):
        return None
    return list(extractor.compute(stream))


def _read_audio(path):
    """16 kHz mono float32 samples of the file, or None."""
    import numpy as np
    import soundfile as sf
    data, rate = sf.read(str(path), dtype='float32', always_2d=True)
    samples = data.mean(axis=1)
    if rate != SAMPLE_RATE and len(samples):
        n = int(round(len(samples) * SAMPLE_RATE / rate))
        samples = np.interp(np.linspace(0, len(samples) - 1, n),
                            np.arange(len(samples)), samples).astype('float32')
    return samples


try:
    from integrations.service_tools.model_orchestrator import ModelLoader as _LoaderBase
except Exception:        # the orchestrator is optional in a bare test import
    _LoaderBase = object


class SpeakerLoader(_LoaderBase):
    """The model-setup loader for model_type 'speaker' (the orchestrator calls
    download / load / validate; see ModelOrchestrator.register_loader)."""

    def is_downloaded(self, entry) -> bool:
        return model_path().is_file() and model_path().stat().st_size > 0

    def download(self, entry) -> bool:
        """Fetch the pinned file.  Trust is the file's content (SPEAKER_SHA256);
        a file whose hash is not the pin, fetched or already on disk, is
        deleted and refused."""
        if self.is_downloaded(entry):
            return self._verified()
        try:
            from huggingface_hub import hf_hub_download
            model_dir().mkdir(parents=True, exist_ok=True)
            hf_hub_download(repo_id=SPEAKER_REPO, filename=SPEAKER_FILE,
                            local_dir=str(model_dir()))
        except Exception:
            logger.warning('voiceprint: fetching %s from %s failed', SPEAKER_FILE,
                           SPEAKER_REPO, exc_info=True)
            return False
        return self._verified()

    def _verified(self) -> bool:
        """True when the file on disk is the pinned one; else it is removed."""
        if not self.is_downloaded(None):
            return False
        import hashlib
        h = hashlib.sha256()
        with open(model_path(), 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
        if h.hexdigest() == SPEAKER_SHA256:
            return True
        logger.warning('voiceprint: %s is not the pinned speaker model (sha256 %s); '
                       'removed and not loaded', model_path(), h.hexdigest())
        try:
            model_path().unlink()
        except OSError:
            logger.warning('voiceprint: the unpinned file could not be removed', exc_info=True)
        return False

    def load(self, entry, run_mode: str = 'cpu') -> bool:
        global _extractor
        if not self.is_downloaded(entry) and not self.download(entry):
            return False
        if not self._verified():
            return False
        try:
            extractor = _build_extractor(model_path())
        except Exception:
            logger.warning('voiceprint: the speaker model did not load', exc_info=True)
            return False
        with _lock:
            _extractor = extractor
        return True

    def unload(self, entry) -> None:
        global _extractor
        with _lock:
            _extractor = None

    def is_loaded(self, entry) -> bool:
        return _extractor is not None

    def validate(self, entry) -> tuple:
        """A fixed one-second tone must give a voiceprint."""
        import numpy as np
        if _extractor is None:
            return (False, 'speaker model not loaded')
        t = np.arange(SAMPLE_RATE, dtype='float32') / SAMPLE_RATE
        emb = _embed_samples(_extractor, (0.1 * np.sin(2 * np.pi * 220 * t)).astype('float32'))
        return (bool(emb), f'voiceprint of {len(emb or [])} values')


def ready() -> bool:
    """True when the speaker model is loaded.  When it is not, model setup is
    asked for it (on need, in the background) and this answers False, so the
    turn goes on with no voice window, as before."""
    if _extractor is not None:
        return True
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        get_orchestrator().ensure_loaded_async('speaker', caller='voiceprint')
    except Exception:
        logger.warning('voiceprint: model setup could not be asked for the speaker model',
                       exc_info=True)
    return False


def _cosine(a, b) -> float:
    import numpy as np
    a, b = np.asarray(a, dtype='float32'), np.asarray(b, dtype='float32')
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / d) if d else 0.0


def hear(person: str, audio_path, words: str = '') -> Optional[dict]:
    """Which voice said ``words`` in ``audio_path`` on ``person``'s device:
    {'voice': 'A', 'score': 0.83, 'new': False}, or None (no model yet, audio
    too short or unreadable, no person).  Memory only."""
    if not person or not ready():
        return None
    try:
        samples = _read_audio(audio_path)
    except Exception:
        logger.warning('voiceprint: the audio could not be read', exc_info=True)
        return None
    if samples is None or len(samples) < MIN_SECONDS * SAMPLE_RATE:
        return None
    try:
        emb = _embed_samples(_extractor, samples)
    except Exception:
        logger.warning('voiceprint: no voiceprint for this audio', exc_info=True)
        return None
    if not emb:
        return None
    now = time.time()
    with _lock:
        if now - _last_seen.get(person, now) > IDLE_S:
            _voices.pop(person, None)
            _heard.pop(person, None)
        _last_seen[person] = now
        while len(_last_seen) > MAX_PERSONS:
            oldest = min(_last_seen, key=_last_seen.get)
            for store in (_voices, _heard, _last_seen):
                store.pop(oldest, None)
        voices = _voices.setdefault(person, [])
        best, best_score = None, -1.0
        for v in voices:
            s = _cosine(emb, v['emb'])
            if s > best_score:
                best, best_score = v, s
        new = best is None or best_score < SAME_VOICE
        if new:
            # A ninth voice drops the one heard longest ago and frees its
            # letter, so the 2-minute window can rarely show two voices under
            # one letter.
            if len(voices) >= MAX_VOICES:
                voices.sort(key=lambda v: v['last'])
                voices.pop(0)
            used = {v['letter'] for v in voices}
            letter = next(c for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if c not in used)
            best = {'letter': letter, 'emb': emb, 'n': 1, 'last': now}
            voices.append(best)
        else:
            n = best['n']
            best['emb'] = [(x * n + y) / (n + 1) for x, y in zip(best['emb'], emb)]
            best['n'] = n + 1
            best['last'] = now
        heard = _heard.setdefault(person, deque(maxlen=MAX_HEARD))
        heard.append((now, best['letter'], round(best_score if not new else 1.0, 2),
                      str(words or '')[:200]))
    return {'voice': best['letter'], 'score': round(best_score, 2) if not new else None,
            'new': new}


VOICE_WINDOW_HEADER = ("Voices heard on this person's account in the last 2 minutes (the "
                       "same letter is the same voice; work out who each voice is "
                       "from the conversation):")


def voice_window(person: str) -> str:
    """The person's voice window for their turn, or '' (nothing heard)."""
    now = time.time()
    with _lock:
        rows = [r for r in (_heard.get(person) or ()) if now - r[0] <= WINDOW_S]
    if not rows:
        return ''
    lines = [f"{time.strftime('%H:%M:%S', time.localtime(at))} voice {letter}: "
             f"\"{words}\"" for at, letter, _score, words in rows]
    return VOICE_WINDOW_HEADER + '\n' + '\n'.join(lines)


def forget(person: str) -> None:
    """Drop everything heard on this person's device (tests, sign-out)."""
    with _lock:
        _voices.pop(person, None)
        _heard.pop(person, None)
        _last_seen.pop(person, None)
