"""Phonetic transcription: the sounds a person made, in IPA, beside Whisper's words.

Owner 2026-11-25: "a catalog entry, a tool that runs it alongside multilingual
Whisper, and lifecycle registration so it can borrow GPU memory and give it
back", the On Top of Pasketti model.  Design note design_1125, section C:

  * the weights are a model-catalog row (PHONETIC_MODEL_ID, its own
    model_type 'phonetic', so no speech-to-text pick can take it) that model
    setup fetches on need through the orchestrator's 'phonetic' loader
    (PhoneticLoader below), from one repo at one pinned revision;
  * it runs in its own worker subprocess (gpu_worker), always in fp32 (its
    card: WavLM's relative position bias gives NaNs in fp16 and bf16), on the
    device the orchestrator decided;
  * an agent that works on how speech sounds (needs_phones) asks for it on
    its person's turn; while it is loaded, each utterance heard on the
    person's device gets its phones in the voice window
    (integrations/audio/voiceprint.py);
  * it was trained on English-speaking children, so a sound English lacks
    (Tamil's retroflex consonants, for example) comes out as the nearest
    English sound.  A hint for the agent, never a gate.
"""
import base64
import logging
import threading
import time
from types import SimpleNamespace

from integrations.audio.voiceprint import IDLE_S
from integrations.service_tools.gpu_worker import ToolWorker
from integrations.service_tools.model_orchestrator import ModelLoader

logger = logging.getLogger(__name__)

PHONETIC_MODEL_ID = 'phonetic-pasketti'
PHONETIC_REPO = 'drivendata/pasketti-phonetic'
#: The one revision fetched: the hub's commit for the files below, read from
#: the hub's model API on 2026-11-25.
PHONETIC_REVISION = '675dfe392a2af7050f1374eeab4458cb1f003b3e'
PHONETIC_WEIGHTS = 'model.safetensors'
#: The files model setup fetches at that revision, with their sizes in bytes
#: from the same listing.  A file is on disk only at exactly its size.
PHONETIC_FILES = {
    PHONETIC_WEIGHTS: 1262097724,
    'config.json': 2396,
    'preprocessor_config.json': 256,
    'vocab.json': 634,
    'tokenizer_config.json': 794,
    'special_tokens_map.json': 53,
    'added_tokens.json': 18,
}
PHONETIC_LICENSE = 'bigcode-openrail-m'
SAMPLE_RATE = 16000
#: The card's input limit: clips of 60 s or less.
MAX_SECONDS = 60
#: A fetch starts only when what it still needs is at most this share of the
#: free disk where it lands: an older hub writes a cache copy beside the
#: file, so a fetch can need twice its size.
FETCH_FREE_SHARE = 0.5
#: Words that mark an agent working on how speech sounds (design 1125 v3 N1).
#: Only the voice window reads them; tool loading never does.
PHONES_WORDS = ('speech therapy', 'speech delay', 'articulation', 'phonetic')

#: The persons whose own latest turn was with an agent that works on speech,
#: with when it was.  Phones are worked out only for them, for the speaker's
#: session window: the phone's transcribe route hears inside its request, so
#: phones for everyone made every transcription wait (review of 5ecf725e8).
_wanted = {}
_wanted_lock = threading.Lock()


def model_dir():
    """Where model setup puts the files: this row's folder under the model
    storage root."""
    from integrations.service_tools.model_storage import model_storage
    return model_storage.get_tool_dir(PHONETIC_MODEL_ID)


# ── Worker side (inside the subprocess) ──────────────────────────

def _load():
    """Load the pinned model in fp32.  The device is the orchestrator's: the
    parent hides the GPU from a worker it put on the CPU (ToolWorker.run_mode),
    so a GPU seen here is one the orchestrator chose.  Never builds an
    orchestrator: lifecycle registration is the parent's."""
    import torch
    from transformers import Wav2Vec2Processor, WavLMForCTC
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    path = str(model_dir())
    processor = Wav2Vec2Processor.from_pretrained(path)
    model = WavLMForCTC.from_pretrained(path).to(device=device, dtype=torch.float32).eval()
    return SimpleNamespace(processor=processor, model=model, device=device)


