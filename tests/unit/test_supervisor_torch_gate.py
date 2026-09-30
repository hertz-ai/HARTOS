"""The hevolveai brain spawn is gated on the CHILD interpreter importing torch.

WHY (macOS incident 2026-06-16): the brain's api_server imports
weight_tracker -> ``import torch`` at module load.  ``_hevolveai_available``
only checked that *hevolveai* was importable in the parent, not torch.  On the
frozen macOS build ``_resolve_python_exe`` falls back to ``sys.executable`` --
the Nunba binary run as ``<exe> -c`` -- whose minimal frozen sys.path has no
torch, so the brain crash-looped ~20x and failed the post-build ``--validate``
DMG gate.

The fix adds ``_child_can_import_torch``: a POSITIVE capability gate that probes
the actual child interpreter once (cached) and is required by
``supervisor_should_run``.  Windows (python-embed child carries torch) is
unaffected -- the probe passes and the brain spawns normally.

Behavioral: mock the ``run_bounded`` boundary + ``_hevolveai_available``; assert
the gate.  Covers: probe pass/fail/timeout/spawn-error, caching, and the
supervisor_should_run composition.
"""
import os
import sys
import types
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import integrations.agent_engine.hevolveai_supervisor as sup  # noqa: E402


def _bounded(returncode=0, timed_out=False):
    """Stand-in for core.subprocess_safe.BoundedResult."""
    return types.SimpleNamespace(
        returncode=returncode, stdout='', stderr='', timed_out=timed_out)


def _reset_cache():
    sup._CHILD_TORCH_OK = None


def _patch_probe(**bounded_kw):
    """Patch run_bounded (imported lazily inside the probe) to return a
    BoundedResult-like object, and reset the module cache first."""
    _reset_cache()
    return patch('core.subprocess_safe.run_bounded',
                 return_value=_bounded(**bounded_kw))


# ── _child_can_import_torch ──────────────────────────────────────────
def test_torch_probe_true_when_child_resolves_torch():
    with _patch_probe(returncode=0):
        assert sup._child_can_import_torch() is True


def test_torch_probe_false_when_child_missing_torch():
    with _patch_probe(returncode=3):
        assert sup._child_can_import_torch() is False


def test_torch_probe_false_on_timeout():
    # run_bounded returns returncode=-1, timed_out=True on timeout.
    with _patch_probe(returncode=-1, timed_out=True):
        assert sup._child_can_import_torch() is False


def test_torch_probe_false_when_spawn_raises():
    _reset_cache()
    with patch('core.subprocess_safe.run_bounded',
               side_effect=FileNotFoundError('no interpreter')):
        assert sup._child_can_import_torch() is False


def test_torch_probe_is_cached_one_spawn_per_process():
    _reset_cache()
    with patch('core.subprocess_safe.run_bounded',
               return_value=_bounded(returncode=0)) as m:
        assert sup._child_can_import_torch() is True
        assert sup._child_can_import_torch() is True
        assert m.call_count == 1  # second call hit the cache, no respawn


# ── supervisor_should_run composition ────────────────────────────────
def test_should_run_false_when_torch_absent_even_if_hevolveai_present():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', None)
        os.environ.pop('HEVOLVEAI_API_URL', None)
        with patch.object(sup, '_hevolveai_available', return_value=True), \
                patch.object(sup, '_child_can_import_torch',
                             return_value=False):
            assert sup.supervisor_should_run() is False


def test_should_run_true_when_hevolveai_and_torch_present():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', None)
        os.environ.pop('HEVOLVEAI_API_URL', None)
        with patch.object(sup, '_hevolveai_available', return_value=True), \
                patch.object(sup, '_child_can_import_torch',
                             return_value=True):
            assert sup.supervisor_should_run() is True


def test_should_run_skips_torch_probe_when_hevolveai_absent():
    # Short-circuit order: never probe torch if hevolveai itself is missing.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', None)
        os.environ.pop('HEVOLVEAI_API_URL', None)
        with patch.object(sup, '_hevolveai_available', return_value=False), \
                patch.object(sup, '_child_can_import_torch') as probe:
            assert sup.supervisor_should_run() is False
            probe.assert_not_called()


# ── a torch the child can FIND but cannot READ (live 2026-09-25) ──────
# The installed python-embed's sitecustomize puts ~/.nunba/site-packages
# first; its torch/ carried an Administrators-only ACL, so under the
# normal (unelevated) token find_spec resolved it and the gate passed,
# then the child died reading torch/__init__.py with PermissionError and
# crash-looped 5x per breaker window.  These tests run the REAL probe in a
# REAL child interpreter against a fake torch package the child can find
# but cannot open, and a readable control.
import contextlib  # noqa: E402
import logging  # noqa: E402

import pytest  # noqa: E402


@contextlib.contextmanager
def _unreadable(path):
    """Hold ``path`` so no other process can read it, while its directory
    entry (what find_spec checks) stays visible.  Windows: an open handle
    with share mode 0 -> any other open fails ERROR_SHARING_VIOLATION
    (PermissionError, errno 13), exactly the error the live child raised.
    POSIX: mode 000."""
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        h = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if h in (None, wintypes.HANDLE(-1).value):
            pytest.skip('could not lock the fake torch file exclusively')
        try:
            yield
        finally:
            k32.CloseHandle(h)
    else:
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            pytest.skip('root reads mode-000 files')
        os.chmod(path, 0)
        try:
            yield
        finally:
            os.chmod(path, 0o644)


