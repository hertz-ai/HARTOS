"""A worker that fails to start says what kind of failure it was in fields
of its answer, and the vault says when a key has been stored.

Before (review rv_2ae3 of 2ae39fa58): every start failure was one string,
"<voice> worker startup failed: ...", and a reader had to match words.
Nunba's voice ladder set a voice aside for 7 days after one such start, so:

  - a network timeout, a full disk or the GPU out of memory while a worker
    STARTED cost a healthy voice 7 days.  Those now carry ``transient``,
    the flag a crash mid-request already carries.  A start that is really
    broken (a missing package, a bad model file, the worker's own startup
    deadline) carries no flag, as before;
  - a gated repo's answer was told apart by its sentence.  It now carries
    ``needs_key`` and ``key_refused`` (ai_key_vault.gated_weights_failure);
  - a voice set aside for want of a key had no way back when the owner
    stored one.  store_credential now tells its listeners the key's NAME
    (ai_key_vault.on_credential_stored).

Faked: the worker's start, the secrets store behind the vault (a dict), the
key ask and the audit log.  Key values are made up.
"""
import os
import sys
import types

import pytest

import hartos.ai_key_vault as kv
from integrations.service_tools import gpu_worker as gw

REPO = 'ai4bharat/indic-parler-tts'
PAGE = f'https://huggingface.co/{REPO}'
GATED_401 = (f'401 Client Error. Cannot access gated repo for url {PAGE}/'
             f'resolve/main/config.json.\nAccess to model {REPO} is restricted.')


class _Store:
    def __init__(self):
        self._cache = {}

    def get_secret(self, name):
        return self._cache.get(name, '')

    def set_secret(self, name, value):
        self._cache[name] = value


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


def _start_answer(error, stderr_tail=None):
    tw = gw.ToolWorker(tool_name='indic_parler', tool_module='x',
                       vram_budget='tts_indic_parler', output_subdir='t',
                       engine='t')
    if stderr_tail is not None:
        error.stderr_tail = stderr_tail

    def _start():
        raise error
    tw._get_or_start = _start
    return tw.call({'text': 'a'})


# ── a start that may pass ───────────────────────────────────────────

@pytest.mark.parametrize('why', [
    'ReadTimeout: HTTPSConnectionPool(host=huggingface.co): Read timed out.',
    'requests.exceptions.ConnectionError: Max retries exceeded with url',
    'socket.gaierror: [Errno 11001] getaddrinfo failed',
    'OSError: [Errno 28] No space left on device',
    'OSError: [WinError 112] There is not enough space on the disk',
    'torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB',
    'RuntimeError: CUDA error: out of memory',
])
def test_a_start_that_may_pass_is_flagged_transient(vault, why):
    answer = _start_answer(gw.WorkerError(why))
    assert answer == {
        'error': f'indic_parler worker startup failed: {why}',
        'transient': True}


def test_the_dead_childs_last_words_are_read_too(vault):
    crash = gw.WorkerCrash('indic_parler: died during startup (exit=1)')
    answer = _start_answer(crash, 'torch.OutOfMemoryError: CUDA out of memory.')
    assert answer == {
        'error': 'indic_parler worker startup failed: indic_parler: died '
                 'during startup (exit=1)',
        'transient': True}


@pytest.mark.parametrize('why', [
    "ModuleNotFoundError: No module named 'parler_tts'",
    'ImportError: DLL load failed while importing _C',
    'FileNotFoundError: model file not found: config.json',
    'RuntimeError: Error(s) in loading state_dict for ParlerTTS',
    'indic_parler: startup timeout (120s)',
    'indic_parler: died during startup (exit=1)',
])
def test_a_start_that_is_broken_keeps_the_answer_it_had(vault, why):
    answer = _start_answer(gw.WorkerError(why))
    assert answer == {'error': f'indic_parler worker startup failed: {why}'}


def test_a_broken_start_is_not_excused_by_a_network_line_above_it(vault):
    crash = gw.WorkerCrash('indic_parler: died during startup (exit=1)')
    answer = _start_answer(
        crash, 'warning: connection reset, retrying\n'
               "ModuleNotFoundError: No module named 'parler_tts'")
    assert 'transient' not in answer


def test_the_workers_own_startup_deadline_is_not_a_passing_failure(vault):
    """A worker that never answers would be started again on every call."""
    answer = _start_answer(
        gw.WorkerTimeout('indic_parler: startup timeout (120s)'))
    assert 'transient' not in answer


def test_a_crash_mid_request_is_flagged_as_it_was(vault):
    tw = gw.ToolWorker(tool_name='indic_parler', tool_module='x',
                       vram_budget='tts_indic_parler', output_subdir='t',
                       engine='t')

    class _Worker:
        def call(self, request):
            raise gw.WorkerTimeout('request timed out after 60s')
    tw._get_or_start = lambda: _Worker()
    tw._notify = lambda event: None
    assert tw.call({'text': 'a'}) == {
        'error': 'indic_parler crashed: request timed out after 60s',
        'transient': True}


# ── a gated repo's answer says which of the two it is ───────────────

def test_a_refused_stored_key_is_a_field_of_the_answer(vault):
    vault.store_credential('HEVOLVE_HF_TOKEN', 'made-up-key')
    answer = _start_answer(gw.WorkerError(GATED_401))
    assert answer == {
        'error': f'{REPO} refused the stored HEVOLVE_HF_TOKEN; accept its '
                 f'terms at {PAGE} with that account',
        'needs_key': 'HEVOLVE_HF_TOKEN',
        'key_refused': True}
    assert 'made-up-key' not in repr(answer)


def test_a_missing_key_is_a_field_of_the_answer(vault):
    answer = _start_answer(gw.WorkerError(GATED_401))
    assert answer == {
        'error': f'needs HEVOLVE_HF_TOKEN: {REPO} is gated; accept its terms '
                 f'at {PAGE}',
        'needs_key': 'HEVOLVE_HF_TOKEN',
        'key_refused': False}


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
    """Even when the 401 text sits next to a network line."""
    crash = gw.WorkerCrash('indic_parler: died during startup (exit=1)')
    answer = _start_answer(crash, 'connection reset, retrying\n' + GATED_401)
    assert 'transient' not in answer
    assert answer.get('needs_key') == 'HEVOLVE_HF_TOKEN'


# ── the vault says a key was stored ─────────────────────────────────

def _listen(monkeypatch=None):
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
    heard = _listen()
    vault.store_credential('HEVOLVE_HF_TOKEN', 'made-up-key', granted_only=True)
    assert heard == ['HEVOLVE_HF_TOKEN']


def test_a_store_the_vault_refuses_is_not_announced(vault, monkeypatch):
    heard = _listen()
    monkeypatch.setenv('SOME_PROCESS_NAME', 'held-by-the-process')
    vault.store_credential('SOME_PROCESS_NAME', 'made-up-key')
    assert heard == []
    assert vault._sm._cache == {}


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
