"""Tests for core.platform_paths — the canonical data-dir / log-rotation helper.

This is a single-source used everywhere (DB path, prompts, logs, memory). It was
partially covered; the untested paths were the platform-branch selection in
get_data_dir (Windows/macOS/Linux-XDG/embedded-HARTOS) and the whole
cleanup_old_logs rotation (age + size budget). A bug in either mis-locates data
or deletes the wrong files, so both are worth pinning.

Pure stdlib (os/sys/time) — tests drive it with env + module-global monkeypatch
and tmp dirs; nothing touches the real ~/Documents/Nunba tree.
"""
from __future__ import annotations

import os
import time

import pytest

import core.platform_paths as pp


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # get_data_dir caches in a module global; clear it before AND after so tests
    # neither see a stale cache nor leak one. Also clear the env overrides.
    for k in ('NUNBA_DATA_DIR', 'HARTOS_DATA_DIR', 'XDG_DATA_HOME'):
        monkeypatch.delenv(k, raising=False)
    pp.reset_cache()
    yield
    pp.reset_cache()


# ── get_data_dir precedence + platform branches ─────────────────────────────
def test_nunba_data_dir_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    assert pp.get_data_dir() == str(tmp_path)


def test_hartos_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv('HARTOS_DATA_DIR', str(tmp_path))
    assert pp.get_data_dir() == str(tmp_path)


def test_result_is_cached(monkeypatch, tmp_path):
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    first = pp.get_data_dir()
    # Change the env; without reset_cache the cached value must persist.
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'other'))
    assert pp.get_data_dir() == first


def test_embedded_hartos_release(monkeypatch):
    monkeypatch.setattr(pp, '_IS_LINUX', True, raising=False)
    monkeypatch.setattr(pp, '_IS_WINDOWS', False, raising=False)
    monkeypatch.setattr(pp, '_IS_MACOS', False, raising=False)
    monkeypatch.setattr(os.path, 'isfile',
                        lambda p: p == '/etc/hartos-release')
    assert pp.get_data_dir() == '/var/lib/hartos'


def test_windows_default(monkeypatch):
    monkeypatch.setattr(pp, '_IS_WINDOWS', True, raising=False)
    monkeypatch.setattr(pp, '_IS_MACOS', False, raising=False)
    monkeypatch.setattr(pp, '_IS_LINUX', False, raising=False)
    monkeypatch.setattr(os.path, 'isfile', lambda p: False)
    monkeypatch.setattr(os.path, 'expanduser', lambda p: '/home/u')
    assert pp.get_data_dir() == os.path.join('/home/u', 'Documents', 'Nunba')


def test_macos_default(monkeypatch):
    monkeypatch.setattr(pp, '_IS_WINDOWS', False, raising=False)
    monkeypatch.setattr(pp, '_IS_MACOS', True, raising=False)
    monkeypatch.setattr(pp, '_IS_LINUX', False, raising=False)
    monkeypatch.setattr(os.path, 'isfile', lambda p: False)
    monkeypatch.setattr(os.path, 'expanduser', lambda p: '/Users/u')
    assert pp.get_data_dir() == os.path.join(
        '/Users/u', 'Library', 'Application Support', 'Nunba')


def test_linux_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, '_IS_WINDOWS', False, raising=False)
    monkeypatch.setattr(pp, '_IS_MACOS', False, raising=False)
    monkeypatch.setattr(pp, '_IS_LINUX', True, raising=False)
    monkeypatch.setattr(os.path, 'isfile', lambda p: False)
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    assert pp.get_data_dir() == os.path.join(str(tmp_path), 'nunba')


def test_linux_no_xdg_falls_back_to_config(monkeypatch):
    monkeypatch.setattr(pp, '_IS_WINDOWS', False, raising=False)
    monkeypatch.setattr(pp, '_IS_MACOS', False, raising=False)
    monkeypatch.setattr(pp, '_IS_LINUX', True, raising=False)
    monkeypatch.setattr(os.path, 'isfile', lambda p: False)
    monkeypatch.setattr(os.path, 'expanduser', lambda p: '/home/u')
    assert pp.get_data_dir() == os.path.join('/home/u', '.config', 'nunba')


def test_derived_dirs_hang_off_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    assert pp.get_db_dir() == os.path.join(str(tmp_path), 'data')
    assert pp.get_db_path().endswith('hevolve_database.db')
    assert pp.get_db_path('x.db').endswith('x.db')


# ── cleanup_old_logs: age + size rotation ───────────────────────────────────
def _mk_log(d, name, age_days=0, size=10):
    p = os.path.join(d, name)
    with open(p, 'wb') as f:
        f.write(b'x' * size)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


def test_cleanup_no_dir_is_noop(monkeypatch, tmp_path):
    missing = str(tmp_path / 'nope')
    monkeypatch.setattr(pp, 'get_log_dir', lambda: missing)
    pp.cleanup_old_logs()  # must not raise


def test_cleanup_deletes_old_keeps_recent(monkeypatch, tmp_path):
    d = str(tmp_path)
    monkeypatch.setattr(pp, 'get_log_dir', lambda: d)
    old = _mk_log(d, 'old.log', age_days=30)
    recent = _mk_log(d, 'recent.log', age_days=0)
    non_log = _mk_log(d, 'keep.txt', age_days=30)  # not a *.log → untouched
    pp.cleanup_old_logs(max_age_days=7, max_total_mb=50)
    assert not os.path.exists(old)
    assert os.path.exists(recent)
    assert os.path.exists(non_log)


def test_cleanup_size_budget_deletes_oldest_first(monkeypatch, tmp_path):
    d = str(tmp_path)
    monkeypatch.setattr(pp, 'get_log_dir', lambda: d)
    # All recent (age 0) so phase-1 keeps them; phase-2 must trim by size.
    big_old = _mk_log(d, 'a.log', age_days=1, size=2 * 1024 * 1024)
    big_new = _mk_log(d, 'b.log', age_days=0, size=2 * 1024 * 1024)
    # 3 MB budget, 4 MB present -> delete the OLDEST (a.log, 2 MB) and the
    # remaining 2 MB is under budget, so the newest survives. (A 1 MB budget
    # would delete BOTH, since even one 2 MB file exceeds it — that is correct
    # behaviour, just not the oldest-first property this test isolates.)
    pp.cleanup_old_logs(max_age_days=3650, max_total_mb=3)
    assert not os.path.exists(big_old)
    assert os.path.exists(big_new)


def test_cleanup_matches_rotated_log_suffix(monkeypatch, tmp_path):
    d = str(tmp_path)
    monkeypatch.setattr(pp, 'get_log_dir', lambda: d)
    rotated = _mk_log(d, 'app.log.3', age_days=30)  # *.log.* pattern
    pp.cleanup_old_logs(max_age_days=7)
    assert not os.path.exists(rotated)


# ── ensure_data_dirs + reset_cache ──────────────────────────────────────────
def test_ensure_data_dirs_creates_tree(monkeypatch, tmp_path):
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    pp.ensure_data_dirs()
    assert os.path.isdir(pp.get_db_dir())
    assert os.path.isdir(pp.get_log_dir())


def test_reset_cache_forces_recompute(monkeypatch, tmp_path):
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    assert pp.get_data_dir() == str(tmp_path)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path / 'two'))
    pp.reset_cache()
    assert pp.get_data_dir() == str(tmp_path / 'two')