def _synthesize(state, req: dict) -> dict:
    """op 'phones': the IPA of one clip ('pcm16': base64 of 16-bit
    little-endian mono samples at SAMPLE_RATE)."""
    if req.get('op') != 'phones':
        return {'error': f"Unknown op: {req.get('op')}"}
    import numpy as np
    import torch
    raw = base64.b64decode(req.get('pcm16') or '')
    samples = np.frombuffer(raw[:len(raw) - len(raw) % 2], dtype='<i2').astype('float32') / 32768.0
    if not len(samples):
        return {'phones': '', 'device': state.device}
    inputs = state.processor(samples, sampling_rate=SAMPLE_RATE, return_tensors='pt')
    with torch.no_grad():
        logits = state.model(inputs.input_values.to(state.device)).logits
    said = state.processor.batch_decode(torch.argmax(logits, dim=-1))[0]
    return {'phones': said.strip(), 'device': state.device}


# ── Parent side ──────────────────────────────────────────────────

_tool = ToolWorker(
    tool_name=PHONETIC_MODEL_ID,
    tool_module='integrations.service_tools.phonetic_tool',
    vram_budget=PHONETIC_MODEL_ID,
    output_subdir=PHONETIC_MODEL_ID,   # not used: phones are answered, not written
    engine='pasketti',
    startup_timeout=120.0,   # 1.26 GB of fp32 weights read from disk
    request_timeout=120.0,   # a 60 s clip on a slow CPU
)


def _missing() -> dict:
    """The pinned files not on disk at their pinned size: {name: bytes}."""
    folder = model_dir()
    missing = {}
    for name, size in PHONETIC_FILES.items():
        try:
            if (folder / name).stat().st_size == size:
                continue
        except OSError:
            pass
        missing[name] = size
    return missing


class PhoneticLoader(ModelLoader):
    """The model-setup loader for model_type 'phonetic' (registered by
    model_orchestrator.register_hartos_loaders)."""

    def is_downloaded(self, entry) -> bool:
        return not _missing()

    def download(self, entry) -> bool:
        """Fetch the pinned files from PHONETIC_REPO at PHONETIC_REVISION,
        through model storage, when what is missing fits FETCH_FREE_SHARE of
        the free disk where it goes."""
        need = sum(_missing().values())
        if not need:
            return True
        from core.platform_paths import free_bytes
        try:
            free = free_bytes(model_dir())
        except OSError:
            logger.warning('phonetic: the free disk at %s could not be read', model_dir(),
                           exc_info=True)
            return False
        if need > free * FETCH_FREE_SHARE:
            logger.warning('phonetic: the model needs %.2f GB and %.2f GB is free where it '
                           'goes; a fetch may take %.0f%% of the free disk, so it is not '
                           'fetched', need / 1e9, free / 1e9, FETCH_FREE_SHARE * 100)
            return False
        from integrations.service_tools.model_storage import model_storage
        model_storage.download_hf_model(PHONETIC_MODEL_ID, PHONETIC_REPO,
                                        revision=PHONETIC_REVISION,
                                        allow_patterns=sorted(PHONETIC_FILES))
        return self.is_downloaded(entry)

    def load(self, entry, run_mode: str = 'cpu') -> bool:
        """Start the worker on the device the orchestrator decided, then set
        the row's idle timeout (the session window), so its timer runs from
        the start."""
        if not self.is_downloaded(entry) and not self.download(entry):
            return False
        _tool.run_mode = run_mode
        try:
            _tool._get_or_start()
        except Exception:
            logger.warning('phonetic: the model did not start', exc_info=True)
            return False
        if run_mode == 'gpu' and _tool.call({'op': 'phones', 'pcm16': ''}).get('device') != 'cuda':
            # The GPU was picked by free memory, but this torch has no CUDA
            # (the installed python-embed is a CPU build): the worker runs on
            # the CPU, so its booking is given back and a restart books none.
            logger.info('phonetic: torch here has no CUDA, so the model runs on the CPU')
            _tool.run_mode = 'cpu'
            from integrations.service_tools.vram_manager import vram_manager
            vram_manager.release(PHONETIC_MODEL_ID)
        idle_s = getattr(entry, 'idle_timeout_s', None)
        if idle_s is not None:
            _tool.set_idle_timeout(float(idle_s))
        return True

    def unload(self, entry) -> None:
        _tool.stop()

    def is_loaded(self, entry) -> bool:
        return _tool.is_alive()


