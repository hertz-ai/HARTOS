"""A save waits a moment for another handle to let go of the file.

Push review of 4a1649e21, 22 Nov 2026 (measured, the demo machine is
Windows): a vault save while another handle held secrets.enc open raised
PermissionError (errno 13) at os.replace.  The atomic writer left the file
whole and passed the error on, so the owner's phone answer got the 500
"could not be stored".  The in-place write before it (open 'wb') needed no
delete access and passed the same overlap, at the cost of a torn file on a
crash.  CPython, and most scanners, open a file on Windows without
FILE_SHARE_DELETE, and os.replace onto it is refused while that handle is
open.

Now the one atomic writer (core.file_cache, behind every atomic_json_write
and the vault's atomic_bytes_write) tries the final move again for about
0.2 s on PermissionError, and only the move: a directory that refuses the
temp file still fails at once (test_admin_config_concurrent_save).

Real: SecretsManager with a temp vault dir and a fake vault key, the
writer, a real reader holding the file on Windows.  Faked in the last two
tests: os.replace refusing, so the retry runs on any system.  Every value
here is a fake test value.
"""
import json
import logging
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

FAKE_VAULT_KEY = 'fake-vault-key-for-a-test-only'
WINDOWS = sys.platform == 'win32'


@pytest.fixture
def vault_dir(monkeypatch, tmp_path):
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


def _left_beside(folder, *names):
    return sorted(set(os.listdir(str(folder))) - set(names))


def _hold(path, seconds):
    """Open ``path`` for reading, as another process starting up reads the
    vault, and close it after ``seconds`` (None: when the returned event is
    set).  Returns (held, release, thread)."""
    held, release = threading.Event(), threading.Event()

    def holder():
        with open(path, 'rb') as f:
            f.read(1)
            held.set()
            release.wait(seconds if seconds is not None else 30)
    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(10), 'the holder never opened the file'
    return held, release, th


def _replace_spy(monkeypatch, refusals=None):
    """Count os.replace calls; the first ``refusals`` of them are refused as
    Windows refuses a move onto a file another handle holds open (None:
    none refused, the real os.replace decides)."""
    real = os.replace
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if refusals is not None and len(calls) <= refusals:
            raise PermissionError(13, 'Access is denied', str(dst))
        return real(src, dst)
    monkeypatch.setattr(os, 'replace', replace)
    return calls


def test_a_vault_save_waits_for_a_reader_to_let_go(vault_dir, caplog):
    from security.secrets_manager import SecretsManager
    sm = SecretsManager.get_instance()
    sm.set_secret('FX_OLD', 'fake-old-value')
    _, _, holder = _hold(str(vault_dir / 'secrets.enc'), 0.1)

    raised = None
    with caplog.at_level(logging.INFO):
        try:
            sm.set_secret('FX_NEW', 'fake-new-value')
        except PermissionError as e:
            raised = e
    holder.join(10)

    assert raised is None, f'the save failed while a reader held the file: {raised!r}'
    assert _on_disk('FX_NEW') == 'fake-new-value'
    assert _on_disk('FX_OLD') == 'fake-old-value'
    assert _left_beside(vault_dir, 'secrets.enc', 'secrets.salt') == []
    held = [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and 'held open' in r.getMessage()]
    if WINDOWS:                 # only Windows refuses the move meanwhile
        assert len(held) == 1, held
    assert not any('fake-' in m for m in caplog.messages)


@pytest.mark.skipif(not WINDOWS, reason='only Windows refuses a move onto an '
                                        'open file')
