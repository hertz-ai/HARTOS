"""A worker that fails to start says what kind of failure it was and what it
died of, in fields of its answer; it is not started again on every call;
and the vault says when a key has been stored.

Before (reviews rv_2ae3 and rv_4625): every start failure was one string,
"<voice> worker startup failed: ...", and a reader had to match words.
Nunba's voice ladder set a voice aside for 7 days after one such start.

  - a network timeout, a full disk, or the GPU or the machine out of memory
    while a worker STARTED cost a healthy voice 7 days.  Those carry
    ``transient``, the flag a crash mid-request already carries.  A start
    that is really broken (a missing package, a bad model file, the
    worker's own startup deadline) carries no flag, as before;
  - "try again later" must not be "try again on the next line": after such
    a start the worker answers the same thing without spawning, for 30 s
    doubling to 10 minutes (core.circuit_breaker.PeerBackoff), until a
    start works.  Measured before this: 20 lines, 20 starts;
  - the kind is read from the exception the child died of, its LAST stderr
    line, not from the log lines above it;
  - the answer carries that line as ``cause`` (secrets masked, folders
    dropped), so whoever asked for the voice gets the real reason and not
    only "died during startup (exit=1)" (owner ruling 2026-11-22);
  - a gated repo's answer carries ``needs_key`` and ``key_refused``;
  - a key that arrives is announced by NAME through one function
    (ai_key_vault.announce_credential_stored), whichever door it came in by.

Faked: the worker's start, the secrets store behind the vault (a dict), the
key ask and the audit log.  Key values are made up.
"""
import os
import sys
import types

import pytest

import hartos.ai_key_vault as kv
from integrations.service_tools import gpu_worker as gw

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
REPO = 'ai4bharat/indic-parler-tts'
PAGE = f'https://huggingface.co/{REPO}'
GATED_401 = (f'401 Client Error. Cannot access gated repo for url {PAGE}/'
             f'resolve/main/config.json.\nAccess to model {REPO} is restricted.')
DIED = 'indic_parler: died during startup (exit=2)'
HF_RETRY = ("'(ReadTimeoutError(\"HTTPSConnectionPool(host='huggingface.co', "
            "port=443): Read timed out. (read timeout=10)\"), '(Request ID: "
            "1)')' thrown while requesting HEAD " + PAGE + "/resolve/main/"
            "config.json\nRetrying in 1s [Retry 1/5].\n")


class _Store:
    def __init__(self):
        self._cache = {}

    def get_secret(self, name):
        return self._cache.get(name, '')

    def set_secret(self, name, value):
        self._cache[name] = value

    def set_secrets(self, values):
        self._cache.update(values)

    def delete_secret(self, name):
        self._cache.pop(name, None)


@pytest.fixture
def vault(monkeypatch):
    monkeypatch.delenv('HEVOLVE_HF_TOKEN', raising=False)
    monkeypatch.delenv('HF_TOKEN', raising=False)
    monkeypatch.setitem(
        sys.modules, 'security.immutable_audit_log',
        types.SimpleNamespace(get_audit_log=lambda: types.SimpleNamespace(
            log_event=lambda *a, **k: None)))
    v = kv.AIKeyVault()
    v._sm = _Store()
    monkeypatch.setattr(kv, 'get_ai_key_vault', lambda: v)
    monkeypatch.setattr(kv, 'reads_from_env', lambda name: False)
    monkeypatch.setattr(kv, '_STORED_LISTENERS', [], raising=False)
    monkeypatch.setattr(kv, 'request_credential',
                        lambda desc, agent_id=None, **kw: 'asked')
    return v


def _worker(error=None, stderr_tail=None):
    """A voice's worker whose start raises ``error``; ``starts`` counts."""
    tw = gw.ToolWorker(tool_name='indic_parler', tool_module='x',
                       vram_budget='tts_indic_parler', output_subdir='t',
                       engine='t')
    if error is not None and stderr_tail is not None:
        error.stderr_tail = stderr_tail
    tw.starts = 0
    tw.fails_with = error

    class _Alive:
        def call(self, request):
            return {'path': 'a.wav', 'duration': 1.0}

    def _start():
        tw.starts += 1
        if tw.fails_with is not None:
            raise tw.fails_with
        return _Alive()
    tw._get_or_start = _start
    tw._touch_idle = lambda: None
    return tw


