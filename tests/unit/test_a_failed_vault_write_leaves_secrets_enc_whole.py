"""A vault write that fails part-way leaves secrets.enc as it was.

Review rv5dd of 5dd7e7331, 22 Nov 2026, finding 2 (measured): _save_vault,
the one writer behind set_secret and delete_secret, rewrote secrets.enc in
place with open('wb'), with no temp file, no rename and no lock.  ENOSPC
raised mid-write left a 0-byte file, a secret unrelated to the write was
lost from disk, and the take-back's log said "taken back in memory only".
5dd7e7331 made that write far more frequent: every phone answer, failed
grant and revoke now writes the vault.

Now the vault is written through the one atomic writer
(core.file_cache.atomic_bytes_write: a temp file beside it, flush, fsync,
os.replace), under one lock, and a failure says what the file still holds.

Real: SecretsManager with a temp vault dir and a fake vault key, the
consent blueprint and vault (fixtures of
test_the_phone_answers_a_credential_ask.py).  Faked: the disk running out
of space.  Every value here is a fake test value.
"""
import errno
import logging
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from tests.unit.test_the_phone_answers_a_credential_ask import (  # noqa: E402,F401 (fixtures)
    OWNER, phone, world)

OTHER = 'fake-unrelated-secret-9Z'
TYPED = 'fake-typed-value-4M'
FAKE_VAULT_KEY = 'fake-vault-key-for-a-test-only'
_REAL_OPEN, _REAL_FDOPEN = open, os.fdopen


@pytest.fixture
def vault_dir(monkeypatch, tmp_path):
    """secrets.enc and its salt in a temp dir, opened with a fake key."""
    from security import secrets_manager
    from security.secrets_manager import SecretsManager
    monkeypatch.setattr(secrets_manager, '_VAULT_PATH', str(tmp_path / 'secrets.enc'))
    monkeypatch.setattr(secrets_manager, '_SALT_PATH', str(tmp_path / 'secrets.salt'))
    monkeypatch.setenv('HEVOLVE_MASTER_KEY', FAKE_VAULT_KEY)
    SecretsManager.reset()
    yield tmp_path
    SecretsManager.reset()


def _on_disk(name):
    """A fresh read of secrets.enc, as the next start reads it."""
    from security.secrets_manager import SecretsManager
    return SecretsManager()._cache.get(name)


class _NoSpace:
    """A file opened for writing whose every write fails: the disk is full."""

    def __init__(self, f):
        self.f = f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def write(self, data):
        raise OSError(errno.ENOSPC, 'No space left on device')

    def __getattr__(self, name):
        return getattr(self.f, name)


def _disk_full(folder):
    """From now on every file opened for writing under ``folder`` fails on
    its first write, after the open, whether the vault's writer opens it
    with open() or with os.fdopen on a descriptor it made.  Returns the
    function that frees the disk again."""
    from security import secrets_manager
    folder = os.path.abspath(str(folder))

    def _open(path, mode='r', *args, **kwargs):
        f = _REAL_OPEN(path, mode, *args, **kwargs)
        inside = os.path.abspath(str(path)).startswith(folder)
        return _NoSpace(f) if inside and 'w' in mode else f

    def _fdopen(fd, mode='r', *args, **kwargs):
        f = _REAL_FDOPEN(fd, mode, *args, **kwargs)
        return _NoSpace(f) if 'w' in mode else f

    secrets_manager.open = _open
    os.fdopen = _fdopen

    def free():
        secrets_manager.__dict__.pop('open', None)
        os.fdopen = _REAL_FDOPEN
    return free