def test_a_holder_that_never_lets_go_fails_the_save_after_a_moment(
        vault_dir, monkeypatch, caplog):
    from core import file_cache
    from security.secrets_manager import SecretsManager
    sm = SecretsManager.get_instance()
    sm.set_secret('FX_OLD', 'fake-old-value')
    _, release, holder = _hold(str(vault_dir / 'secrets.enc'), None)
    calls = _replace_spy(monkeypatch)

    raised, started = None, time.monotonic()
    try:
        with caplog.at_level(logging.INFO):
            try:
                sm.set_secret('FX_NEW', 'fake-new-value')
            except PermissionError as e:
                raised = e
    finally:
        took = time.monotonic() - started
        release.set()
        holder.join(10)

    assert isinstance(raised, PermissionError)
    assert len(calls) == getattr(file_cache, '_REPLACE_TRIES', 5) > 1
    assert took < 2.0, f'the save held the vault for {took:.2f} s'
    assert _on_disk('FX_OLD') == 'fake-old-value'           # the old bytes
    assert _on_disk('FX_NEW') is None
    assert _left_beside(vault_dir, 'secrets.enc', 'secrets.salt') == []
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any('not replaced' in m for m in said), said
    assert not any('held open' in m for m in said)          # it never got through


def test_a_refused_move_is_tried_again_on_any_system(tmp_path, monkeypatch, caplog):
    """The same retry for every JSON save (atomic_json_write)."""
    from core.file_cache import atomic_json_write
    target = tmp_path / 'saved.json'
    target.write_text(json.dumps({'round': 0}), encoding='utf-8')
    calls = _replace_spy(monkeypatch, refusals=2)

    raised = None
    with caplog.at_level(logging.WARNING, logger='hevolve_core'):
        try:
            atomic_json_write(str(target), {'round': 1})
        except PermissionError as e:
            raised = e

    assert raised is None, f'two refusals failed the save: {raised!r}'
    assert len(calls) == 3
    assert json.loads(target.read_text(encoding='utf-8')) == {'round': 1}
    assert _left_beside(tmp_path, 'saved.json') == []
    held = [r.getMessage() for r in caplog.records if 'held open' in r.getMessage()]
    assert len(held) == 1 and 'try 3 of' in held[0], held


def test_a_holder_of_a_quarter_second_is_waited_out(tmp_path, monkeypatch):
    """A save takes about 12 ms, so a holder of 0.1 s outlasts several;
    the retry covers at least 0.25 s (review rv4a1)."""
    from core.file_cache import atomic_bytes_write
    target = tmp_path / 'secrets.enc'
    target.write_bytes(b'old')
    real, until = os.replace, []

    def replace(src, dst):
        if not until:
            until.append(time.monotonic() + 0.25)
        if time.monotonic() < until[0]:
            raise PermissionError(13, 'Access is denied', str(dst))
        return real(src, dst)
    monkeypatch.setattr(os, 'replace', replace)

    raised = None
    try:
        atomic_bytes_write(str(target), b'new')
    except PermissionError as e:
        raised = e

    assert raised is None, f'a 0.25 s holder failed the save: {raised!r}'
    assert target.read_bytes() == b'new'
    assert _left_beside(tmp_path, 'secrets.enc') == []


def test_a_move_refused_every_time_fails_at_its_bound(tmp_path, monkeypatch, caplog):
    from core import file_cache
    from core.file_cache import atomic_json_write
    target = tmp_path / 'saved.json'
    target.write_text(json.dumps({'round': 0}), encoding='utf-8')
    calls = _replace_spy(monkeypatch, refusals=10 ** 6)

    raised, started = None, time.monotonic()
    with caplog.at_level(logging.WARNING, logger='hevolve_core'):
        try:
            atomic_json_write(str(target), {'round': 1})
        except PermissionError as e:
            raised = e
    took = time.monotonic() - started

    assert isinstance(raised, PermissionError)
    assert len(calls) == getattr(file_cache, '_REPLACE_TRIES', 5) > 1
    assert took < 1.0, f'{took:.2f} s: the retry is not bounded'
    assert json.loads(target.read_text(encoding='utf-8')) == {'round': 0}
    assert _left_beside(tmp_path, 'saved.json') == []
    assert not [r for r in caplog.records if 'held open' in r.getMessage()]