def _clock(monkeypatch):
    """The backoff's clock, in the test's hands: [seconds]."""
    import time as real
    import core.circuit_breaker as cb
    now = [1000.0]
    fake = types.SimpleNamespace(
        **{name: getattr(real, name) for name in dir(real)
           if not name.startswith('_')})
    fake.monotonic = lambda: now[0]
    monkeypatch.setattr(cb, 'time', fake)
    return now


def _start_answer(error, stderr_tail=None):
    return _worker(error, stderr_tail).call({'text': 'a'})


# ── a start that may pass ───────────────────────────────────────────

@pytest.mark.parametrize('why', [
    'ReadTimeout: HTTPSConnectionPool(host=huggingface.co): Read timed out.',
    'requests.exceptions.ConnectionError: Max retries exceeded with url',
    'socket.gaierror: [Errno 11001] getaddrinfo failed',
    'OSError: [Errno 28] No space left on device',
    'OSError: [WinError 112] There is not enough space on the disk',
    'torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB',
    'RuntimeError: CUDA error: out of memory',
    # the machine's memory, not the GPU's
    'MemoryError',
    'RuntimeError: [enforce fail at alloc_cpu.cpp:114] data. '
    'DefaultCPUAllocator: not enough memory: you tried to allocate 1073741824 '
    'bytes.',
    "RuntimeError: DefaultCPUAllocator: can't allocate memory: you tried to "
    "allocate 1073741824 bytes. Error code 12 (Cannot allocate memory)",
])
def test_a_start_that_may_pass_is_flagged_transient(vault, why):
    answer = _start_answer(gw.WorkerCrash(DIED), why)
    assert answer.get('error') == f'indic_parler worker startup failed: {DIED}'
    assert answer.get('transient') is True
    assert answer.get('cause') == why.rstrip(' .')


def test_the_error_itself_is_read_when_the_child_said_nothing(vault):
    answer = _start_answer(gw.WorkerError('CUDA out of memory'))
    assert answer.get('error') == \
        'indic_parler worker startup failed: CUDA out of memory'
    assert answer.get('transient') is True


@pytest.mark.parametrize('why', [
    "ModuleNotFoundError: No module named 'parler_tts'",
    'ImportError: DLL load failed while importing _C',
    'FileNotFoundError: model file not found: config.json',
    'RuntimeError: Error(s) in loading state_dict for ParlerTTS',
    'safetensors_rust.SafetensorError: Error while deserializing header: '
    'HeaderTooLarge',
    "_pickle.UnpicklingError: invalid load key, '<'.",
    # a missing package whose name happens to hold a network word
    "ModuleNotFoundError: No module named 'timeout_decorator'",
    # words that only LOOK like the network's or the memory's (review
    # rv_vt: 'timeout' and 'is not running' were matched as substrings)
    'ValueError: invalid timeout_s config value',
    'RuntimeError: the sidecar is not running',
    'KeyError: connection_timeout',
    'ImportError: libgomp.so.1: cannot allocate memory in static TLS block',
])
def test_a_start_that_is_broken_is_not_flagged(vault, why):
    answer = _start_answer(gw.WorkerCrash(DIED), why)
    assert answer.get('error') == f'indic_parler worker startup failed: {DIED}'
    assert 'transient' not in answer
    assert answer.get('cause') == why.rstrip(' .')


@pytest.mark.parametrize('log_above, died_of', [
    # the hub's retry log above a model file that will not load
    (HF_RETRY, 'safetensors_rust.SafetensorError: Error while deserializing '
               'header: HeaderTooLarge'),
    # a probe's own retry note
    ('[indic_parler] probe: fp16 out of memory on a 64-token warmup, retrying '
     'in fp32\n', 'RuntimeError: Error(s) in loading state_dict for '
                  'ParlerTTSForConditionalGeneration: size mismatch'),
    # a lock's wait
    ('INFO filelock: Lock 2264 acquired... wait timed out after 1s, retrying\n',
     "_pickle.UnpicklingError: invalid load key, '<'."),
    (HF_RETRY, "ModuleNotFoundError: No module named 'parler_tts'"),
])
def test_only_what_the_child_died_of_is_read_not_its_log(vault, log_above,
                                                         died_of):
    """Review rv_4625: any of 40 tail lines used to decide."""
    answer = _start_answer(gw.WorkerCrash(DIED), log_above + died_of + '\n')
    assert 'transient' not in answer
    assert answer.get('cause') == died_of.rstrip(' .')


