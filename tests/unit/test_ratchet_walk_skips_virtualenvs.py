"""The repo-health ratchet measures the repo's own source, not a virtualenv
that happens to sit in the checkout.

MEASURED 2026-11-20 on a Windows dev box: an untracked ``venv-hart/`` in the
HARTOS checkout (24,166 .py files, 366 MB) was walked and AST-parsed by
test_source_guard_repo_health_ratchet's ``_walk``, because its SKIP set only
names ``venv`` and ``.venv``.  The test then ran past a 15-minute cap; on a
clean export of the same commit it took 26 to 62 s.  Its counts also took in
the virtualenv's swallows and duplicate names, so a local run measured a
different tree from CI's.
"""
from tests.unit.test_source_guard_repo_health_ratchet import _walk


def _write(path, text='x = 1\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _walked(repo):
    return sorted(rel.replace('\\', '/') for rel, _ in _walk(str(repo)))


def test_a_virtualenv_under_any_name_is_not_walked(tmp_path):
    # Its .py files outside site-packages too: only pyvenv.cfg marks those.
    _write(tmp_path / 'core' / 'real.py')
    _write(tmp_path / 'venv-hart' / 'pyvenv.cfg', 'home = C:/Python311\n')
    _write(tmp_path / 'venv-hart' / 'Scripts' / 'activate_this.py')
    _write(tmp_path / 'venv-hart' / 'Lib' / 'site-packages' / 'pkg' / 'mod.py')
    assert _walked(tmp_path) == ['core/real.py']


def test_a_site_packages_tree_is_not_walked(tmp_path):
    _write(tmp_path / 'core' / 'real.py')
    _write(tmp_path / 'python-embed' / 'Lib' / 'site-packages' / 'pkg' / 'mod.py')
    _write(tmp_path / 'deb' / 'dist-packages' / 'pkg' / 'mod.py')
    assert _walked(tmp_path) == ['core/real.py']


def test_a_source_folder_named_like_a_venv_is_still_walked(tmp_path):
    # No pyvenv.cfg: a source package whose name merely starts with "venv".
    _write(tmp_path / 'venv_tools' / 'helper.py')
    assert _walked(tmp_path) == ['venv_tools/helper.py']


def test_the_named_skips_still_apply(tmp_path):
    _write(tmp_path / 'core' / 'real.py')
    _write(tmp_path / 'node_modules' / 'x.py')
    _write(tmp_path / 'build' / 'y.py')
    assert _walked(tmp_path) == ['core/real.py']
