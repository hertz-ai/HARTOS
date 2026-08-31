"""Behavioural tests for core.venv_paths — the single source of truth for
per-backend venv resolution.

This module exists to close a PARALLEL-PATHS bug (2026-05-03, chatterbox_turbo):
the INSTALL path (Nunba tts.backend_venv) and the SPAWN path (HARTOS gpu_worker)
must compute the SAME venv path, or the worker dies with ModuleNotFoundError
because the dep was pip-installed into a venv the spawn path never reads. Both
consumers import from here so the path is computed in exactly one place — these
tests pin that resolution + its security validation + its platform branches so a
future edit can't silently reintroduce the drift.

0% covered before this file. Drives the REAL functions and asserts observable
behaviour (returned paths, raised ValueErrors, filesystem existence), never
source substrings.

    python -m pytest tests/unit/test_venv_paths.py -q --noconftest
"""
from __future__ import annotations

import os

import pytest

from core import venv_paths


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Every test starts from a clean cache and an override pointing at a
    throwaway dir, so nothing touches the real ~/Documents/Nunba tree."""
    venv_paths._reset_cache_for_tests()
    monkeypatch.setenv("NUNBA_VENV_ROOT_OVERRIDE", str(tmp_path / "venvs"))
    yield
    venv_paths._reset_cache_for_tests()


# ── venv_root: resolution order + caching ───────────────────────────────────
class TestVenvRoot:
    def test_override_env_wins_and_dir_is_created(self, tmp_path):
        root = venv_paths.venv_root()
        assert root == str(tmp_path / "venvs")
        assert os.path.isdir(root), "venv_root must materialise the override dir"

    def test_override_is_not_cached_so_a_changed_override_takes_effect(
            self, monkeypatch, tmp_path):
        first = venv_paths.venv_root()
        second_dir = tmp_path / "other"
        monkeypatch.setenv("NUNBA_VENV_ROOT_OVERRIDE", str(second_dir))
        assert venv_paths.venv_root() == str(second_dir)
        assert venv_paths.venv_root() != first

    def test_blank_override_falls_through_to_data_dir(self, monkeypatch, tmp_path):
        # An empty / whitespace override must NOT be honoured (it would resolve
        # to cwd or "" and scatter venvs); fall through to platform_paths.
        monkeypatch.setenv("NUNBA_VENV_ROOT_OVERRIDE", "   ")
        monkeypatch.setattr(
            "core.platform_paths.get_data_dir", lambda: str(tmp_path / "dd"))
        root = venv_paths.venv_root()
        assert root == os.path.join(str(tmp_path / "dd"), "data", "venvs")

    def test_data_dir_result_is_cached(self, monkeypatch, tmp_path):
        monkeypatch.delenv("NUNBA_VENV_ROOT_OVERRIDE", raising=False)
        calls = []

        def _fake_dd():
            calls.append(1)
            return str(tmp_path / "dd")

        monkeypatch.setattr("core.platform_paths.get_data_dir", _fake_dd)
        r1 = venv_paths.venv_root()
        r2 = venv_paths.venv_root()
        assert r1 == r2
        assert len(calls) == 1, "venv_root must cache the non-override resolution"


# ── backend-name validation (path-traversal defence) ────────────────────────
class TestBackendNameValidation:
    @pytest.mark.parametrize("name", ["chatterbox_turbo", "luxtts", "vlm-worker",
                                      "a", "abc123", "123"])
    def test_valid_names_resolve_under_root(self, name):
        p = venv_paths.venv_path(name)
        assert p == os.path.join(venv_paths.venv_root(), name)

    @pytest.mark.parametrize("bad", ["", "../etc", "a/b", "a\\b", "foo/../bar",
                                     ".hidden", "has space", "semi;colon"])
    def test_unsafe_names_are_rejected(self, bad):
        with pytest.raises(ValueError):
            venv_paths.venv_path(bad)

    def test_non_string_is_rejected(self):
        with pytest.raises(ValueError):
            venv_paths.venv_path(None)  # type: ignore[arg-type]

    def test_traversal_can_never_escape_root(self):
        # The whole point: no backend string may join to a path outside root.
        with pytest.raises(ValueError):
            venv_paths.venv_path("../../../../etc/passwd")


# ── venv_python: platform-correct interpreter path ──────────────────────────
class TestVenvPython:
    def test_windows_uses_scripts_python_exe(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "win32")
        p = venv_paths.venv_python("luxtts")
        assert p == os.path.join(venv_paths.venv_path("luxtts"),
                                 "Scripts", "python.exe")

    def test_posix_uses_bin_python(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        p = venv_paths.venv_python("luxtts")
        assert p == os.path.join(venv_paths.venv_path("luxtts"), "bin", "python")

    def test_macos_uses_bin_python(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "darwin")
        p = venv_paths.venv_python("luxtts")
        assert p.endswith(os.path.join("bin", "python"))

    def test_install_and_spawn_paths_agree(self, monkeypatch):
        # The regression this module closes: the INSTALL side and the SPAWN side
        # must derive byte-identical interpreter paths from the same backend id.
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        install_side = venv_paths.venv_python("chatterbox_turbo")
        spawn_side = venv_paths.venv_python("chatterbox_turbo")
        assert install_side == spawn_side


# ── venv_python_if_exists: existence-checked fallthrough ─────────────────────
class TestVenvPythonIfExists:
    def test_none_and_empty_return_none(self):
        assert venv_paths.venv_python_if_exists(None) is None
        assert venv_paths.venv_python_if_exists("") is None

    def test_invalid_name_returns_none_not_raise(self):
        # The spawn path must fall through to python-embed on a bad id, never
        # crash — so ValueError is swallowed into None here (unlike venv_python).
        assert venv_paths.venv_python_if_exists("../evil") is None

    def test_missing_venv_returns_none(self):
        assert venv_paths.venv_python_if_exists("never_installed") is None

    def test_existing_interpreter_is_returned(self, monkeypatch):
        monkeypatch.setattr(venv_paths.sys, "platform", "linux")
        target = venv_paths.venv_python("realbackend")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        assert venv_paths.venv_python_if_exists("realbackend") == target