def test_the_workers_own_startup_deadline_is_not_a_passing_failure(vault):
    """A worker that never answers would hold every later line for the
    whole deadline."""
    answer = _start_answer(
        gw.WorkerTimeout('indic_parler: startup timeout (120s)'),
        'Read timed out. Retrying')
    assert 'transient' not in answer


def test_a_crash_mid_request_is_flagged_as_it_was(vault):
    tw = _worker()

    class _Worker:
        def call(self, request):
            raise gw.WorkerTimeout('request timed out after 60s')
    tw._get_or_start = lambda: _Worker()
    tw._notify = lambda event: None
    assert tw.call({'text': 'a'}) == {
        'error': 'indic_parler crashed: request timed out after 60s',
        'transient': True}


# ── later, not on the next call ─────────────────────────────────────

def _disk(monkeypatch, free=10 * 2 ** 30):
    """The free space every disk reports, in the test's hands: [bytes]."""
    import shutil
    now = [free]
    monkeypatch.setattr(
        shutil, 'disk_usage',
        lambda path: types.SimpleNamespace(total=2 ** 40, used=0, free=now[0]))
    return now


FULL_DISK = 'OSError: [Errno 28] No space left on device'


def test_a_voice_whose_start_may_pass_is_not_started_on_every_call(
        vault, monkeypatch):
    """Review rv_4625: a full disk, 20 lines, 20 worker starts."""
    _disk(monkeypatch)
    tw = _worker(gw.WorkerCrash(DIED), FULL_DISK)
    first = tw.call({'text': 'a'})
    later = [tw.call({'text': 'a'}) for _ in range(19)]
    assert tw.starts == 1
    assert first.get('error') == f'indic_parler worker startup failed: {DIED}'
    assert first.get('transient') is True and first.get('cause') == FULL_DISK
    assert 'held_back' not in first


def test_while_it_waits_the_answer_says_so_and_when_it_is_tried_again(
        vault, monkeypatch):
    """Review rv_vt: the failed start's own answer was served again, so it
    never said a wait was on, nor until when."""
    import time
    _disk(monkeypatch)
    _clock(monkeypatch)
    tw = _worker(gw.WorkerCrash(DIED), FULL_DISK)
    tw.call({'text': 'a'})
    answer = tw.call({'text': 'a'})
    assert answer.get('held_back') is True
    assert answer.get('transient') is True
    assert answer.get('cause') == FULL_DISK
    wait = answer.get('retry_at', 0) - time.time()
    assert 29 < wait < 31
    at = time.strftime('%H:%M:%S', time.localtime(answer['retry_at']))
    assert answer.get('error') == (
        f'indic_parler is held back after a failed start: {FULL_DISK}; '
        f'it is tried again at {at}')
    # not the words of a start that failed just now: none was made
    assert 'worker startup failed' not in answer['error']
    assert tw.starts == 1


def test_a_disk_with_room_again_lets_one_start_through_early(
        vault, monkeypatch):
    """Review rv_vt: with the disk freed during the wait, the next call
    still answered "No space left on device" and started nothing."""
    free = _disk(monkeypatch, 2 ** 20)
    _clock(monkeypatch)
    tw = _worker(gw.WorkerCrash(DIED), FULL_DISK)
    for _ in range(5):
        tw.call({'text': 'a'})
    assert tw.starts == 1
    free[0] += 100 * 2 ** 20            # not enough to matter
    assert tw.call({'text': 'a'}).get('held_back') is True
    assert tw.starts == 1
    free[0] += 200 * 2 ** 20            # 300 MB more than when it failed
    tw.fails_with = None
    assert tw.call({'text': 'a'}).get('path') == 'a.wav'
    assert tw.starts == 2
    assert tw.call({'text': 'a'}).get('path') == 'a.wav'


def test_an_early_start_that_fails_again_is_one_start_not_a_storm(
        vault, monkeypatch):
    free = _disk(monkeypatch, 2 ** 20)
    _clock(monkeypatch)
    tw = _worker(gw.WorkerCrash(DIED), FULL_DISK)
    tw.call({'text': 'a'})
    free[0] += 300 * 2 ** 20
    for _ in range(10):
        tw.call({'text': 'a'})
    # the freed disk bought one start; it failed, and the disk has not
    # grown since
    assert tw.starts == 2
    assert tw.call({'text': 'a'}).get('held_back') is True


