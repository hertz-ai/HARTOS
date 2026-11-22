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
