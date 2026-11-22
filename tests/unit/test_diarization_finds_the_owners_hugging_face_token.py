"""Speaker diarization finds the owner's Hugging Face token wherever it is kept.

WHAT WAS WRONG (audit row H21, copy)
  DiarizationService.start() read HEVOLVE_HF_TOKEN from the environment
  itself, then config.json, beside hartos.ai_key_vault.hf_token, the one
  lookup every other reader of that token uses (the environment, then the
  owner's vault).  A token the owner had stored in the vault and that was not
  also in os.environ left diarization switched off with "No HuggingFace
  token", and the sidecar, which reads its own environment, never got it.

THE RULE
  - the token is hf_token()'s answer, else config.json's 'huggingface' key
    (here, then ~/.hevolve), as before;
  - the sidecar is started with that token as HEVOLVE_HF_TOKEN in its
    environment unless the environment already names one (gpu_worker's rule
    for its workers);
  - CARRIED OVER: a token in the environment, a token in config.json, no
    token (not started, a warning) and no whisperx (not started).

    python -m pytest tests/unit/test_diarization_finds_the_owners_hugging_face_token.py -q
"""
import json
import os

import pytest

from integrations.audio import diarization_service as ds

VAULT = 'hf_from_the_vault'
ENV = 'hf_from_the_environment'
FILE = 'hf_from_config_json'