def test_a_freed_disk_lets_nothing_through_for_another_cause(
        vault, monkeypatch):
    free = _disk(monkeypatch, 2 ** 20)
    _clock(monkeypatch)
    tw = _worker(gw.WorkerCrash(DIED),
                 'torch.OutOfMemoryError: CUDA out of memory.')
    tw.call({'text': 'a'})
    free[0] += 10 * 2 ** 30
    for _ in range(5):
        assert tw.call({'text': 'a'}).get('held_back') is True
    assert tw.starts == 1


def test_what_python_prints_after_the_exception_is_not_the_cause(vault):
    """Review rv_vt: an "Exception ignored in: ..." block follows the real
    exception when an object is torn down at exit; its last line was read
    as the cause and made a CUDA out-of-memory start a broken one."""
    answer = _start_answer(
        gw.WorkerCrash(DIED),
        'Traceback (most recent call last):\n'
        '  File "worker.py", line 9, in _load\n'
        'torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB\n'
        'Exception ignored in: <function Model.__del__ at 0x000001>\n'
        'Traceback (most recent call last):\n'
        '  File "model.py", line 3, in __del__\n'
        "AttributeError: 'NoneType' object has no attribute 'close'\n")
    assert answer.get('transient') is True
    assert answer.get('cause') == \
        'torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB'


def test_it_is_started_again_once_the_wait_is_over(vault, monkeypatch):
    tw = _worker(gw.WorkerError('CUDA out of memory'))
    now = _clock(monkeypatch)
    for _ in range(5):
        tw.call({'text': 'a'})
    assert tw.starts == 1
    now[0] += 29.0
    tw.call({'text': 'a'})
    assert tw.starts == 1
    now[0] += 2.0            # 31 s after the first failure
    tw.call({'text': 'a'})
    assert tw.starts == 2
    now[0] += 59.0           # the wait doubled to 60 s
    tw.call({'text': 'a'})
    assert tw.starts == 2
    now[0] += 2.0
    tw.call({'text': 'a'})
    assert tw.starts == 3


def test_the_wait_never_grows_past_ten_minutes(vault, monkeypatch):
    tw = _worker(gw.WorkerError('CUDA out of memory'))
    now = _clock(monkeypatch)
    for _ in range(12):
        tw.call({'text': 'a'})
        now[0] += 601.0
    assert tw.starts == 12


def test_a_start_that_works_ends_the_wait(vault, monkeypatch):
    tw = _worker(gw.WorkerError('CUDA out of memory'))
    now = _clock(monkeypatch)
    tw.call({'text': 'a'})
    now[0] += 31.0
    tw.fails_with = None
    assert tw.call({'text': 'a'}).get('path') == 'a.wav'
    assert tw.call({'text': 'a'}).get('path') == 'a.wav'
    assert tw.starts == 3
    # the next failure begins at the shortest wait again
    tw.fails_with = gw.WorkerError('CUDA out of memory')
    tw.call({'text': 'a'})
    now[0] += 31.0
    tw.call({'text': 'a'})
    assert tw.starts == 5


def test_a_broken_start_and_a_gated_one_are_not_held_back(vault):
    """They are their reader's to count, as before: one start each call."""
    broken = _worker(gw.WorkerCrash(DIED),
                     "ModuleNotFoundError: No module named 'parler_tts'")
    gated = _worker(gw.WorkerError(GATED_401))
    for _ in range(3):
        broken.call({'text': 'a'})
        gated.call({'text': 'a'})
    assert (broken.starts, gated.starts) == (3, 3)


def test_one_voices_wait_is_not_anothers(vault):
    full = _worker(gw.WorkerError('CUDA out of memory'))
    other = _worker(gw.WorkerError('CUDA out of memory'))
    full.call({'text': 'a'})
    full.call({'text': 'a'})
    other.call({'text': 'a'})
    assert (full.starts, other.starts) == (1, 1)


# ── what it died of, for whoever asked ──────────────────────────────

def test_the_cause_is_what_the_child_died_of(vault):
    answer = _start_answer(
        gw.WorkerCrash(DIED),
        "loading model...\nModuleNotFoundError: No module named 'parler_tts'\n")
    assert answer.get('cause') == \
        "ModuleNotFoundError: No module named 'parler_tts'"
    # 'error' keeps its words
    assert answer.get('error') == f'indic_parler worker startup failed: {DIED}'


