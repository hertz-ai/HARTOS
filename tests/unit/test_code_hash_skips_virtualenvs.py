"""The code hash walks this node's own source, never a virtualenv inside it.

Measured 2026-10-21 on a dev checkout (D:/.../HARTOS with an untracked
``venv-hart/``): ``_EXCLUDE_DIRS`` knows ``venv``, ``.venv`` and ``venv310``
by NAME, so a virtualenv under any other name is walked and every one of
its .py files hashed.  Two boot threads (``hartos-bootstrap`` via
``start_relay_hub`` and ``platform-init`` via telemetry) sat in
``compute_code_hash`` for over 12 minutes; meanwhile ``init_social`` never
ran, every ``/api/social/*`` route answered 404 and the Tier-1 chat adapter
never started.

A virtualenv is recognised by what it IS (it carries ``pyvenv.cfg``), and an
installed-package tree by its ``site-packages`` directory, whatever the
parent is called.
"""
from pathlib import Path

from security.node_integrity import compute_file_manifest


def _write(path: Path, text: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_a_virtualenv_under_any_name_is_not_hashed(tmp_path):
    _write(tmp_path / "pkg" / "a.py")
    # A virtualenv named nothing like "venv": pyvenv.cfg marks it.
    _write(tmp_path / "venv-hart" / "pyvenv.cfg", "home = C:/Python311\n")
    _write(tmp_path / "venv-hart" / "Lib" / "site-packages" / "big.py")
    _write(tmp_path / "venv-hart" / "Scripts" / "activate_this.py")

    manifest = compute_file_manifest(str(tmp_path))

    assert set(manifest) == {"pkg/a.py"}


def test_a_site_packages_tree_is_not_hashed_wherever_it_sits(tmp_path):
    _write(tmp_path / "pkg" / "a.py")
    _write(tmp_path / "tools" / "site-packages" / "dep" / "mod.py")

    manifest = compute_file_manifest(str(tmp_path))

    assert set(manifest) == {"pkg/a.py"}


def test_the_named_excludes_still_apply(tmp_path):
    _write(tmp_path / "pkg" / "a.py")
    _write(tmp_path / ".venv" / "lib" / "x.py")
    _write(tmp_path / "node_modules" / "y.py")
    _write(tmp_path / "tests" / "test_z.py")

    manifest = compute_file_manifest(str(tmp_path))

    assert set(manifest) == {"pkg/a.py"}
