"""An unplugged model drive must not stop the node from starting.

4208c81df made model storage honour HEVOLVE_MODEL_DIR, so a big model can live
on an external drive instead of filling C: (measured 2026-09-21: C: had 23 GB
free against F:'s 468 GB). But ModelStorageManager.__init__ ran
``self.base_dir.mkdir(parents=True, exist_ok=True)`` eagerly, and the module
builds its singleton at IMPORT (``model_storage = ModelStorageManager()``).
Before that commit the only directory it ever created was ~/.hevolve/models,
which is always reachable. After it, the eager mkdir ran against a user path,
so with the drive unplugged:

    import integrations.service_tools.model_storage
    -> FileNotFoundError [WinError 3] The system cannot find the path specified

and vram_manager fails through service_tools/__init__.py, taking Nunba's TTS
down with it (found by fix-all's cross-review, reproduced 2026-09-23).

The fix creates the directory LAZILY, on the first write. It deliberately does
NOT fall back to the home directory: a silent fallback would make the person's
models "disappear" and could start re-downloading them onto C:, which filled
to 100% on 2026-09-13. An operation that needs an unreachable directory
reports it; importing does not.

"Unreachable" is simulated portably: a path whose parent is a regular FILE
cannot be created on Windows, macOS or Linux. A literal ``Q:/`` would be a
valid relative directory name on Linux, so it would prove nothing there.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from integrations.service_tools.model_storage import ModelStorageManager

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def unreachable(tmp_path):
    """A model directory that cannot exist: its parent is a file."""
    blocker = tmp_path / 'not_a_dir'
    blocker.write_text('a file, so nothing can be created beneath it')
    return blocker / 'models'


def test_constructing_against_an_unreachable_dir_does_not_raise(unreachable):
    storage = ModelStorageManager(base_dir=unreachable)
    assert storage.base_dir == unreachable
    assert not unreachable.exists(), 'construction must not try to create it'


def test_importing_with_the_drive_unplugged_does_not_raise(unreachable):
    """The real path the person hits: the module is imported at boot with the
    env var pointing at a drive that is not there.  Run in a subprocess so the
    singleton in THIS process is left alone."""
    env = dict(os.environ)
    env['HEVOLVE_MODEL_DIR'] = str(unreachable)
    env['PYTHONPATH'] = str(REPO) + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.run(
        # importlib, not `import pkg.model_storage as m`: the package
        # __init__ re-exports the singleton under the same name, so the
        # plain form binds `m` to the INSTANCE and not the module.
        [sys.executable, '-c',
         'import importlib; '
         'm = importlib.import_module("integrations.service_tools.model_storage"); '
         'print("BASE", m.model_storage.base_dir)'],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f'importing model_storage with an unreachable HEVOLVE_MODEL_DIR must '
        f'not crash the node; stderr tail:\n{proc.stderr[-800:]}')
    assert 'BASE' in proc.stdout


def test_a_fresh_reachable_dir_is_created_on_the_first_write(tmp_path):
    """Lazy creation must not break a normal first install: the directory does
    not exist yet, and the first write makes it."""
    fresh = tmp_path / 'first' / 'install' / 'models'
    storage = ModelStorageManager(base_dir=fresh)
    assert not fresh.exists()
    storage._write_manifest({'tools': {'x': {'downloaded': True}}})
    assert storage.manifest_file.exists()
    assert storage.get_manifest() == {'tools': {'x': {'downloaded': True}}}


def test_writing_to_an_unreachable_dir_fails_honestly(unreachable):
    """No silent fallback to the home directory: the write fails, and nothing
    lands anywhere else."""
    storage = ModelStorageManager(base_dir=unreachable)
    with pytest.raises(OSError):
        storage._write_manifest({'tools': {}})
    assert storage.base_dir == unreachable, 'must not have moved elsewhere'


def test_reading_an_unreachable_dir_does_not_crash_and_says_so(unreachable):
    """Reading must not crash -- and must not be MISTAKEN for an empty store.
    This test used to assert only the empty manifest, which quietly encoded
    "drive unplugged" as "no models installed". fix-all's review caught it: an
    empty answer is what makes a caller decide to fetch the models again."""
    storage = ModelStorageManager(base_dir=unreachable)
    assert storage.get_manifest() == {'tools': {}}
    assert storage.is_reachable() is False


def test_an_unplugged_drive_is_distinguishable_from_an_empty_store(
        unreachable, tmp_path):
    """The two truths an empty manifest used to hide: nothing is installed, or
    the drive holding the models is not there. Only the first means 'go get
    them'."""
    empty = ModelStorageManager(base_dir=tmp_path / 'fresh' / 'models')
    gone = ModelStorageManager(base_dir=unreachable)
    assert empty.is_reachable() is True
    assert gone.is_reachable() is False
    assert empty.get_manifest() == gone.get_manifest() == {'tools': {}}, (
        'both read as empty -- so the manifest alone cannot tell them apart')


def test_an_existing_store_is_reachable(tmp_path):
    store = tmp_path / 'models'
    store.mkdir()
    assert ModelStorageManager(base_dir=store).is_reachable() is True


def test_a_store_path_that_is_a_file_is_not_reachable(tmp_path):
    f = tmp_path / 'models'
    f.write_text('not a directory')
    assert ModelStorageManager(base_dir=f).is_reachable() is False