def test_a_secret_in_the_cause_is_masked_and_folders_are_dropped(vault):
    key = 'sk-' + 'A1b2C3d4E5f6G7h8' * 3
    answer = _start_answer(
        gw.WorkerCrash(DIED),
        f"OSError: cannot read C:\\Users\\someone\\.cache\\hf\\model.safetensors "
        f"with api_key={key} from {PAGE}/resolve/main/config.json")
    cause = answer.get('cause') or ''
    assert key not in cause
    assert 'someone' not in cause
    assert 'model.safetensors' in cause
    assert f'{PAGE}/resolve/main/config.json' in cause
    assert cause.startswith('OSError: cannot read ')


@pytest.mark.parametrize('line, kept, gone', [
    # the reviewer's lines (rv_vt): a folder whose name holds a space
    ("OSError: [Errno 28] No space left on device: "
     "'C:\\Users\\Jane Doe\\AppData\\Local\\Nunba\\models\\x.bin'",
     "x.bin", ('Jane', 'Doe', 'Users', 'AppData')),
    ('FileNotFoundError: C:\\Program Files\\Nunba\\lib\\tts\\x.py',
     'x.py', ('Program', 'Files', 'Nunba')),
    # as pip and repr() write it, every backslash doubled
    ("OSError: [Errno 28] No space left on device: "
     "'C:\\\\Users\\\\Jane Doe\\\\models\\\\x.bin'",
     'x.bin', ('Jane', 'Doe', 'Users')),
    ('OSError: [Errno 28] No space left on device: '
     "'/home/jane doe/.cache/huggingface/x.bin'",
     'x.bin', ('jane', 'doe', 'huggingface')),
])
def test_a_folder_name_with_a_space_leaves_nothing_behind(vault, line, kept,
                                                          gone):
    """befa0bc31 cut paths with a pattern of its own: "Jane Doex.bin".  The
    one helper that already does this (runtime_manager._setup_reason) is
    used now."""
    cause = _start_answer(gw.WorkerCrash(DIED), line).get('cause') or ''
    assert kept in cause
    for word in gone:
        assert word not in cause
    assert cause.startswith(line.split(':')[0])


def test_the_path_is_cut_by_the_setup_helper_not_by_a_second_one():
    """Shape: no path pattern of the worker's own."""
    src = open(os.path.join(ROOT, 'integrations', 'service_tools',
                            'gpu_worker.py'), encoding='utf-8').read()
    assert '_setup_reason' in src
    assert '_URL_OR_PATH' not in src


def test_a_gated_answer_says_what_the_hub_said(vault):
    answer = _start_answer(gw.WorkerCrash(DIED), GATED_401)
    assert answer.get('cause') == f'Access to model {REPO} is restricted'


def test_the_agents_speech_result_carries_the_cause(vault, monkeypatch):
    """Owner ruling 2026-11-22: "Real failure reasons should be available to
    the agent so that it can ask correct questions".  The agent's TTS tool
    returns the router's result (TTSResult.to_dict): each engine that was
    tried says what its worker died of."""
    from integrations.channels.media import tts_router
    from integrations.service_tools import indic_parler_tool
    tw = _worker(gw.WorkerCrash(DIED),
                 'OSError: [Errno 28] No space left on device: '
                 'C:\\Users\\someone\\.cache\\hf\\model.safetensors')
    monkeypatch.setattr(indic_parler_tool, '_tool', tw)
    result = tts_router.TTSRouter().synthesize(
        'vanakkam', language='ta', engine_override='indic_parler',
        source='agent_tool').to_dict()
    said = ' '.join(result.get('warnings') or [])
    assert 'No space left on device' in said
    assert 'model.safetensors' in said and 'someone' not in said
    assert f'indic_parler worker startup failed: {DIED}' in said


# ── a gated repo's answer says which of the two it is ───────────────

def test_a_refused_stored_key_is_a_field_of_the_answer(vault):
    vault.store_credential('HEVOLVE_HF_TOKEN', 'made-up-key')
    answer = _start_answer(gw.WorkerError(GATED_401))
    assert answer.get('error') == (
        f'{REPO} refused the stored HEVOLVE_HF_TOKEN; accept its terms at '
        f'{PAGE} with that account')
    assert answer.get('needs_key') == 'HEVOLVE_HF_TOKEN'
    assert answer.get('key_refused') is True
    assert 'transient' not in answer
    assert 'made-up-key' not in repr(answer)


def test_a_missing_key_is_a_field_of_the_answer(vault):
    answer = _start_answer(gw.WorkerError(GATED_401))
    assert answer.get('error') == (
        f'needs HEVOLVE_HF_TOKEN: {REPO} is gated; accept its terms at {PAGE}')
    assert answer.get('needs_key') == 'HEVOLVE_HF_TOKEN'
    assert answer.get('key_refused') is False