class _Proc:
    stdout = None

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A service whose sidecar launch is recorded; an empty home and working
    folder; the vault's answer in ``rig.vault``."""
    import hartos.ai_key_vault as vault

    class _Rig:
        started = []
        vault = ''
    rig = _Rig()
    rig.started = []

    def popen(cmd, **kw):
        rig.started.append((cmd, dict(kw.get('env') or os.environ)))
        return _Proc()
    monkeypatch.setattr(ds.subprocess, 'Popen', popen)
    monkeypatch.setattr(ds.subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    monkeypatch.setattr(ds.atexit, 'register', lambda fn: fn)
    monkeypatch.setattr(ds.DiarizationService, '_is_whisperx_available', lambda self: True)
    # hf_token as it is written: the environment first, then the vault.
    monkeypatch.setattr(vault, 'hf_token',
                        lambda: os.environ.get('HEVOLVE_HF_TOKEN', '') or rig.vault)

    # The vault itself, for the code that asks why no token came: never the
    # real one.  ``rig.vault_error`` is what a locked vault raises.
    class _Vault:
        def get_tool_key(self, name):
            rig.vault_asked.append(name)
            if rig.vault_error is not None:
                raise rig.vault_error
            return os.environ.get(name, '') or rig.vault

        def unavailable(self):
            return rig.vault_state
    rig.vault_error, rig.vault_asked, rig.vault_state = None, [], ''
    monkeypatch.setattr(vault, 'get_ai_key_vault', lambda: _Vault())
    monkeypatch.delenv('HEVOLVE_HF_TOKEN', raising=False)
    home = tmp_path / 'home'
    work = tmp_path / 'work'
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(work)
    rig.home, rig.work = home, work
    svc = ds.DiarizationService.__new__(ds.DiarizationService)
    svc._port, svc._process, svc._running, svc._ready = 1, None, False, False
    rig.svc = svc
    return rig


def _child_token(rig):
    assert len(rig.started) == 1, rig.started
    return rig.started[0][1].get('HEVOLVE_HF_TOKEN')


# -- carried over: these pass before the fold and after it --

def test_a_token_in_the_environment_starts_the_sidecar_with_it(rig, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', ENV)
    rig.svc.start()
    assert rig.svc._running is True
    assert _child_token(rig) == ENV


@pytest.mark.parametrize('where', ['work', 'home'])
def test_a_token_in_config_json_starts_the_sidecar(rig, where):
    folder = rig.work if where == 'work' else rig.home / '.hevolve'
    folder.mkdir(exist_ok=True)
    (folder / 'config.json').write_text(json.dumps({'huggingface': FILE}))
    rig.svc.start()
    assert rig.svc._running is True and len(rig.started) == 1


def test_no_token_anywhere_starts_nothing_and_says_so(rig, caplog):
    rig.svc.start()
    assert rig.svc._running is False and rig.started == []
    assert 'No HuggingFace token' in caplog.text


def test_the_no_token_warning_names_every_place_a_token_is_read_from(rig, caplog):
    """It said only "Set HEVOLVE_HF_TOKEN env var." after the vault and
    config.json had become places the token is found (review of 5437e38a3)."""
    rig.svc.start()
    said = ' '.join(r.getMessage() for r in caplog.records
                    if 'No HuggingFace token' in r.getMessage())
    assert 'vault' in said and '/api/credentials/submit' in said
    assert 'env var' in said and 'config.json' in said


@pytest.mark.parametrize('where', ['vault', 'environment', 'config.json'])
def test_the_token_is_never_written_to_the_log(rig, caplog, monkeypatch, where):
    caplog.set_level(0)
    secret = 'hf_SECRETVALUE_abc123XYZ'
    if where == 'vault':
        rig.vault = secret
    elif where == 'environment':
        monkeypatch.setenv('HEVOLVE_HF_TOKEN', secret)
    else:
        (rig.work / 'config.json').write_text(json.dumps({'huggingface': secret}))
    rig.svc.start()
    assert len(rig.started) == 1
    assert secret not in caplog.text
    assert all(secret not in str(r.args) for r in caplog.records)


def test_a_config_json_that_cannot_be_read_is_no_token(rig):
    (rig.work / 'config.json').write_text('{not json')
    rig.svc.start()
    assert rig.started == []


def test_without_whisperx_nothing_starts(rig, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', ENV)
    monkeypatch.setattr(ds.DiarizationService, '_is_whisperx_available', lambda self: False)
    rig.svc.start()
    assert rig.started == [] and rig.svc._running is False


def test_the_sidecar_command_is_unchanged(rig, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', ENV)
    rig.svc.start()
    cmd = rig.started[0][0]
    assert cmd[1:] == ['-m', 'integrations.audio.diarization_server', '--port', '1']


# -- the fold --

def test_a_token_kept_only_in_the_vault_starts_the_sidecar_with_it(rig):
    rig.vault = VAULT
    rig.svc.start()
    assert rig.svc._running is True
    assert _child_token(rig) == VAULT
    assert 'HEVOLVE_HF_TOKEN' not in os.environ, 'only the sidecar is given it'


def test_a_config_json_token_reaches_the_sidecar_too(rig):
    (rig.work / 'config.json').write_text(json.dumps({'huggingface': FILE}))
    rig.svc.start()
    assert _child_token(rig) == FILE


def test_the_environments_token_wins_over_the_vaults(rig, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', ENV)
    rig.vault = VAULT
    rig.svc.start()
    assert _child_token(rig) == ENV


def test_the_vault_wins_over_config_json(rig):
    rig.vault = VAULT
    (rig.work / 'config.json').write_text(json.dumps({'huggingface': FILE}))
    rig.svc.start()
    assert _child_token(rig) == VAULT


def test_a_bare_launch_looks_the_token_up_itself(rig):
    """_start_subprocess() with no argument, as its other callers use it."""
    rig.vault = VAULT
    rig.svc._start_subprocess()
    assert _child_token(rig) == VAULT


def test_a_launch_with_no_token_names_none(rig):
    rig.svc._start_subprocess()
    assert _child_token(rig) is None


# -- what the warning tells the person to do (review of 085f4a0d6) ----------

def _no_token_warning(caplog):
    said = [r.getMessage() for r in caplog.records if 'No HuggingFace token' in r.getMessage()]
    assert len(said) == 1, said
    return said[0]


def test_the_warning_says_a_restart_is_needed_after_storing_the_token(rig, caplog):
    """The sidecar is started once, when the app starts."""
    rig.svc.start()
    said = _no_token_warning(caplog)
    assert 'Store it in the vault' in said and said.endswith('then restart the app.')
    assert rig.vault_asked == ['HEVOLVE_HF_TOKEN']


def test_a_locked_vault_is_named_as_locked_not_as_empty(rig, caplog, monkeypatch):
    """hf_token answers '' for a vault that cannot be read; "store the
    token" is the wrong thing to tell a person whose token is in it."""
    rig.vault = VAULT                       # stored, and the vault will not open
    rig.vault_error = RuntimeError('vault locked')
    import hartos.ai_key_vault as vault
    # what hf_token gives for a vault that raises
    monkeypatch.setattr(vault, 'hf_token', lambda: '')
    rig.svc.start()
    assert rig.svc._running is False and rig.started == []
    said = _no_token_warning(caplog)
    assert 'The vault could not be read (vault locked)' in said
    assert 'Unlock the vault' in said and said.endswith('then restart the app.')
    assert 'Store it in the vault' not in said
    assert VAULT not in caplog.text


def test_a_locked_vault_with_a_token_in_config_json_still_starts(rig, caplog):
    rig.vault_error = RuntimeError('vault locked')
    (rig.work / 'config.json').write_text(json.dumps({'huggingface': FILE}))
    rig.svc.start()
    assert rig.svc._running is True and _child_token(rig) == FILE
    assert 'No HuggingFace token' not in caplog.text
    assert rig.vault_asked == [], 'the vault is asked why only when no token was found'


def test_nothing_the_vault_holds_is_ever_in_the_warning(rig, caplog, monkeypatch):
    """The vault is asked only whether it answers; what it answers is not said."""
    import hartos.ai_key_vault as vault
    rig.vault = VAULT
    monkeypatch.setattr(vault, 'hf_token', lambda: '')
    rig.svc.start()
    said = _no_token_warning(caplog)
    assert 'Store it in the vault' in said and VAULT not in caplog.text


# -- "locked" is read from the vault's own state (review of 996bdd897) ------
# A real vault that cannot be opened does not raise: SecretsManager logs it
# and answers '' for every name, as an empty vault does.  The locked wording
# above ran only for a fake that raised.  The state is kept where it is
# known (SecretsManager.unavailable) and read through the vault
# (AIKeyVault.unavailable).

def test_a_vault_that_says_it_is_closed_is_named_without_raising(rig, caplog, monkeypatch):
    import hartos.ai_key_vault as vault
    monkeypatch.setattr(vault, 'hf_token', lambda: '')
    rig.vault_state = 'HEVOLVE_MASTER_KEY is not set, so the encrypted vault cannot be opened'
    rig.svc.start()
    assert rig.svc._running is False and rig.started == []
    said = _no_token_warning(caplog)
    assert ('The vault could not be read (HEVOLVE_MASTER_KEY is not set, so the encrypted '
            'vault cannot be opened)') in said
    assert 'opens with the HEVOLVE_MASTER_KEY it was saved with' in said
    assert 'Store it in the vault' not in said and said.endswith('restart the app.')


def test_a_vaults_error_text_is_redacted_before_it_is_said(rig, caplog, monkeypatch):
    import hartos.ai_key_vault as vault
    monkeypatch.setattr(vault, 'hf_token', lambda: '')
    leaked = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'
    rig.vault_error = RuntimeError(f'keyring refused token {leaked} for this user')
    rig.svc.start()
    said = _no_token_warning(caplog)
    assert 'The vault could not be read (keyring refused token [REDACTED' in said, said
    assert leaked not in caplog.text
    assert leaked not in str(rig.svc.get_status())


def test_an_error_that_cannot_be_redacted_is_named_by_its_type_alone(rig, caplog, monkeypatch):
    import sys
    import hartos.ai_key_vault as vault
    monkeypatch.setattr(vault, 'hf_token', lambda: '')
    monkeypatch.setitem(sys.modules, 'security.secret_redactor', None)
    rig.vault_error = RuntimeError('secret words')
    rig.svc.start()
    said = _no_token_warning(caplog)
    assert 'The vault could not be read (RuntimeError)' in said and 'secret words' not in said


@pytest.fixture
def real_vault(monkeypatch, tmp_path):
    """The real SecretsManager and AIKeyVault over a vault file of this
    test's own: ``open_with(key)`` starts a fresh process's view of it."""
    import hartos.ai_key_vault as vault
    from security import secrets_manager as sm
    monkeypatch.setattr(sm, '_VAULT_PATH', str(tmp_path / 'secrets.enc'))
    monkeypatch.setattr(sm, '_SALT_PATH', str(tmp_path / 'secrets.salt'))
    monkeypatch.setattr(sm.SecretsManager, '_instance', None)
    monkeypatch.delenv('HEVOLVE_HF_TOKEN', raising=False)

    def open_with(key):
        if key is None:
            monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
        else:
            monkeypatch.setenv('HEVOLVE_MASTER_KEY', key)
        monkeypatch.setattr(sm.SecretsManager, '_instance', None)
        store = vault.AIKeyVault()
        monkeypatch.setattr(vault, 'get_ai_key_vault', lambda: store)
        return store
    return open_with


REAL_TOKEN = 'hf_' + 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3l'


def _stored_in_a_real_vault(real_vault):
    store = real_vault('the right key')
    store._secrets_manager().set_secret('HEVOLVE_HF_TOKEN', REAL_TOKEN)
    return store


def test_the_real_vault_says_why_it_is_closed(real_vault):
    store = _stored_in_a_real_vault(real_vault)
    assert store.unavailable() == '' and store.get_tool_key('HEVOLVE_HF_TOKEN') == REAL_TOKEN
    # the same file, a process with no key
    store = real_vault(None)
    assert store.get_tool_key('HEVOLVE_HF_TOKEN') == '', 'a closed vault answers as an empty one'
    assert store.unavailable() == ('HEVOLVE_MASTER_KEY is not set, so the encrypted vault '
                                   'cannot be opened')
    # the same file, the wrong key
    store = real_vault('not the key')
    assert store.get_tool_key('HEVOLVE_HF_TOKEN') == ''
    assert 'HEVOLVE_MASTER_KEY does not open the encrypted vault' in store.unavailable()
    # and the right key again
    assert real_vault('the right key').unavailable() == ''


def test_no_vault_file_is_not_a_closed_vault(real_vault):
    """Environment variables only, the documented default: nothing is locked."""
    assert real_vault(None).unavailable() == ''
    assert real_vault('any key').unavailable() == ''


@pytest.mark.parametrize('key, why', [
    (None, 'HEVOLVE_MASTER_KEY is not set'),
    ('not the key', 'HEVOLVE_MASTER_KEY does not open the encrypted vault'),
])
def test_a_token_in_a_real_closed_vault_is_called_locked_not_missing(rig, caplog, real_vault,
                                                                    monkeypatch, key, why):
    import hartos.ai_key_vault as vault
    _stored_in_a_real_vault(real_vault)
    monkeypatch.setattr(vault, 'hf_token',
                        lambda: vault.get_ai_key_vault().get_tool_key(vault.HF_TOKEN_KEY) or '')
    real_vault(key)
    rig.svc.start()
    assert rig.svc._running is False and rig.started == []
    said = _no_token_warning(caplog)
    assert 'The vault could not be read (' + why in said, said
    assert 'Store it in the vault' not in said
    assert REAL_TOKEN not in caplog.text


def test_a_token_in_a_real_open_vault_starts_the_sidecar(rig, caplog, real_vault, monkeypatch):
    import hartos.ai_key_vault as vault
    _stored_in_a_real_vault(real_vault)
    monkeypatch.setattr(vault, 'hf_token',
                        lambda: vault.get_ai_key_vault().get_tool_key(vault.HF_TOKEN_KEY) or '')
    rig.svc.start()
    assert rig.svc._running is True and _child_token(rig) == REAL_TOKEN
    assert REAL_TOKEN not in caplog.text


def test_a_real_empty_vault_is_still_told_to_store_the_token(rig, caplog, real_vault, monkeypatch):
    import hartos.ai_key_vault as vault
    monkeypatch.setattr(vault, 'hf_token',
                        lambda: vault.get_ai_key_vault().get_tool_key(vault.HF_TOKEN_KEY) or '')
    real_vault(None)
    rig.svc.start()
    assert 'Store it in the vault' in _no_token_warning(caplog)


# -- a sidecar that is off says so, with the reason (get_status) ------------

def test_the_status_of_a_sidecar_that_is_off_says_why(rig, monkeypatch):
    import hartos.ai_key_vault as vault
    assert 'reason' not in rig.svc.get_status()
    rig.svc.start()
    status = rig.svc.get_status()
    assert status['running'] is False and status['ready'] is False
    assert status['reason'] == 'off: no Hugging Face token'
    monkeypatch.setattr(vault, 'hf_token', lambda: '')
    rig.vault_state = 'HEVOLVE_MASTER_KEY is not set, so the encrypted vault cannot be opened'
    rig.svc.start()
    assert rig.svc.get_status()['reason'] == (
        'off: the vault could not be read (HEVOLVE_MASTER_KEY is not set, so the encrypted '
        'vault cannot be opened)')


def test_the_status_of_a_started_sidecar_carries_no_reason(rig, monkeypatch):
    """Carried over: the four keys, and nothing more, once it runs."""
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', ENV)
    rig.svc.start()
    assert set(rig.svc.get_status()) == {'running', 'ready', 'alive', 'port'}


def test_a_vault_that_cannot_say_whether_it_is_closed_is_read_as_open(rig, caplog, monkeypatch):
    """Another store standing in for the vault (it has get_tool_key and no
    unavailable): its silence is not an error to report."""
    import hartos.ai_key_vault as vault
    bare = type('Bare', (), {'get_tool_key': lambda self, name: ''})()
    monkeypatch.setattr(vault, 'get_ai_key_vault', lambda: bare)
    monkeypatch.setattr(vault, 'hf_token', lambda: '')
    rig.svc.start()
    assert 'Store it in the vault' in _no_token_warning(caplog)


def test_the_lookups_own_log_line_carries_no_secret_from_the_vaults_error(caplog, monkeypatch):
    """hartos.ai_key_vault.hf_token logged the vault's error text as it came."""
    import hartos.ai_key_vault as vault
    leaked = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'

    class _Raises:
        def get_tool_key(self, name):
            raise RuntimeError(f'cannot parse entry {name}: {leaked}')
    monkeypatch.setattr(vault, 'get_ai_key_vault', lambda: _Raises())
    caplog.set_level(0)
    assert vault.hf_token() == ''
    said = [r.getMessage() for r in caplog.records if 'token lookup failed' in r.getMessage()]
    assert len(said) == 1 and 'cannot parse entry' in said[0] and '[REDACTED' in said[0], said
    assert leaked not in caplog.text