@pytest.mark.parametrize('write', ['set_secret', 'delete_secret'])
def test_a_write_that_fails_after_the_file_opens_leaves_secrets_enc_whole(
        vault_dir, caplog, write):
    from security.secrets_manager import SecretsManager
    sm = SecretsManager.get_instance()
    sm.set_secret('FX_OTHER', OTHER)
    sm.set_secret('FX_KEPT', TYPED)
    free = _disk_full(vault_dir)
    try:
        with caplog.at_level(logging.INFO, logger='hevolve_security'):
            with pytest.raises(OSError) as raised:
                if write == 'set_secret':
                    sm.set_secret('FX_NEW', 'fake-new-value')
                else:
                    sm.delete_secret('FX_KEPT')
    finally:
        free()

    assert raised.value.errno == errno.ENOSPC
    assert os.path.getsize(str(vault_dir / 'secrets.enc')) > 0
    assert _on_disk('FX_OTHER') == OTHER            # the unrelated secret
    assert _on_disk('FX_KEPT') == TYPED             # the file as it was
    assert _on_disk('FX_NEW') is None
    assert sorted(os.listdir(str(vault_dir))) == ['secrets.enc', 'secrets.salt'], \
        'no temp file is left beside the vault'
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any('secrets.enc' in m and 'not replaced' in m for m in said), said
    assert not any(OTHER in m or TYPED in m for m in caplog.messages)


def test_a_save_overtaken_by_another_loses_no_secret(vault_dir, monkeypatch):
    """Each save writes the whole vault.  Without one writer at a time, a
    save that is overtaken (its snapshot taken, then a slow disk) lands
    after the newer one and drops the newer secret from disk."""
    from security.secrets_manager import SecretsManager
    sm = SecretsManager.get_instance()
    real_encrypt = sm._fernet.encrypt
    first_waiting, second_saved = threading.Event(), threading.Event()
    calls = []

    def encrypt(data):
        calls.append(1)
        if len(calls) == 1:             # the first save, held up by the disk
            first_waiting.set()
            second_saved.wait(2)        # overtaken here, if it can be
        return real_encrypt(data)
    monkeypatch.setattr(sm._fernet, 'encrypt', encrypt)

    def second():
        sm.set_secret('FX_SECOND', 'fake-second')
        second_saved.set()
    first = threading.Thread(target=sm.set_secret, args=('FX_FIRST', 'fake-first'))
    first.start()
    assert first_waiting.wait(10)
    later = threading.Thread(target=second)
    later.start()
    for th in (first, later):
        th.join(30)

    assert _on_disk('FX_FIRST') == 'fake-first'
    assert _on_disk('FX_SECOND') == 'fake-second'


def test_a_take_back_whose_write_fails_says_what_secrets_enc_holds(
        world, phone, vault_dir, caplog):
    """The reviewer's measured case: a phone answer whose grant fails, and
    the take-back's own write hits a full disk.  Readers in this process
    stop getting the value, the file keeps every other secret, and the log
    says the value is still in secrets.enc (it was written before the
    grant), not that it was "taken back in memory only"."""
    from unittest.mock import patch
    from hartos.ai_key_vault import get_ai_key_vault
    from integrations.social import consent_api
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    from security.secrets_manager import SecretsManager
    SecretsManager.get_instance().set_secret('FX_OTHER', OTHER)
    with db_session() as db:
        rid = ConsentService.request_consent(db, OWNER, 'credential',
                                             scope='secret:FX_WF').id
    frees = []

    def grant_fails(*args, **kwargs):
        frees.append(_disk_full(vault_dir))
        raise RuntimeError('database is locked')

    try:
        with caplog.at_level(logging.INFO):
            with patch.object(consent_api.ConsentService, 'grant_consent',
                              side_effect=grant_fails):
                try:
                    status = phone('', {'consent_type': 'credential',
                                        'request_id': rid,
                                        'value': TYPED}).status_code
                except RuntimeError:
                    status = 500
    finally:
        for free in frees:
            free()

    assert frees, 'the grant was never reached'
    assert status == 500
    assert get_ai_key_vault().get_tool_key('FX_WF') == ''
    assert SecretsManager.get_instance().get_secret('FX_WF') == ''
    assert os.path.getsize(str(vault_dir / 'secrets.enc')) > 0
    assert _on_disk('FX_OTHER') == OTHER
    lines = [r.getMessage() for r in caplog.records if 'FX_WF' in r.getMessage()
             and r.levelno >= logging.WARNING]
    assert any('secrets.enc' in m for m in lines), lines
    assert not any('in memory only' in m for m in lines), lines
    assert not any(TYPED in m or OTHER in m for m in caplog.messages)