def test_the_fields_are_the_vaults_and_the_words_are_unchanged(vault):
    failure = getattr(kv, 'gated_weights_failure', None)
    assert failure is not None
    assert failure(REPO, voice='f5_tts')['error'] == \
        kv.gated_weights_answer(REPO, voice='f5_tts')
    assert failure(REPO, 'voice setup')['error'] == \
        kv.gated_weights_answer(REPO, 'voice setup')
    vault.store_credential('HEVOLVE_HF_TOKEN', 'made-up-key')
    assert failure(REPO)['error'] == kv.gated_weights_answer(REPO)
    assert failure(REPO)['key_refused'] is True


def test_a_gated_start_is_never_flagged_transient(vault):
    """Even when the 401 text sits under a network line."""
    answer = _start_answer(gw.WorkerCrash(DIED),
                           'connection reset, retrying\n' + GATED_401)
    assert 'transient' not in answer
    assert answer.get('needs_key') == 'HEVOLVE_HF_TOKEN'


# ── the vault says a key was stored ─────────────────────────────────

def _listen():
    heard = []
    register = getattr(kv, 'on_credential_stored', None)
    assert register is not None, 'the vault has no way to say a key was stored'
    register(heard.append)
    return heard


def test_a_stored_key_is_announced_by_name_only(vault):
    heard = _listen()
    vault.store_credential('hevolve_hf_token', 'made-up-key')
    assert heard == ['HEVOLVE_HF_TOKEN']
    vault.store_credential('BOT_TOKEN', 'made-up-other', 'discord')
    assert heard == ['HEVOLVE_HF_TOKEN', 'DISCORD_BOT_TOKEN']
    assert 'made-up' not in repr(heard)


def test_a_key_typed_on_a_consent_card_is_announced_too(vault):
    """Once its grant has committed (consent_api.grant_consent calls
    announce_credential_stored), never by the store: a grant that fails
    takes the value back (review rv_4625)."""
    heard = _listen()
    # The answer's value and its record go in one save (set_secrets).
    vault._sm.set_secrets = lambda changes: vault._sm._cache.update(changes)
    vault.store_credential('HEVOLVE_HF_TOKEN', 'made-up-key', granted_only=True)
    assert heard == []
    kv.announce_credential_stored('HEVOLVE_HF_TOKEN')
    assert heard == ['HEVOLVE_HF_TOKEN']


def test_there_is_one_announce_for_every_door(vault):
    """Nunba's desktop card stores in its own vault and calls this itself
    (review rv_4625: the card never passed through store_credential)."""
    heard = _listen()
    announce = getattr(kv, 'announce_credential_stored', None)
    assert announce is not None, 'no public announce for a key stored elsewhere'
    announce('HEVOLVE_HF_TOKEN')
    assert heard == ['HEVOLVE_HF_TOKEN']


def test_a_value_entered_again_on_the_desktop_card_is_announced(vault):
    """hold_credential: what Nunba's vault hands over.  The first hold of a
    run is the app handing over what it already had, at every start."""
    heard = _listen()
    vault.hold_credential('SITE_PASSWORD', 'made-up-one')
    assert heard == []
    vault.hold_credential('SITE_PASSWORD', 'made-up-one')
    assert heard == []
    vault.hold_credential('SITE_PASSWORD', 'made-up-two')
    assert heard == ['SITE_PASSWORD']
    assert vault.get_tool_key('SITE_PASSWORD') == 'made-up-two'


def test_a_store_the_vault_refuses_is_not_announced(vault, monkeypatch):
    heard = _listen()
    monkeypatch.setenv('SOME_PROCESS_NAME', 'held-by-the-process')
    vault.store_credential('SOME_PROCESS_NAME', 'made-up-key')
    assert heard == []
    assert vault._sm._cache.get('SOME_PROCESS_NAME') is None


def test_a_listener_is_kept_once_and_one_that_raises_stops_nothing(vault):
    register = getattr(kv, 'on_credential_stored', None)
    assert register is not None
    heard = []

    def boom(name):
        raise RuntimeError('listener broke')
    register(boom)
    register(heard.append)
    register(heard.append)
    assert vault.store_credential('HEVOLVE_HF_TOKEN', 'made-up-key') == \
        'HEVOLVE_HF_TOKEN'
    assert heard == ['HEVOLVE_HF_TOKEN']
    assert vault.get_tool_key('HEVOLVE_HF_TOKEN') == 'made-up-key'