def ready() -> bool:
    """True when the model is loaded.  When it is not, model setup is asked
    for it (on need, in the background) and this answers False: no phones on
    this turn, and the turn goes on."""
    if _tool.is_alive():
        return True
    try:
        from integrations.service_tools.model_orchestrator import get_orchestrator
        get_orchestrator().ensure_loaded_async('phonetic', caller='phonetic')
    except Exception:
        logger.warning('phonetic: model setup could not be asked for the phonetic model',
                       exc_info=True)
    return False


def want_phones(person: str, wanted: bool) -> None:
    """The person's own turn says whether their current agent works on speech
    (reuse_recipe._reuse_agent_hears_phones); their latest turn decides.
    Marks older than the session window are forgotten here."""
    now = time.time()
    with _wanted_lock:
        for gone in [p for p, at in _wanted.items() if now - at > IDLE_S]:
            del _wanted[gone]
        if wanted:
            _wanted[str(person)] = now
        else:
            _wanted.pop(str(person), None)


def _is_wanted(person) -> bool:
    with _wanted_lock:
        at = _wanted.get(str(person))
    return at is not None and time.time() - at <= IDLE_S


def phones(person, samples) -> str:
    """The IPA the model heard in one clip of SAMPLE_RATE mono float samples,
    for a person whose current agent works on speech (want_phones), or ''
    (not wanted, not loaded, no clip, a clip longer than MAX_SECONDS, or a
    worker error).  Never starts the model: only the turn of an agent that
    needs phones asks for it (ready).  Each use is stamped on the model's
    lifecycle record, so a model in use never reads as idle."""
    if samples is None or not _is_wanted(person) or not _tool.is_alive():
        return ''
    if not 0 < len(samples) <= MAX_SECONDS * SAMPLE_RATE:
        return ''
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype='float32'), -1.0, 1.0) * 32767).astype('<i2')
    got = _tool.call({'op': 'phones',
                      'pcm16': base64.b64encode(pcm.tobytes()).decode('ascii')})
    if not isinstance(got, dict) or got.get('error'):
        logger.warning('phonetic: no phones for this clip: %s',
                       got.get('error') if isinstance(got, dict) else got)
        return ''
    try:
        from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
        get_model_lifecycle_manager().notify_access(PHONETIC_MODEL_ID)
    except Exception:
        logger.debug('phonetic: the use was not stamped', exc_info=True)
    return str(got.get('phones') or '').strip()


def needs_phones(stored_tags, goal_text) -> bool:
    """True when an agent works on how speech sounds: one of its stored goal
    tags or its own goal text names one of PHONES_WORDS.  A stored value that
    is not a list of tags reads as none (the record is user-writable JSON)."""
    if not isinstance(stored_tags, (list, tuple, set, frozenset)):
        stored_tags = ()
    texts = [t for t in stored_tags if isinstance(t, str)] + [str(goal_text or '')]
    for text in texts:
        flat = text.lower().replace('_', ' ').replace('-', ' ')
        if any(word in flat for word in PHONES_WORDS):
            return True
    return False


# NOTE: no `if __name__ == '__main__':` block; the gpu_worker dispatcher
# resolves `_load` / `_synthesize` by convention.
