"""Two admin-config saves at once neither fail nor leave a stale file.

bff95ab44 swapped tempfile.mkstemp (which spins up to 2**31 times on a
refused Windows directory) for ONE fixed temp name, admin_config.json.tmp.
Two saves at once then share that file: one thread's os.replace moves the
other's half-written temp into place, or finds it already gone and the save
fails.  Saves do run at once: every consent answer for the camera or screen
saves (consent_service._embodied_feed_from_consent), and so do the admin
routes, each on its own request thread.

The fix reuses the repo's one atomic writer, core.file_cache.
atomic_json_write (unique temp in the same dir, fsync, os.replace), which
now creates its temp with ONE exclusive open instead of mkstemp's retry
loop, and serialises saves so the last save writes the latest state.

    python -m pytest tests/unit/test_admin_config_concurrent_save.py -q
"""
import json
import logging
import os
import sys
import threading

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def test_concurrent_saves_all_succeed_and_the_file_holds_the_last_state(
        tmp_path, monkeypatch, caplog):
    from integrations.channels.admin.api import AdminAPI

    cfg = tmp_path / 'admin_config.json'
    monkeypatch.setattr(AdminAPI, '_config_path', lambda self: str(cfg))
    api = AdminAPI()
    threads, rounds = 8, 40
    start = threading.Barrier(threads)

    def _saver(n):
        start.wait()
        for i in range(rounds):
            api._channels = {f'ch{n}': {'round': i, 'pad': 'x' * 2000}}
            api._save_config()

    with caplog.at_level(logging.WARNING,
                         logger='integrations.channels.admin.api'):
        workers = [threading.Thread(target=_saver, args=(n,))
                   for n in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(120)

    failed = [r.getMessage() for r in caplog.records
              if 'Failed to save admin config' in r.getMessage()]
    assert failed == [], f'{len(failed)} saves failed, e.g. {failed[:2]}'
    on_disk = json.loads(cfg.read_text(encoding='utf-8'))
    assert on_disk['channels'] == api._channels, (
        'the file does not hold the state the last save was asked to write')
    left = [p.name for p in tmp_path.iterdir() if p.name != cfg.name]
    assert left == [], f'temp files left behind: {left}'


def test_the_shared_writer_alone_survives_concurrent_writers(tmp_path):
    """atomic_json_write has other callers (create_recipe) with no lock of
    their own: each call needs its own temp file, or concurrent writers of
    one path collide on it."""
    from core.file_cache import atomic_json_write

    target = str(tmp_path / 'shared.json')
    threads, rounds = 8, 40
    start = threading.Barrier(threads)
    errors = []

    def _writer(n):
        start.wait()
        for i in range(rounds):
            try:
                atomic_json_write(target, {'writer': n, 'round': i,
                                           'pad': 'x' * 2000})
            except Exception as e:  # noqa: BLE001 -- counted, then asserted
                errors.append(repr(e))

    workers = [threading.Thread(target=_writer, args=(n,))
               for n in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(120)

    # On Windows os.replace onto a file another thread is replacing at
    # that instant can be refused (WinError 5/32): that is the OS, not a
    # shared temp, and it leaves the target intact.  A shared temp shows
    # up as FileExistsError / FileNotFoundError on the temp itself.
    shared = [e for e in errors
              if 'FileExistsError' in e or 'FileNotFoundError' in e]
    assert shared == [], f'{len(shared)} writes collided, e.g. {shared[:2]}'
    data = json.loads(open(target, encoding='utf-8').read())
    assert set(data) == {'writer', 'round', 'pad'}
    left = [p.name for p in tmp_path.iterdir() if p.name != 'shared.json']
    assert left == [], f'temp files left behind: {left}'


def test_the_shared_writer_creates_its_temp_with_one_try(tmp_path, monkeypatch):
    """The reason bff95ab44 dropped mkstemp stays fixed in the shared writer:
    a directory that refuses the temp file fails the write at once."""
    import pytest
    from core.file_cache import atomic_json_write

    tries = []

    def refusing_os_open(path, flags, *a, **k):
        tries.append(path)
        raise PermissionError(13, 'Access is denied', str(path))

    monkeypatch.setattr(os, 'open', refusing_os_open)
    with pytest.raises(PermissionError):
        atomic_json_write(str(tmp_path / 'x.json'), {'a': 1})
    assert len(tries) == 1