def _fake_torch(tmp_path, monkeypatch):
    pkg = tmp_path / 'torch'
    pkg.mkdir()
    init = pkg / '__init__.py'
    init.write_text('x = 1\n')
    # The probe inherits this env; PYTHONPATH puts the fake torch ahead of
    # any real one, as sitecustomize put ~/.nunba/site-packages first.
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    # Probe THIS interpreter, never a dev hevolveai checkout's pinned one.
    monkeypatch.setattr(sup, '_resolve_repo_root', lambda: None)
    monkeypatch.setattr(sup, '_resolve_python_exe', lambda: sys.executable)
    _reset_cache()
    return init


def test_torch_probe_true_for_a_readable_torch(tmp_path, monkeypatch):
    _fake_torch(tmp_path, monkeypatch)
    assert sup._child_can_import_torch() is True


def test_torch_probe_false_when_child_finds_torch_but_cannot_read_it(
        tmp_path, monkeypatch, caplog):
    init = _fake_torch(tmp_path, monkeypatch)
    with _unreadable(init), caplog.at_level(logging.INFO,
                                             logger='hevolve_agent_engine'):
        verdict = sup._child_can_import_torch()
    assert verdict is False
    # Actionable: the operator is told WHICH file and WHY, not just "no torch".
    text = caplog.text
    assert str(init) in text or repr(str(init))[1:-1] in text
    assert 'errno 13' in text.lower()


# ── an EMPTY leftover torch/ directory (review of 865130b86) ──────────
# A torch/ dir with no __init__.py is a namespace package: find_spec
# returns a spec with origin None, so the open() check was skipped and the
# gate passed, yet ``import torch`` gives an empty module and the brain's
# first ``torch.<attr>`` dies -- the same crash loop.  Real child, real
# probe; the exit code it returns is the named one.

def _spy_run_bounded(monkeypatch, flags=()):
    """Run the REAL run_bounded, recording each result; ``flags`` are
    interpreter options put before the probe's own ``-c`` (e.g. -S: no
    site-packages, so no installed torch can answer instead of the fake)."""
    from core import subprocess_safe
    seen = []
    real = subprocess_safe.run_bounded

    def spy(argv, *a, **kw):
        res = real([argv[0], *flags, *argv[1:]], *a, **kw)
        seen.append(res)
        return res
    monkeypatch.setattr(subprocess_safe, 'run_bounded', spy)
    return seen


def test_torch_probe_false_for_an_empty_torch_dir(tmp_path, monkeypatch,
                                                   caplog):
    (tmp_path / 'torch').mkdir()
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setattr(sup, '_resolve_repo_root', lambda: None)
    monkeypatch.setattr(sup, '_resolve_python_exe', lambda: sys.executable)
    # -S: a child with no site-packages, so no installed torch can stand in
    # for the empty dir -- the shape of the live box, where the leftover was
    # all there was.
    seen = _spy_run_bounded(monkeypatch, ['-S'])
    _reset_cache()
    with caplog.at_level(logging.INFO, logger='hevolve_agent_engine'):
        verdict = sup._child_can_import_torch()
    assert verdict is False
    assert [r.returncode for r in seen] == [sup.TORCH_PROBE_EXIT_NOT_A_PACKAGE]
    assert str(tmp_path / 'torch') in caplog.text
    assert 'not a package' in caplog.text


def test_torch_probe_readable_torch_exits_ok(tmp_path, monkeypatch):
    _fake_torch(tmp_path, monkeypatch)
    seen = _spy_run_bounded(monkeypatch, ['-S'])
    assert sup._child_can_import_torch() is True
    assert [r.returncode for r in seen] == [0]


def test_torch_probe_unreadable_exit_code_is_the_named_one(
        tmp_path, monkeypatch):
    init = _fake_torch(tmp_path, monkeypatch)
    seen = _spy_run_bounded(monkeypatch)
    with _unreadable(init):
        assert sup._child_can_import_torch() is False
    assert [r.returncode for r in seen] == [sup.TORCH_PROBE_EXIT_UNREADABLE]


def test_torch_probe_not_found_exit_code_is_the_named_one(monkeypatch):
    # -S -I: no site-packages, no PYTHONPATH -> no torch anywhere.
    monkeypatch.setattr(sup, '_resolve_repo_root', lambda: None)
    monkeypatch.setattr(sup, '_resolve_python_exe', lambda: sys.executable)
    seen = _spy_run_bounded(monkeypatch, ['-S', '-I'])
    _reset_cache()
    assert sup._child_can_import_torch() is False
    assert [r.returncode for r in seen] == [sup.TORCH_PROBE_EXIT_NOT_FOUND]


@pytest.mark.parametrize('code,words', [
    (3, 'not found'), (4, 'not readable'), (5, 'not a package')])
def test_log_names_the_exit_code(code, words, caplog):
    assert sup.TORCH_PROBE_EXIT_REASONS[code] == words
    with _patch_probe(returncode=code), \
            caplog.at_level(logging.INFO, logger='hevolve_agent_engine'):
        assert sup._child_can_import_torch() is False
    assert f'exit {code}: {words}' in caplog.text


def test_exit_codes_are_distinct_and_nonzero():
    codes = [sup.TORCH_PROBE_EXIT_NOT_FOUND, sup.TORCH_PROBE_EXIT_UNREADABLE,
             sup.TORCH_PROBE_EXIT_NOT_A_PACKAGE]
    assert len(set(codes)) == 3 and 0 not in codes and 1 not in codes
    assert set(sup.TORCH_PROBE_EXIT_REASONS) == set(codes)
